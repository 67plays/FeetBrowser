//! Turn a parsed arena tree into the Python node objects the browser reads.
//!
//! # Why this exists rather than a pyo3 facade over the arena
//!
//! The obvious move, once `footnote::parse` produces a real tree, is to expose the
//! arena to Python behind a `#[pyclass]` that quacks like
//! `feetbrowser.htmlparser.Element` and let the arena be the live document.
//! Two measured facts say otherwise.
//!
//! * The Python side of this browser reads DOM nodes about **1.2 million
//!   times per style+layout pass** on a real page (en.wikipedia.org/wiki/HTML,
//!   15 292 nodes). Those reads are free today: `node.style`, `node.tag`,
//!   `node.children` are plain attribute loads on plain Python objects. Behind
//!   a pyclass every one of them becomes a Python->Rust crossing.
//! * The JavaScript engine reads DOM nodes **7 times** on that same page.
//!
//! Moving the live document into the arena therefore removes 7 crossings and
//! creates on the order of a million. The direction of the trade is wrong by
//! about five orders of magnitude, and it does not flip until a page's scripts
//! do tens of thousands of DOM operations per load, which real pages do not do
//! at load time.
//!
//! There is a second, harder blocker. `layout.py` and `browser.py` mutate
//! `node.attributes` and `node.children` **in place** in roughly fifty places
//! (`node.attributes["value"] = v`, `del node.attributes["selected"]`,
//! `children.append(...)`), and `rust/src/dom.rs` and `rust/src/css.rs` both
//! require them to be a real `dict` and a real `list` (`cast_into::<PyDict>`,
//! `cast_into::<PyList>`). A pyclass cannot observe an in-place mutation of a
//! dict it handed out, so the arena would stop being authoritative the first
//! time a form field was edited. Keeping both in sync is two sources of truth,
//! which is the bug the arena was meant to remove.
//!
//! So the arena's win is taken where it is unambiguous — **parsing** — and the
//! document stays where every one of its consumers already is. This module is
//! the one-shot handoff: parse in Rust with the spec-conformant tree builder,
//! materialise once, and hand back the same shapes `htmlparser.py` produced.
//!
//! # What is dropped
//!
//! Comments, doctypes and processing instructions have no class in
//! `htmlparser.py` and every Python consumer type-tests with
//! `isinstance(n, Element)` / `isinstance(n, Text)`. Materialising them as a
//! third kind would make every one of those tests fall through to a silent
//! else-branch. They are skipped, exactly as the old parser skipped them.

use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList};

use footnote::domtree::{Dom, NodeData, NodeId};

/// The `Element` and `Text` classes, looked up once per call.
struct Classes<'py> {
    element: Bound<'py, PyAny>,
    text: Bound<'py, PyAny>,
}

impl<'py> Classes<'py> {
    fn load(py: Python<'py>) -> PyResult<Classes<'py>> {
        let m = py.import("feetbrowser.htmlparser")?;
        Ok(Classes {
            element: m.getattr("Element")?,
            text: m.getattr("Text")?,
        })
    }
}

/// Build the Python object for `id` and, recursively, its children.
///
/// Returns `None` for node kinds the Python tree has no class for.
fn build<'py>(
    py: Python<'py>,
    dom: &Dom,
    id: NodeId,
    parent: &Bound<'py, PyAny>,
    cls: &Classes<'py>,
) -> PyResult<Option<Bound<'py, PyAny>>> {
    let Ok(data) = dom.data(id) else {
        return Ok(None);
    };
    match data {
        NodeData::Text(t) => {
            let node = cls.text.call1((t.as_str(), parent))?;
            Ok(Some(node))
        }
        NodeData::Element(e) => {
            // `htmlparser.py` keyed attributes by lowercase name and kept the
            // first of a duplicate pair. The tokenizer has already applied
            // both rules, but a namespaced attribute (`xlink:href`) has to be
            // rebuilt into the qualified form Python selectors expect.
            let attrs = PyDict::new(py);
            for a in &e.attrs {
                let key = match &a.prefix {
                    Some(_) => a.qualified_name(),
                    None => a.local.clone(),
                };
                if !attrs.contains(&key)? {
                    attrs.set_item(key, a.value.as_str())?;
                }
            }
            let node = cls.element.call1((e.name.as_str(), attrs, parent))?;
            let children = node.getattr("children")?;
            let children = children.cast_into::<PyList>()?;
            append_children(py, dom, id, &node, &children, cls)?;
            Ok(Some(node))
        }
        NodeData::Fragment => Ok(None),
        NodeData::Document | NodeData::Doctype(_) => Ok(None),
        NodeData::Comment(_) | NodeData::ProcessingInstruction(_) => Ok(None),
    }
}

/// Append the materialised children of `id` to `children`.
///
/// A `<template>` keeps its contents in a `Fragment` child rather than as
/// direct children. Python has no template semantics and no fragment class, so
/// the fragment is transparent here: its children are spliced into the
/// template element. Dropping it instead would lose the whole subtree, which
/// is what a page that ships its markup inside `<template>` is made of.
fn append_children<'py>(
    py: Python<'py>,
    dom: &Dom,
    id: NodeId,
    parent: &Bound<'py, PyAny>,
    children: &Bound<'py, PyList>,
    cls: &Classes<'py>,
) -> PyResult<()> {
    for child in dom.children(id) {
        if matches!(dom.data(child), Ok(NodeData::Fragment)) {
            append_children(py, dom, child, parent, children, cls)?;
            continue;
        }
        if let Some(obj) = build(py, dom, child, parent, cls)? {
            children.append(obj)?;
        }
    }
    Ok(())
}

/// The `<html>` element under a parsed document, which is the root every
/// Python consumer expects. The tree builder always produces one.
fn html_root(dom: &Dom, document: NodeId) -> Option<NodeId> {
    dom.children(document)
        .find(|&c| matches!(dom.data(c), Ok(NodeData::Element(_))))
}

/// Parse `source` and return the Python `Element` tree for it.
///
/// This is the browser's parser. It replaces `feetbrowser.htmlparser.HTMLParser`,
/// whose regex-and-a-stack design scored well below this one on the html5lib
/// tree-construction suite and had no path to the algorithms (foster
/// parenting, formatting reconstruction, the adoption agency) that the
/// remaining cases need.
#[pyfunction]
#[pyo3(signature = (source, scripting = false))]
pub fn parse_html(py: Python<'_>, source: &str, scripting: bool) -> PyResult<Py<PyAny>> {
    let (dom, document) = footnote::treebuilder::parse_document(source, scripting);
    let cls = Classes::load(py)?;

    let root = match html_root(&dom, document) {
        Some(r) => r,
        // Unreachable for the real tree builder, which always emits <html>,
        // but a caller that hands us an empty arena should get an empty
        // document rather than a panic.
        None => {
            let attrs = PyDict::new(py);
            return Ok(cls
                .element
                .call1(("html", attrs, py.None()))?
                .unbind());
        }
    };

    let none = py.None().into_bound(py);
    match build(py, &dom, root, &none, &cls)? {
        Some(obj) => Ok(obj.unbind()),
        None => {
            let attrs = PyDict::new(py);
            Ok(cls.element.call1(("html", attrs, py.None()))?.unbind())
        }
    }
}

/// Parse `source` as the contents of a `context` element and return the
/// resulting top-level nodes as a Python list.
///
/// This is the engine behind `innerHTML`. Parsing in the context of the
/// element being assigned to is what makes `row.innerHTML = "<td>x"` produce a
/// cell instead of dropping it: the fragment algorithm primes the tree
/// builder's insertion mode from the context element, which a context-free
/// parser cannot do.
pub(crate) fn fragment_children<'py>(
    py: Python<'py>,
    source: &str,
    context: &str,
) -> PyResult<Bound<'py, PyList>> {
    let (dom, fragment) = footnote::parse_fragment_html(source, context);
    let cls = Classes::load(py)?;
    let none = py.None().into_bound(py);
    let out = PyList::empty(py);
    append_children(py, &dom, fragment, &none, &out, &cls)?;
    Ok(out)
}

#[pyfunction]
#[pyo3(signature = (source, context = "body"))]
pub fn parse_fragment_html(py: Python<'_>, source: &str, context: &str) -> PyResult<Py<PyAny>> {
    Ok(fragment_children(py, source, context)?.unbind().into())
}
