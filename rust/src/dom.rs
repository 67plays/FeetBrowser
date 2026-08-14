//! Rust implementation of the DOM bridge, ported from `feetbrowser/jsdom.py`.
//!
//! The Python DOM node tree (`feetbrowser.htmlparser.Element`/`Text`) remains
//! the single source of truth: these functions read and mutate the underlying
//! Python node objects via pyo3 (getattr/setattr/call on `tag`, `attributes`,
//! `children`, `is_focused`, `style`, text nodes, ...) exactly as `jsdom.py`
//! did, so the Python layout/rendering code keeps working unchanged.
//!
//! Exposed to Python as `dom_get(kind, target, name)`, `dom_set(...)`,
//! `dom_call(...)`. The Python classes in `jsdom.py` are thin shims whose
//! `js_get`/`js_set` delegate here, and native methods are returned as
//! `_DomMethod` callables that dispatch back into `dom_call`.

use pyo3::conversion::IntoPyObjectExt;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList, PyTuple};
use std::collections::BTreeSet;
use std::sync::OnceLock;

use crate::pybind::PyJsValue;
use crate::value::JsValue;

// Tags serialized as self-closing voids by the innerHTML serializer
// (matches the VOID_TAGS set in jsdom.py).
const VOID_TAGS: &[&str] = &["br", "img", "hr", "input", "meta", "link", "base"];

static UNDEFINED_SENTINEL: OnceLock<Py<PyAny>> = OnceLock::new();

fn undefined_py(py: Python<'_>) -> PyResult<Py<PyAny>> {
    if let Some(u) = UNDEFINED_SENTINEL.get() {
        return Ok(u.clone_ref(py));
    }
    let jsengine = py.import("feetbrowser.jsengine")?;
    let u = jsengine.getattr("UNDEFINED")?.unbind();
    let _ = UNDEFINED_SENTINEL.set(u.clone_ref(py));
    Ok(u)
}

fn is_undefined(py: Python<'_>, v: &Bound<'_, PyAny>) -> bool {
    match UNDEFINED_SENTINEL.get() {
        Some(u) => v.is(u.bind(py)),
        None => match py.import("feetbrowser.jsengine") {
            Ok(m) => match m.getattr("UNDEFINED") {
                Ok(u) => {
                    let _ = UNDEFINED_SENTINEL.set(u.clone().unbind());
                    v.is(&u)
                }
                Err(_) => false,
            },
            Err(_) => false,
        },
    }
}

fn str_of(_py: Python<'_>, v: &Bound<'_, PyAny>) -> PyResult<String> {
    let s = v.str()?;
    s.extract::<String>()
}

/// Extract the i-th argument as a string: a plain str is used directly,
/// otherwise Python `str()` is applied (matches `str(value)` in jsdom.py).
fn str_arg(py: Python<'_>, args: &[Py<PyAny>], i: usize) -> String {
    match args.get(i) {
        Some(a) => a
            .bind(py)
            .extract::<String>()
            .unwrap_or_else(|_| str_of(py, a.bind(py)).unwrap_or_default()),
        None => String::new(),
    }
}

/// The Python `int(name)` helper for classList indexing.
fn int_index(name: &str) -> Option<i64> {
    name.parse::<i64>().ok()
}

/// Extract the i-th argument as an integer: floats (JS numbers cross the
/// boundary as Python floats) are truncated, plain strings are parsed.
fn arg_i64(py: Python<'_>, args: &[Py<PyAny>], i: usize) -> i64 {
    match args.get(i) {
        Some(a) => {
            let b = a.bind(py);
            if let Ok(n) = b.extract::<f64>() {
                return n as i64;
            }
            if let Ok(s) = b.extract::<String>() {
                if let Ok(n) = s.parse::<f64>() {
                    return n as i64;
                }
            }
            -1
        }
        None => -1,
    }
}

// -- node helpers -----------------------------------------------------------

fn node_tag(node: &Bound<'_, PyAny>) -> String {
    node.getattr("tag")
        .and_then(|t| t.extract::<String>())
        .unwrap_or_default()
}

fn node_attributes<'py>(node: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyDict>> {
    node.getattr("attributes")?.cast_into::<PyDict>().map_err(Into::into)
}

fn attr_str(node: &Bound<'_, PyAny>, key: &str) -> Option<String> {
    let attrs = node_attributes(node).ok()?;
    match attrs.get_item(key).ok()? {
        Some(v) => v.extract::<String>().ok(),
        None => None,
    }
}

fn mark_dirty(flag: &Bound<'_, PyAny>) {
    if let Ok(f) = flag.cast::<PyDict>() {
        let _ = f.set_item("dirty", true);
    }
}

/// Yield the Element nodes under `node` (including `node` itself) in document
/// order, mirroring `_iter_elements`.
fn iter_elements(py: Python<'_>, node: &Bound<'_, PyAny>) -> PyResult<Vec<Py<PyAny>>> {
    let mut out = Vec::new();
    let mut stack: Vec<Py<PyAny>> = vec![node.clone().unbind()];
    while let Some(n) = stack.pop() {
        let nb = n.bind(py);
        if nb.getattr("tag").is_ok() {
            out.push(n.clone_ref(py));
        }
        if let Ok(children) = nb.getattr("children") {
            if let Ok(list) = children.cast::<PyList>() {
                for c in list.iter().rev() {
                    stack.push(c.unbind());
                }
            }
        }
    }
    Ok(out)
}

fn find_by_tag(py: Python<'_>, root: &Bound<'_, PyAny>, tag: &str) -> PyResult<Option<Py<PyAny>>> {
    for n in iter_elements(py, root)? {
        if node_tag(n.bind(py)) == tag {
            return Ok(Some(n));
        }
    }
    Ok(None)
}

fn make_element<'py>(
    py: Python<'py>,
    tag: &str,
    parent: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyAny>> {
    let htmlparser = py.import("feetbrowser.htmlparser")?;
    let cls = htmlparser.getattr("Element")?;
    let attrs = PyDict::new(py);
    cls.call1((tag, attrs, parent))
}

fn make_text<'py>(
    py: Python<'py>,
    text: &str,
    parent: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyAny>> {
    let htmlparser = py.import("feetbrowser.htmlparser")?;
    let cls = htmlparser.getattr("Text")?;
    cls.call1((text, parent))
}

// -- wrapper construction ---------------------------------------------------

/// Build a `_DomMethod` callable for a native method, cached per target so the
/// method keeps its identity across `js_get` calls (like the old bound
/// methods in jsdom.py's `_methods` dict).
fn method(py: Python<'_>, kind: &str, target: &Bound<'_, PyAny>, name: &str) -> PyResult<Py<PyAny>> {
    let cache = match target.getattr("_dom_methods") {
        Ok(c) => c.cast_into::<PyDict>()?,
        Err(_) => {
            let d = PyDict::new(py);
            target.setattr("_dom_methods", &d)?;
            d
        }
    };
    if let Some(m) = cache.get_item(name)? {
        return Ok(m.unbind());
    }
    let dm = Py::new(
        py,
        DomMethod {
            kind: kind.to_string(),
            target: target.clone().unbind(),
            name: name.to_string(),
        },
    )?
    .into_py_any(py)?;
    cache.set_item(name, &dm)?;
    Ok(dm)
}

fn wrap_element(py: Python<'_>, node: &Bound<'_, PyAny>, flag: &Bound<'_, PyAny>) -> PyResult<Py<PyAny>> {
    let jsdom = py.import("feetbrowser.jsdom")?;
    let cls = jsdom.getattr("JSElement")?;
    let tuple = PyTuple::new(py, vec![node.clone().unbind(), flag.clone().unbind()])?;
    Ok(cls.call(tuple, None)?.unbind())
}

fn wrap_nodelist(py: Python<'_>, items: Vec<Py<PyAny>>, flag: &Bound<'_, PyAny>) -> PyResult<Py<PyAny>> {
    let jsdom = py.import("feetbrowser.jsdom")?;
    let cls = jsdom.getattr("JSNodeList")?;
    let list = PyList::new(py, items)?;
    let items: Vec<Py<PyAny>> = vec![list.into_any().unbind(), flag.clone().unbind()];
    let tuple = PyTuple::new(py, items)?;
    Ok(cls.call(tuple, None)?.unbind())
}

fn is_jsdom_instance(py: Python<'_>, obj: &Py<PyAny>, cls_name: &str) -> bool {
    let jsdom = match py.import("feetbrowser.jsdom") {
        Ok(m) => m,
        Err(_) => return false,
    };
    let cls = match jsdom.getattr(cls_name) {
        Ok(c) => c,
        Err(_) => return false,
    };
    obj.bind(py).is_instance(&cls).unwrap_or(false)
}

/// Unwrap a `PyJsValue` back to the Python object it wraps when that value is
/// a JS host object (JSElement/JSNodeList/...). JS-to-Python conversions
/// (js_to_py) wrap Host values in PyJsValue, so DOM method args that JS passes
/// arrive here as PyJsValue rather than the raw JSElement; peeling them lets
/// appendChild(createElement(...)) & co. see the actual element.
fn unwrap_host(py: Python<'_>, a: &Py<PyAny>) -> Py<PyAny> {
    let b = a.bind(py);
    match b.extract::<PyJsValue>() {
        Ok(jv) => match &jv.value {
            JsValue::Host(h) => h.clone_ref(py),
            _ => a.clone_ref(py),
        },
        Err(_) => a.clone_ref(py),
    }
}

// -- serialization ----------------------------------------------------------

fn escape_text(text: &str) -> String {
    text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
}

fn escape_attr(value: &str) -> String {
    escape_text(value).replace('"', "&quot;")
}

fn serialize_children(py: Python<'_>, children: &Bound<'_, PyList>) -> PyResult<String> {
    let mut out = String::new();
    for c in children.iter() {
        if let Ok(t) = c.getattr("text") {
            if let Ok(s) = t.extract::<String>() {
                out.push_str(&escape_text(&s));
                continue;
            }
        }
        if c.getattr("tag").is_ok() {
            out.push_str(&serialize_element(py, &c)?);
        }
    }
    Ok(out)
}

fn serialize_element(py: Python<'_>, node: &Bound<'_, PyAny>) -> PyResult<String> {
    let tag = node_tag(node);
    let mut attrs = String::new();
    let adict = node_attributes(node)?;
    for (k, v) in adict.iter() {
        let ks: String = k.extract()?;
        let vs: String = v.extract()?;
        attrs.push_str(&format!(" {}=\"{}\"", ks, escape_attr(&vs)));
    }
    if VOID_TAGS.contains(&tag.as_str()) {
        Ok(format!("<{}{}>", tag, attrs))
    } else {
        let children = node.getattr("children")?.cast_into::<PyList>()?;
        let inner = serialize_children(py, &children)?;
        Ok(format!("<{}{}>{}</{}>", tag, attrs, inner, tag))
    }
}

// -- text / selector helpers ------------------------------------------------

fn text_content(py: Python<'_>, node: &Bound<'_, PyAny>) -> PyResult<String> {
    let mut out = String::new();
    let mut stack: Vec<Py<PyAny>> = vec![node.clone().unbind()];
    while let Some(n) = stack.pop() {
        let nb = n.bind(py);
        if let Ok(t) = nb.getattr("text") {
            if let Ok(s) = t.extract::<String>() {
                out.push_str(&s);
            }
        }
        if let Ok(children) = nb.getattr("children") {
            if let Ok(list) = children.cast::<PyList>() {
                for c in list.iter().rev() {
                    stack.push(c.unbind());
                }
            }
        }
    }
    Ok(out)
}

fn camel_to_kebab(name: &str) -> String {
    let mut out = String::new();
    for (i, c) in name.chars().enumerate() {
        if c.is_ascii_uppercase() && i > 0 {
            out.push('-');
        }
        out.push(c.to_ascii_lowercase());
    }
    out
}

fn python_title(s: &str) -> String {
    let mut chars = s.chars();
    match chars.next() {
        Some(first) => {
            let mut out = String::new();
            for c in first.to_uppercase() {
                out.push(c);
            }
            for c in chars.flat_map(|c| c.to_lowercase()) {
                out.push(c);
            }
            out
        }
        None => String::new(),
    }
}

fn camelize(s: &str) -> String {
    let parts: Vec<&str> = s.split('-').collect();
    let mut out = parts.first().copied().unwrap_or("").to_string();
    for p in parts.iter().skip(1) {
        out.push_str(&python_title(p));
    }
    out
}

fn is_valid_tag(s: &str) -> bool {
    let mut chars = s.chars();
    match chars.next() {
        Some(c) if c.is_ascii_alphabetic() => {
            chars.all(|c| c.is_ascii_alphanumeric() || c == '_' || c == '-')
        }
        _ => false,
    }
}

/// Parse a simple selector: tag, #id, .class, or combinations.
/// Returns (tag, classes, ids) or None if unsupported.
fn parse_selector(sel: &str) -> Option<(Option<String>, Vec<String>, Vec<String>)> {
    let sel = sel.trim();
    let mut parts: Vec<String> = Vec::new();
    let mut cur = String::new();
    for c in sel.chars() {
        if c == '#' || c == '.' {
            if !cur.is_empty() {
                parts.push(std::mem::take(&mut cur));
            }
            cur.push(c);
        } else {
            cur.push(c);
        }
    }
    if !cur.is_empty() {
        parts.push(cur);
    }
    let mut tag: Option<String> = None;
    let mut classes: Vec<String> = Vec::new();
    let mut ids: Vec<String> = Vec::new();
    for part in parts {
        if let Some(rest) = part.strip_prefix('#') {
            ids.push(rest.to_string());
        } else if let Some(rest) = part.strip_prefix('.') {
            classes.push(rest.to_string());
        } else if !part.is_empty() {
            if !is_valid_tag(&part) {
                return None;
            }
            tag = Some(part.to_lowercase());
        }
    }
    Some((tag, classes, ids))
}

fn selector_matches(node: &Bound<'_, PyAny>, tag: &Option<String>, classes: &[String], ids: &[String]) -> bool {
    if let Some(t) = tag {
        if node_tag(node) != *t {
            return false;
        }
    }
    if !ids.is_empty() {
        match attr_str(node, "id") {
            Some(id) => {
                if !ids.contains(&id) {
                    return false;
                }
            }
            None => return false,
        }
    }
    if !classes.is_empty() {
        let cls = attr_str(node, "class").unwrap_or_default();
        let set: BTreeSet<&str> = cls.split_whitespace().collect();
        for c in classes {
            if !set.contains(c.as_str()) {
                return false;
            }
        }
    }
    true
}

fn class_attr_contains(node: &Bound<'_, PyAny>, cls: &str) -> bool {
    match attr_str(node, "class") {
        Some(c) => c.split_whitespace().any(|w| w == cls),
        None => false,
    }
}

/// Find an element-typed relative of `node`: its first/last element child or
/// its next/previous element sibling (whichever `name` names).
fn sibling_element(
    py: Python<'_>,
    node: &Bound<'_, PyAny>,
    name: &str,
    flag: &Bound<'_, PyAny>,
) -> PyResult<Py<PyAny>> {
    if name == "firstElementChild" || name == "lastElementChild" {
        let children = match node.getattr("children") {
            Ok(c) => c.cast_into::<PyList>()?,
            Err(_) => return undefined_py(py),
        };
        let elements: Vec<_> = children.iter().filter(|c| c.getattr("tag").is_ok()).collect();
        let target = if name == "firstElementChild" {
            elements.first()
        } else {
            elements.last()
        };
        return match target {
            Some(t) => wrap_element(py, t, flag),
            None => undefined_py(py),
        };
    }
    let parent = match node.getattr("parent") {
        Ok(p) if !p.is_none() => p,
        _ => return undefined_py(py),
    };
    let children = match parent.getattr("children") {
        Ok(c) => c.cast_into::<PyList>()?,
        Err(_) => return undefined_py(py),
    };
    let elements: Vec<_> = children
        .iter()
        .enumerate()
        .filter(|(_, c)| c.getattr("tag").is_ok())
        .map(|(i, c)| (i, c))
        .collect();
    let mut pos = None;
    for (i, c) in children.iter().enumerate() {
        if c.is(node) {
            pos = Some(i);
            break;
        }
    }
    let pos = match pos {
        Some(p) => p,
        None => return undefined_py(py),
    };
    if name == "nextElementSibling" {
        for (i, c) in elements {
            if i > pos {
                return wrap_element(py, &c, flag);
            }
        }
    } else {
        for (i, c) in elements.iter().rev() {
            if *i < pos {
                return wrap_element(py, c, flag);
            }
        }
    }
    undefined_py(py)
}

// -- style helpers ----------------------------------------------------------

fn style_overrides<'py>(py: Python<'py>, node: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyDict>> {
    match node.getattr("_js_style_overrides") {
        Ok(o) => o.cast_into::<PyDict>().map_err(Into::into),
        Err(_) => {
            let d = PyDict::new(py);
            node.setattr("_js_style_overrides", &d)?;
            Ok(d)
        }
    }
}

fn write_style(
    py: Python<'_>,
    node: &Bound<'_, PyAny>,
    flag: &Bound<'_, PyAny>,
    kebab: &str,
    value: &Bound<'_, PyAny>,
) -> PyResult<()> {
    let sv = str_of(py, value)?;
    let overrides = style_overrides(py, node)?;
    overrides.set_item(kebab, &sv)?;
    let style = node.getattr("style")?.cast_into::<PyDict>()?;
    style.set_item(kebab, &sv)?;
    mark_dirty(flag);
    Ok(())
}

fn style_value(py: Python<'_>, node: &Bound<'_, PyAny>, kebab: &str) -> PyResult<Py<PyAny>> {
    let overrides = style_overrides(py, node)?;
    if let Some(v) = overrides.get_item(kebab)? {
        return Ok(v.unbind());
    }
    let style = node.getattr("style")?.cast_into::<PyDict>()?;
    match style.get_item(kebab)? {
        Some(v) => Ok(v.unbind()),
        None => Ok(String::new().into_py_any(py)?),
    }
}

// -- classList helpers ------------------------------------------------------

fn classes_set(_py: Python<'_>, node: &Bound<'_, PyAny>) -> PyResult<BTreeSet<String>> {
    let mut set = BTreeSet::new();
    if let Ok(attrs) = node_attributes(node) {
        if let Some(v) = attrs.get_item("class")? {
            if let Ok(s) = v.extract::<String>() {
                for w in s.split_whitespace() {
                    set.insert(w.to_string());
                }
            }
        }
    }
    Ok(set)
}

fn save_classes(_py: Python<'_>, node: &Bound<'_, PyAny>, flag: &Bound<'_, PyAny>, classes: &BTreeSet<String>) -> PyResult<()> {
    let attrs = node_attributes(node)?;
    let joined = classes.iter().cloned().collect::<Vec<_>>().join(" ");
    attrs.set_item("class", &joined)?;
    mark_dirty(flag);
    Ok(())
}

// -- URL helpers ------------------------------------------------------------

fn host_of(py: Python<'_>, base: &Bound<'_, PyAny>) -> PyResult<String> {
    let base_str = str_of(py, base)?;
    let urlparse = py.import("urllib.parse")?.getattr("urlparse")?;
    let parts = match urlparse.call1((&base_str,)) {
        Ok(p) => p,
        Err(_) => return Ok(String::new()),
    };
    match parts.getattr("hostname") {
        Ok(h) => {
            if h.is_none() {
                Ok(String::new())
            } else {
                Ok(h.extract::<String>().unwrap_or_default())
            }
        }
        Err(_) => Ok(String::new()),
    }
}

fn location_of(py: Python<'_>, doc: &Bound<'_, PyAny>) -> PyResult<Py<PyAny>> {
    let noop = Py::new(py, Noop)?.into_py_any(py)?;
    let base = doc.getattr("base_url")?;
    let base_str = if base.is_none() {
        String::new()
    } else {
        str_of(py, &base)?
    };
    let finish = |py: Python<'_>, d: &Bound<'_, PyDict>| -> PyResult<Py<PyAny>> {
        d.set_item("reload", noop.clone_ref(py))?;
        d.set_item("assign", noop.clone_ref(py))?;
        d.set_item("replace", noop.clone_ref(py))?;
        Ok(d.clone().into_any().unbind())
    };
    let defaults = |py: Python<'_>| -> PyResult<Py<PyAny>> {
        let d = PyDict::new(py);
        d.set_item("href", &base_str)?;
        d.set_item("hostname", "")?;
        d.set_item("protocol", "")?;
        d.set_item("pathname", "")?;
        d.set_item("search", "")?;
        d.set_item("hash", "")?;
        d.set_item("host", "")?;
        d.set_item("origin", "")?;
        d.set_item("port", "")?;
        finish(py, &d)
    };
    if base_str.is_empty() {
        return defaults(py);
    }
    let urlsplit = py.import("urllib.parse")?.getattr("urlsplit")?;
    let parts = match urlsplit.call1((&base_str,)) {
        Ok(p) => p,
        Err(_) => return defaults(py),
    };
    let scheme: String = parts
        .getattr("scheme")
        .and_then(|s| s.extract::<String>())
        .unwrap_or_default();
    if scheme.is_empty() {
        return defaults(py);
    }
    let hostname = parts
        .getattr("hostname")
        .map(|h| {
            if h.is_none() {
                String::new()
            } else {
                h.extract::<String>().unwrap_or_default()
            }
        })
        .unwrap_or_default();
    let path = parts
        .getattr("path")
        .and_then(|p| p.extract::<String>())
        .unwrap_or_default();
    let query = parts
        .getattr("query")
        .and_then(|q| q.extract::<String>())
        .unwrap_or_default();
    let netloc = parts
        .getattr("netloc")
        .and_then(|n| n.extract::<String>())
        .unwrap_or_default();
    let port = parts
        .getattr("port")
        .ok()
        .map(|p| {
            if p.is_none() {
                String::new()
            } else {
                str_of(py, &p).unwrap_or_default()
            }
        })
        .unwrap_or_default();
    let search = if query.is_empty() {
        String::new()
    } else {
        format!("?{}", query)
    };
    let d = PyDict::new(py);
    d.set_item("href", &base_str)?;
    d.set_item("hostname", &hostname)?;
    d.set_item("protocol", format!("{}:", scheme))?;
    d.set_item("pathname", &path)?;
    d.set_item("search", &search)?;
    d.set_item("hash", "")?;
    d.set_item("host", &netloc)?;
    d.set_item("origin", format!("{}://{}", scheme, netloc))?;
    d.set_item("port", &port)?;
    finish(py, &d)
}

// -- mutation helpers -------------------------------------------------------

fn set_text_content(
    py: Python<'_>,
    node: &Bound<'_, PyAny>,
    flag: &Bound<'_, PyAny>,
    value: &Bound<'_, PyAny>,
) -> PyResult<()> {
    let text = if value.is_none() || is_undefined(py, value) {
        String::new()
    } else {
        str_of(py, value)?
    };
    let tn = make_text(py, &text, node)?;
    let list = PyList::new(py, vec![tn.unbind()])?;
    node.setattr("children", list)?;
    mark_dirty(flag);
    Ok(())
}

fn set_inner_html(
    py: Python<'_>,
    node: &Bound<'_, PyAny>,
    flag: &Bound<'_, PyAny>,
    value: &Bound<'_, PyAny>,
) -> PyResult<()> {
    let html = if value.is_none() || is_undefined(py, value) {
        String::new()
    } else {
        str_of(py, value)?
    };
    let htmlparser = py.import("feetbrowser.htmlparser")?;
    let parser_cls = htmlparser.getattr("HTMLParser")?;
    let parser = parser_cls.call1((html,))?;
    let root = parser.call_method0("parse")?;
    let mut source: Option<Bound<'_, PyList>> = None;
    for n in iter_elements(py, &root)? {
        let nb = n.bind(py);
        if node_tag(nb) == "body" {
            source = Some(nb.getattr("children")?.cast_into::<PyList>()?);
            break;
        }
    }
    let source = match source {
        Some(s) => s,
        None => root.getattr("children")?.cast_into::<PyList>()?,
    };
    for c in source.iter() {
        c.setattr("parent", node)?;
    }
    node.setattr("children", source)?;
    mark_dirty(flag);
    Ok(())
}

fn set_title(
    py: Python<'_>,
    doc: &Bound<'_, PyAny>,
    value: &Bound<'_, PyAny>,
) -> PyResult<()> {
    let text = if value.is_none() || is_undefined(py, value) {
        String::new()
    } else {
        str_of(py, value)?
    };
    let root = doc.getattr("root")?;
    for n in iter_elements(py, &root)? {
        let nb = n.bind(py);
        if node_tag(nb) == "title" {
            let tn = make_text(py, &text, nb)?;
            let list = PyList::new(py, vec![tn.unbind()])?;
            nb.setattr("children", list)?;
            return Ok(());
        }
    }
    let root_children = root.getattr("children")?.cast_into::<PyList>()?;
    let mut head: Option<Bound<'_, PyAny>> = None;
    for c in root_children.iter() {
        if c.getattr("tag").is_ok() && node_tag(&c) == "head" {
            head = Some(c);
            break;
        }
    }
    let (parent, via_head) = match &head {
        Some(h) => (h.clone(), true),
        None => (root.clone(), false),
    };
    let title = make_element(py, "title", &parent)?;
    let tn = make_text(py, &text, &title)?;
    let list = PyList::new(py, vec![tn.unbind()])?;
    title.setattr("children", list)?;
    if via_head {
        let children = head.unwrap().getattr("children")?.cast_into::<PyList>()?;
        children.append(title)?;
    } else {
        let children = root.getattr("children")?.cast_into::<PyList>()?;
        children.insert(0, title)?;
    }
    Ok(())
}

// -- document ---------------------------------------------------------------

fn get_title(py: Python<'_>, root: &Bound<'_, PyAny>) -> PyResult<Py<PyAny>> {
    for n in iter_elements(py, root)? {
        let nb = n.bind(py);
        if node_tag(nb) == "title" {
            let mut text = String::new();
            if let Ok(children) = nb.getattr("children") {
                if let Ok(list) = children.cast::<PyList>() {
                    for c in list.iter() {
                        if let Ok(t) = c.getattr("text") {
                            if let Ok(s) = t.extract::<String>() {
                                text.push_str(&s);
                            }
                        }
                    }
                }
            }
            let trimmed = text.trim().to_string();
            if !trimmed.is_empty() {
                return Ok(trimmed.into_py_any(py)?);
            }
        }
    }
    Ok(String::new().into_py_any(py)?)
}

fn dataset(py: Python<'_>, node: &Bound<'_, PyAny>) -> PyResult<Py<PyAny>> {
    let attrs = node_attributes(node)?;
    let data = PyDict::new(py);
    for (k, v) in attrs.iter() {
        let ks: String = k.extract()?;
        if let Some(rest) = ks.strip_prefix("data-") {
            let key = camelize(rest);
            data.set_item(&key, v)?;
        }
    }
    Ok(data.into_any().unbind())
}

fn document_get(py: Python<'_>, doc: &Bound<'_, PyAny>, name: &str) -> PyResult<Py<PyAny>> {
    if matches!(
        name,
        "getElementById"
            | "querySelector"
            | "querySelectorAll"
            | "getElementsByTagName"
            | "getElementsByClassName"
            | "createElement"
            | "createTextNode"
            | "createDocumentFragment"
            | "addEventListener"
            | "removeEventListener"
    ) {
        return method(py, "document", doc, name);
    }
    let root = doc.getattr("root")?;
    let flag = doc.getattr("_flag")?;
    match name {
        "body" => match find_by_tag(py, &root, "body")? {
            Some(n) => wrap_element(py, n.bind(py), &flag),
            None => undefined_py(py),
        },
        "head" => match find_by_tag(py, &root, "head")? {
            Some(n) => wrap_element(py, n.bind(py), &flag),
            None => undefined_py(py),
        },
        "title" => get_title(py, &root),
        "documentElement" => wrap_element(py, &root, &flag),
        "readyState" => Ok("complete".into_py_any(py)?),
        "cookie" | "referrer" => Ok(String::new().into_py_any(py)?),
        "domain" => {
            let base = doc.getattr("base_url")?;
            Ok(host_of(py, &base)?.into_py_any(py)?)
        }
        "URL" => {
            let base = doc.getattr("base_url")?;
            let s = if base.is_none() {
                String::new()
            } else {
                str_of(py, &base)?
            };
            Ok(s.into_py_any(py)?)
        }
        "location" => location_of(py, doc),
        "visibilityState" => Ok("visible".into_py_any(py)?),
        "hidden" => Ok(false.into_py_any(py)?),
        "characterSet" => Ok("UTF-8".into_py_any(py)?),
        "contentType" => Ok("text/html".into_py_any(py)?),
        "fonts" => {
            let interp = doc.getattr("_interp")?;
            let jsdom = py.import("feetbrowser.jsdom")?;
            let cls = jsdom.getattr("JSFontFaceSet")?;
            let tuple = PyTuple::new(py, vec![flag.unbind(), interp.unbind()])?;
            Ok(cls.call(tuple, None)?.unbind())
        }
        "defaultView" => undefined_py(py),
        "all" => {
            let mut out = Vec::new();
            for n in iter_elements(py, &root)? {
                out.push(wrap_element(py, n.bind(py), &flag)?);
            }
            wrap_nodelist(py, out, &flag)
        }
        "scripts" | "images" => {
            let tag = if name == "scripts" { "script" } else { "img" };
            let mut out = Vec::new();
            for n in iter_elements(py, &root)? {
                if node_tag(n.bind(py)) == tag {
                    out.push(wrap_element(py, n.bind(py), &flag)?);
                }
            }
            wrap_nodelist(py, out, &flag)
        }
        _ => undefined_py(py),
    }
}

fn document_set(
    py: Python<'_>,
    doc: &Bound<'_, PyAny>,
    name: &str,
    value: &Bound<'_, PyAny>,
) -> PyResult<()> {
    let flag = doc.getattr("_flag")?;
    match name {
        "title" => {
            set_title(py, doc, value)?;
            mark_dirty(&flag);
        }
        "cookie" => mark_dirty(&flag),
        _ => {}
    }
    Ok(())
}

fn document_call(
    py: Python<'_>,
    doc: &Bound<'_, PyAny>,
    name: &str,
    args: &[Py<PyAny>],
) -> PyResult<Py<PyAny>> {
    let root = doc.getattr("root")?;
    let flag = doc.getattr("_flag")?;
    match name {
        "getElementById" => {
            let element_id = args
                .first()
                .and_then(|a| a.bind(py).extract::<String>().ok());
            for n in iter_elements(py, &root)? {
                if attr_str(n.bind(py), "id") == element_id {
                    return wrap_element(py, n.bind(py), &flag);
                }
            }
            undefined_py(py)
        }
        "querySelector" => {
            let sel = str_arg(py, args, 0);
            match parse_selector(&sel) {
                None => undefined_py(py),
                Some((tag, classes, ids)) => {
                    for n in iter_elements(py, &root)? {
                        if selector_matches(n.bind(py), &tag, &classes, &ids) {
                            return wrap_element(py, n.bind(py), &flag);
                        }
                    }
                    undefined_py(py)
                }
            }
        }
        "querySelectorAll" => {
            let sel = str_arg(py, args, 0);
            match parse_selector(&sel) {
                None => wrap_nodelist(py, Vec::new(), &flag),
                Some((tag, classes, ids)) => {
                    let mut out = Vec::new();
                    for n in iter_elements(py, &root)? {
                        if selector_matches(n.bind(py), &tag, &classes, &ids) {
                            out.push(wrap_element(py, n.bind(py), &flag)?);
                        }
                    }
                    wrap_nodelist(py, out, &flag)
                }
            }
        }
        "getElementsByTagName" => {
            let tag = str_arg(py, args, 0).to_lowercase();
            let mut out = Vec::new();
            for n in iter_elements(py, &root)? {
                if node_tag(n.bind(py)) == tag {
                    out.push(wrap_element(py, n.bind(py), &flag)?);
                }
            }
            wrap_nodelist(py, out, &flag)
        }
        "getElementsByClassName" => {
            let cls = str_arg(py, args, 0);
            let mut out = Vec::new();
            for n in iter_elements(py, &root)? {
                if class_attr_contains(n.bind(py), &cls) {
                    out.push(wrap_element(py, n.bind(py), &flag)?);
                }
            }
            wrap_nodelist(py, out, &flag)
        }
        "createElement" => {
            let tag = str_arg(py, args, 0);
            let el = make_element(py, &tag, py.None().bind(py))?;
            wrap_element(py, &el, &flag)
        }
        "createTextNode" => {
            let text = str_arg(py, args, 0);
            let tn = make_text(py, &text, py.None().bind(py))?;
            wrap_element(py, &tn, &flag)
        }
        "createDocumentFragment" => {
            // A lightweight container with its own child list; appending it
            // to an element moves the children over (see appendChild below).
            let jsdom = py.import("feetbrowser.jsdom")?;
            let cls = jsdom.getattr("JSFragment")?;
            let tuple = PyTuple::new(py, vec![flag.unbind()])?;
            Ok(cls.call(tuple, None)?.unbind())
        }
        "addEventListener" | "removeEventListener" => undefined_py(py),
        _ => undefined_py(py),
    }
}

// -- element ----------------------------------------------------------------

fn element_get(py: Python<'_>, el: &Bound<'_, PyAny>, name: &str) -> PyResult<Py<PyAny>> {
    if matches!(
        name,
        "setAttribute"
            | "getAttribute"
            | "removeAttribute"
            | "hasAttribute"
            | "appendChild"
            | "removeChild"
            | "addEventListener"
            | "removeEventListener"
            | "querySelector"
            | "querySelectorAll"
            | "getElementsByClassName"
            | "getElementsByTagName"
            | "matches"
            | "closest"
            | "remove"
            | "contains"
    ) {
        return method(py, "element", el, name);
    }
    let node = el.getattr("node")?;
    let flag = el.getattr("_flag")?;
    match name {
        "textContent" => Ok(text_content(py, &node)?.into_py_any(py)?),
        "innerHTML" => {
            let children = node.getattr("children")?.cast_into::<PyList>()?;
            Ok(serialize_children(py, &children)?.into_py_any(py)?)
        }
        "outerHTML" => Ok(serialize_element(py, &node)?.into_py_any(py)?),
        "tagName" => Ok(node_tag(&node).to_uppercase().into_py_any(py)?),
        "tag" => Ok(node_tag(&node).into_py_any(py)?),
        "children" => {
            let mut out = Vec::new();
            let children = node.getattr("children")?.cast_into::<PyList>()?;
            for c in children.iter() {
                if c.getattr("tag").is_ok() {
                    out.push(wrap_element(py, &c, &flag)?);
                }
            }
            wrap_nodelist(py, out, &flag)
        }
        "firstElementChild" | "lastElementChild" | "nextElementSibling"
        | "previousElementSibling" => {
            sibling_element(py, &node, name, &flag)
        }
        "childElementCount" => {
            let children = node.getattr("children")?.cast_into::<PyList>()?;
            let count = children
                .iter()
                .filter(|c| c.getattr("tag").is_ok())
                .count();
            Ok((count as u64).into_py_any(py)?)
        }
        "parentNode" => match node.getattr("parent") {
            Ok(p) if !p.is_none() => wrap_element(py, &p, &flag),
            _ => undefined_py(py),
        },
        "classList" => {
            let jsdom = py.import("feetbrowser.jsdom")?;
            let cls = jsdom.getattr("JSClassList")?;
            let tuple = PyTuple::new(py, vec![node.unbind(), flag.unbind()])?;
            Ok(cls.call(tuple, None)?.unbind())
        }
        "dataset" => dataset(py, &node),
        "id" => Ok(attr_str(&node, "id").unwrap_or_default().into_py_any(py)?),
        "className" => Ok(attr_str(&node, "class").unwrap_or_default().into_py_any(py)?),
        "style" => {
            let jsdom = py.import("feetbrowser.jsdom")?;
            let cls = jsdom.getattr("JSElementStyle")?;
            let tuple = PyTuple::new(py, vec![node.unbind(), flag.unbind()])?;
            Ok(cls.call(tuple, None)?.unbind())
        }
        _ => {
            let attrs = node_attributes(&node)?;
            match attrs.get_item(name)? {
                Some(v) => Ok(v.unbind()),
                None => undefined_py(py),
            }
        }
    }
}

fn element_set(
    py: Python<'_>,
    el: &Bound<'_, PyAny>,
    name: &str,
    value: &Bound<'_, PyAny>,
) -> PyResult<()> {
    let node = el.getattr("node")?;
    let flag = el.getattr("_flag")?;
    match name {
        "textContent" => set_text_content(py, &node, &flag, value)?,
        "innerHTML" => set_inner_html(py, &node, &flag, value)?,
        // `element.value` reads out of the attribute dictionary (the fallback
        // arm of element_get), so writing it has to put it back in the same
        // place -- otherwise `select.value = "b"` or `input.value = ""` would
        // read back the old value and change nothing on screen.
        "value" => {
            let attrs = node_attributes(&node)?;
            attrs.set_item("value", str_of(py, value)?)?;
            mark_dirty(&flag);
        }
        _ => {}
    }
    Ok(())
}

fn element_call(
    py: Python<'_>,
    el: &Bound<'_, PyAny>,
    name: &str,
    args: &[Py<PyAny>],
) -> PyResult<Py<PyAny>> {
    let node = el.getattr("node")?;
    let flag = el.getattr("_flag")?;
    match name {
        "setAttribute" => {
            let aname = str_arg(py, args, 0);
            let avalue = str_arg(py, args, 1);
            let attrs = node_attributes(&node)?;
            attrs.set_item(&aname, &avalue)?;
            mark_dirty(&flag);
            undefined_py(py)
        }
        "getAttribute" => {
            let aname = str_arg(py, args, 0);
            let attrs = node_attributes(&node)?;
            match attrs.get_item(&aname)? {
                Some(v) => Ok(v.unbind()),
                None => Ok(py.None()),
            }
        }
        "removeAttribute" => {
            let aname = str_arg(py, args, 0);
            let attrs = node_attributes(&node)?;
            let _ = attrs.del_item(&aname);
            mark_dirty(&flag);
            undefined_py(py)
        }
        "hasAttribute" => {
            let aname = str_arg(py, args, 0);
            let attrs = node_attributes(&node)?;
            Ok(attrs.contains(&aname)?.into_py_any(py)?)
        }
        "querySelector" => {
            let sel = str_arg(py, args, 0);
            match parse_selector(&sel) {
                None => undefined_py(py),
                Some((tag, classes, ids)) => {
                    for n in iter_elements(py, &node)? {
                        let nb = n.bind(py);
                        if nb.is(&node) {
                            continue;
                        }
                        if selector_matches(nb, &tag, &classes, &ids) {
                            return wrap_element(py, nb, &flag);
                        }
                    }
                    undefined_py(py)
                }
            }
        }
        "querySelectorAll" => {
            let sel = str_arg(py, args, 0);
            match parse_selector(&sel) {
                None => wrap_nodelist(py, Vec::new(), &flag),
                Some((tag, classes, ids)) => {
                    let mut out = Vec::new();
                    for n in iter_elements(py, &node)? {
                        let nb = n.bind(py);
                        if nb.is(&node) {
                            continue;
                        }
                        if selector_matches(nb, &tag, &classes, &ids) {
                            out.push(wrap_element(py, nb, &flag)?);
                        }
                    }
                    wrap_nodelist(py, out, &flag)
                }
            }
        }
        "getElementsByClassName" => {
            let cls = str_arg(py, args, 0);
            let mut out = Vec::new();
            for n in iter_elements(py, &node)? {
                let nb = n.bind(py);
                if nb.is(&node) {
                    continue;
                }
                if class_attr_contains(nb, &cls) {
                    out.push(wrap_element(py, nb, &flag)?);
                }
            }
            wrap_nodelist(py, out, &flag)
        }
        "getElementsByTagName" => {
            let tag = str_arg(py, args, 0).to_lowercase();
            let mut out = Vec::new();
            for n in iter_elements(py, &node)? {
                let nb = n.bind(py);
                if nb.is(&node) {
                    continue;
                }
                if node_tag(nb) == tag {
                    out.push(wrap_element(py, nb, &flag)?);
                }
            }
            wrap_nodelist(py, out, &flag)
        }
        "matches" => {
            let sel = str_arg(py, args, 0);
            let matched = match parse_selector(&sel) {
                Some((tag, classes, ids)) => selector_matches(&node, &tag, &classes, &ids),
                None => false,
            };
            Ok(matched.into_py_any(py)?)
        }
        "closest" => {
            let sel = str_arg(py, args, 0);
            let spec = parse_selector(&sel);
            let mut cur = Some(node.clone().unbind());
            loop {
                let Some(c) = cur.take() else {
                    return undefined_py(py);
                };
                let cb = c.bind(py);
                let hit = match &spec {
                    Some((tag, classes, ids)) => selector_matches(cb, tag, classes, ids),
                    None => false,
                };
                if hit {
                    return wrap_element(py, cb, &flag);
                }
                match cb.getattr("parent") {
                    Ok(p) if !p.is_none() => cur = Some(p.unbind()),
                    _ => return undefined_py(py),
                }
            }
        }
        "contains" => {
            let Some(other) = args.first() else {
                return Ok(false.into_py_any(py)?);
            };
            if !is_jsdom_instance(py, other, "JSElement") {
                return Ok(false.into_py_any(py)?);
            }
            let other_node = other.bind(py).getattr("node")?;
            for n in iter_elements(py, &node)? {
                if n.is(&other_node) {
                    return Ok(true.into_py_any(py)?);
                }
            }
            Ok(false.into_py_any(py)?)
        }
        "remove" => {
            let parent = match node.getattr("parent") {
                Ok(p) if !p.is_none() => p,
                _ => return undefined_py(py),
            };
            let children = parent.getattr("children")?.cast_into::<PyList>()?;
            let _ = children.call_method1("remove", (node.clone(),));
            node.setattr("parent", py.None())?;
            mark_dirty(&flag);
            undefined_py(py)
        }
        "appendChild" => {
            if let Some(child) = args.first() {
                if is_jsdom_instance(py, child, "JSElement") {
                    let child_node = child.bind(py).getattr("node")?;
                    child_node.setattr("parent", &node)?;
                    let children = node.getattr("children")?.cast_into::<PyList>()?;
                    children.append(child_node)?;
                    mark_dirty(&flag);
                    return Ok(child.clone_ref(py));
                }
                if is_jsdom_instance(py, child, "JSFragment") {
                    // Move the fragment's children into the target and empty
                    // the fragment, like a real DocumentFragment append.
                    let items = child.bind(py).getattr("_items")?.cast_into::<PyList>()?;
                    let children = node.getattr("children")?.cast_into::<PyList>()?;
                    for item in items.iter() {
                        if let Ok(n) = item.getattr("node") {
                            n.setattr("parent", &node)?;
                            children.append(n)?;
                        }
                    }
                    let _ = items.call_method0("clear");
                    mark_dirty(&flag);
                    return Ok(child.clone_ref(py));
                }
            }
            undefined_py(py)
        }
        "removeChild" => {
            if let Some(child) = args.first() {
                if is_jsdom_instance(py, child, "JSElement") {
                    let child_node = child.bind(py).getattr("node")?;
                    let children = node.getattr("children")?.cast_into::<PyList>()?;
                    match children.call_method1("remove", (child_node.clone(),)) {
                        Ok(_) => {
                            child_node.setattr("parent", py.None())?;
                            mark_dirty(&flag);
                            return Ok(child.clone_ref(py));
                        }
                        Err(_) => return undefined_py(py),
                    }
                }
            }
            undefined_py(py)
        }
        "addEventListener" => {
            let event_type = str_arg(py, args, 0);
            let handlers = match node.getattr("_js_handlers") {
                Ok(h) => h.cast_into::<PyDict>()?,
                Err(_) => {
                    let d = PyDict::new(py);
                    node.setattr("_js_handlers", &d)?;
                    d
                }
            };
            let list = match handlers.get_item(&event_type)? {
                Some(l) => l.cast_into::<PyList>()?,
                None => {
                    let l = PyList::empty(py);
                    handlers.set_item(&event_type, &l)?;
                    l
                }
            };
            if let Some(fn_) = args.get(1) {
                list.append(fn_.clone_ref(py))?;
            }
            undefined_py(py)
        }
        _ => undefined_py(py),
    }
}

// -- node list / classList / style / fonts ---------------------------------

fn nodelist_get(py: Python<'_>, nl: &Bound<'_, PyAny>, name: &str) -> PyResult<Py<PyAny>> {
    let items = nl.getattr("_items")?.cast_into::<PyList>()?;
    if name == "length" {
        Ok((items.len() as u64).into_py_any(py)?)
    } else if name == "item" || name == "forEach" {
        method(py, "nodelist", nl, name)
    } else if let Some(idx) = int_index(name) {
        if idx >= 0 && (idx as usize) < items.len() {
            Ok(items.get_item(idx as usize)?.unbind())
        } else {
            undefined_py(py)
        }
    } else {
        undefined_py(py)
    }
}

fn nodelist_call(
    py: Python<'_>,
    nl: &Bound<'_, PyAny>,
    name: &str,
    args: &[Py<PyAny>],
) -> PyResult<Py<PyAny>> {
    let items = nl.getattr("_items")?.cast_into::<PyList>()?;
    match name {
        "item" => {
            let idx = arg_i64(py, args, 0);
            if idx >= 0 && (idx as usize) < items.len() {
                Ok(items.get_item(idx as usize)?.unbind())
            } else {
                undefined_py(py)
            }
        }
        "forEach" => {
            let Some(fn_) = args.first() else {
                return undefined_py(py);
            };
            let flag = nl.getattr("_flag")?;
            let interp = flag.cast::<PyDict>()?.get_item("interp")?;
            let interp = match interp {
                Some(i) if !i.is_none() => i,
                _ => return undefined_py(py),
            };
            let n = items.len();
            for (i, item) in items.iter().enumerate() {
                let args = PyTuple::new(py, vec![
                    fn_.clone_ref(py),
                    item.unbind(),
                    (i as u64).into_py_any(py)?,
                    nl.clone().unbind(),
                ])?;
                interp.call_method("call", args, None)?;
            }
            let _ = n;
            undefined_py(py)
        }
        _ => undefined_py(py),
    }
}

fn classlist_get(py: Python<'_>, cl: &Bound<'_, PyAny>, name: &str) -> PyResult<Py<PyAny>> {
    let node = cl.getattr("node")?;
    let classes = classes_set(py, &node)?;
    match name {
        "length" => Ok((classes.len() as u64).into_py_any(py)?),
        "add" | "remove" | "contains" | "toggle" => method(py, "classlist", cl, name),
        _ => {
            if let Some(idx) = int_index(name) {
                if idx >= 0 {
                    let items: Vec<&String> = classes.iter().collect();
                    if (idx as usize) < items.len() {
                        return Ok(items[idx as usize].clone().into_py_any(py)?);
                    }
                }
            }
            undefined_py(py)
        }
    }
}

fn classlist_call(
    py: Python<'_>,
    cl: &Bound<'_, PyAny>,
    name: &str,
    args: &[Py<PyAny>],
) -> PyResult<Py<PyAny>> {
    let node = cl.getattr("node")?;
    let flag = cl.getattr("_flag")?;
    let mut classes = classes_set(py, &node)?;
    match name {
        "add" => {
            for (i, _) in args.iter().enumerate() {
                classes.insert(str_arg(py, args, i));
            }
            save_classes(py, &node, &flag, &classes)?;
            undefined_py(py)
        }
        "remove" => {
            for (i, _) in args.iter().enumerate() {
                classes.remove(&str_arg(py, args, i));
            }
            save_classes(py, &node, &flag, &classes)?;
            undefined_py(py)
        }
        "contains" => {
            let cls = str_arg(py, args, 0);
            Ok(classes.contains(&cls).into_py_any(py)?)
        }
        "toggle" => {
            let cls = str_arg(py, args, 0);
            let present = classes.contains(&cls);
            // Match JSClassList._toggle: add iff `force is True` or
            // (`force` is missing/undefined and the class is absent).
            let add = match args.get(1) {
                Some(f) => {
                    let fb = f.bind(py);
                    if fb.extract::<bool>().unwrap_or(false) {
                        true
                    } else if is_undefined(py, fb) {
                        !present
                    } else {
                        false
                    }
                }
                None => !present,
            };
            if add {
                classes.insert(cls.clone());
            } else {
                classes.remove(&cls);
            }
            save_classes(py, &node, &flag, &classes)?;
            Ok(add.into_py_any(py)?)
        }
        _ => undefined_py(py),
    }
}

// -- style -----------------------------------------------------------------

fn style_get(py: Python<'_>, st: &Bound<'_, PyAny>, name: &str) -> PyResult<Py<PyAny>> {
    if matches!(name, "getPropertyValue" | "setProperty") {
        return method(py, "style", st, name);
    }
    let node = st.getattr("node")?;
    let kebab = camel_to_kebab(name);
    style_value(py, &node, &kebab)
}

fn style_set(
    py: Python<'_>,
    st: &Bound<'_, PyAny>,
    name: &str,
    value: &Bound<'_, PyAny>,
) -> PyResult<()> {
    let node = st.getattr("node")?;
    let flag = st.getattr("_flag")?;
    let kebab = camel_to_kebab(name);
    write_style(py, &node, &flag, &kebab, value)?;
    Ok(())
}

fn style_call(
    py: Python<'_>,
    st: &Bound<'_, PyAny>,
    name: &str,
    args: &[Py<PyAny>],
) -> PyResult<Py<PyAny>> {
    let node = st.getattr("node")?;
    let flag = st.getattr("_flag")?;
    match name {
        "getPropertyValue" => {
            let kebab = str_arg(py, args, 0);
            style_value(py, &node, &kebab)
        }
        "setProperty" => {
            let kebab = str_arg(py, args, 0);
            let value: Bound<'_, PyAny> = match args.get(1) {
                Some(a) => a.bind(py).clone(),
                None => String::new().into_py_any(py)?.bind(py).clone(),
            };
            write_style(py, &node, &flag, &kebab, &value)?;
            undefined_py(py)
        }
        _ => undefined_py(py),
    }
}

// -- fonts -----------------------------------------------------------------

fn fonts_get(py: Python<'_>, ff: &Bound<'_, PyAny>, name: &str) -> PyResult<Py<PyAny>> {
    match name {
        "add" | "load" | "check" | "forEach" => method(py, "fonts", ff, name),
        "ready" => {
            let interp = ff.getattr("_interp")?;
            if interp.is_none() {
                return undefined_py(py);
            }
            let promise = interp.call_method0("create_promise")?;
            Ok(promise.unbind())
        }
        _ => undefined_py(py),
    }
}

fn fonts_call(
    py: Python<'_>,
    ff: &Bound<'_, PyAny>,
    name: &str,
    args: &[Py<PyAny>],
) -> PyResult<Py<PyAny>> {
    match name {
        "add" => {
            let faces = ff.getattr("_faces")?.cast_into::<PyList>()?;
            if let Some(f) = args.first() {
                faces.append(f.clone_ref(py))?;
            }
            Ok(py.None())
        }
        "load" => undefined_py(py),
        "check" => Ok(true.into_py_any(py)?),
        "forEach" => Ok(py.None()),
        _ => undefined_py(py),
    }
}

// -- dispatch --------------------------------------------------------------

fn call_dispatch(
    py: Python<'_>,
    kind: &str,
    target: &Bound<'_, PyAny>,
    name: &str,
    args: &[Py<PyAny>],
) -> PyResult<Py<PyAny>> {
    let args: Vec<Py<PyAny>> = args.iter().map(|a| unwrap_host(py, a)).collect();
    match kind {
        "document" => document_call(py, target, name, &args),
        "element" => element_call(py, target, name, &args),
        "nodelist" => nodelist_call(py, target, name, &args),
        "classlist" => classlist_call(py, target, name, &args),
        "style" => style_call(py, target, name, &args),
        "fonts" => fonts_call(py, target, name, &args),
        _ => undefined_py(py),
    }
}

// -- callables returned to JS ----------------------------------------------

/// Callable returned by `js_get` for native DOM methods; dispatches back into
/// `dom_call` so the JS interpreter can call `el.getAttribute(...)` etc.
#[pyclass(module = "feetbrowser_engine", name = "_DomMethod", unsendable)]
struct DomMethod {
    kind: String,
    target: Py<PyAny>,
    name: String,
}

#[pymethods]
impl DomMethod {
    #[pyo3(signature = (*args))]
    fn __call__(
        &self,
        py: Python<'_>,
        args: Vec<Py<PyAny>>,
    ) -> PyResult<Py<PyAny>> {
        call_dispatch(py, &self.kind, self.target.bind(py), &self.name, &args)
    }
}

/// Callable used for `location.reload`/`assign`/`replace` (no-ops returning
/// None), matching the `lambda: None` in jsdom.py.
#[pyclass(module = "feetbrowser_engine", name = "_DomNoop", unsendable)]
struct Noop;

#[pymethods]
impl Noop {
    #[pyo3(signature = (*args))]
    fn __call__(
        &self,
        py: Python<'_>,
        args: Vec<Py<PyAny>>,
    ) -> PyResult<Py<PyAny>> {
        let _ = args;
        Ok(py.None())
    }
}

// -- public pyfunctions ----------------------------------------------------

#[pyfunction(signature = (kind, target, name))]
pub fn dom_get(
    py: Python<'_>,
    kind: &str,
    target: &Bound<'_, PyAny>,
    name: &str,
) -> PyResult<Py<PyAny>> {
    match kind {
        "document" => document_get(py, target, name),
        "element" => element_get(py, target, name),
        "nodelist" => nodelist_get(py, target, name),
        "classlist" => classlist_get(py, target, name),
        "style" => style_get(py, target, name),
        "fonts" => fonts_get(py, target, name),
        _ => undefined_py(py),
    }
}

#[pyfunction(signature = (kind, target, name, value))]
pub fn dom_set(
    py: Python<'_>,
    kind: &str,
    target: &Bound<'_, PyAny>,
    name: &str,
    value: &Bound<'_, PyAny>,
) -> PyResult<()> {
    match kind {
        "document" => document_set(py, target, name, value)?,
        "element" => element_set(py, target, name, value)?,
        "style" => style_set(py, target, name, value)?,
        _ => {}
    }
    Ok(())
}

#[pyfunction(signature = (kind, target, name, *args))]
pub fn dom_call(
    py: Python<'_>,
    kind: &str,
    target: &Bound<'_, PyAny>,
    name: &str,
    args: &Bound<'_, PyTuple>,
) -> PyResult<Py<PyAny>> {
    let arg_vec: Vec<Py<PyAny>> = args.iter().map(|a| a.unbind()).collect();
    call_dispatch(py, kind, target, name, &arg_vec)
}