//! The CSS cascade: selector matching and the style() tree walk.
//!
//! The parser stays in Python -- it runs once per stylesheet and reads like
//! the grammar it implements. What is here is the part that runs once per
//! node per rule: compiling the parser's selector objects into a shape that
//! can be matched without touching Python at all, bucketing the rules, and
//! walking the tree.
//!
//! That split was re-examined once the arena landed, on the theory that
//! leaving the parser in Python forces a marshalling step that costs more
//! than it saves. It does not: reading the parsed rules across the boundary
//! into `RuleIndex` is 60ms against 1040ms of parsing and 570ms of matching
//! over a corpus of real pages, and it is paid once per rules list rather
//! than once per pass, so the boundary is not what the parser costs. The
//! parser is simply slow in the ordinary way Python is slow, spread evenly
//! across `pair`, `selector` and `parse` with no hot spot left to move. A
//! port would buy the difference between Python and Rust on that work and
//! nothing structural, so the case for it is a performance case to be made
//! on its own, not a consequence of the DOM having moved.
//!
//! The document is mirrored into a flat arena first. That is the whole trick:
//! the Python cascade spent most of its time in attribute lookups on node
//! objects -- `node.parent`, `node.attributes`, `isinstance(node, Element)` --
//! repeated for every rule that might apply, and reading each of them once up
//! front turns the inner loop into integer indexing. Indices into that arena
//! are ours, not the page's, which is why they are indexed directly; every
//! value that did come from the page is a String we already own.
//!
//! The mirror is a copy of a tree that still lives in Python, and it will stop
//! being one: `footnote::domtree` is the arena the document is parsed into, and
//! the end state is that `Tree` below is deleted and the matcher indexes that
//! arena directly. It cannot be today, because the arena is materialised into
//! Python objects and dropped -- see `materialize.rs` for the measurement that
//! says why. What the mirror is *not* is
//! the cost of this module: it is measured at ~1.4% of a style pass across a
//! corpus of real pages, against 30-94% for the two things that were fixed
//! here instead, so removing it is a tidiness win and not a performance one.
//!
//! Everything a selector can ask about is copied, so the mirror is not a
//! correctness boundary either -- the matcher never wants a fact the mirror
//! lacks. The boundary that does bite is the other one: what the cascade calls
//! back into Python for, once per node. `var()` substitution used to be such a
//! call and is now `resolve_vars` below.

use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList, PyString};
use std::collections::HashMap;
use std::sync::{Arc, Mutex, OnceLock};

/// Specificity, as the parser computed it: (ids, classes, elements).
type Prio = (i64, i64, i64);

/// How deep a selector may nest before we stop compiling it.
///
/// `a b c d ...` nests one level per combinator, so a pathological stylesheet
/// could nest as deep as it likes. The Python this replaces hit RecursionError
/// somewhere past a thousand and took the page load with it; here the selector
/// simply stops matching, which is the same amount of styling lost and none of
/// the crash.
const MAX_SELECTOR_DEPTH: usize = 400;

// -- the compiled selector -------------------------------------------------

#[derive(Debug)]
enum Sel {
    Tag(String),
    Class(String),
    Id(String),
    Compound(Vec<Sel>),
    Descendant { ancestor: Box<Sel>, descendant: Box<Sel> },
    Child { parent: Box<Sel>, child: Box<Sel> },
    Sibling { before: Box<Sel>, after: Box<Sel>, adjacent: bool },
    Root,
    Attr { attr: String, op: Option<String>, value: String },
    Pseudo { name: String, arg: Option<String>, subs: Vec<Sel> },
    /// A selector we could not compile. Matches nothing.
    Never,
}

fn compile(obj: &Bound<'_, PyAny>, depth: usize) -> PyResult<Sel> {
    if depth > MAX_SELECTOR_DEPTH {
        return Ok(Sel::Never);
    }
    let kind: String = match obj.getattr("kind") {
        Ok(k) => k.extract()?,
        Err(_) => return Ok(Sel::Never),
    };
    let sel = match kind.as_str() {
        "tag" => Sel::Tag(obj.getattr("tag")?.extract()?),
        "class" => Sel::Class(obj.getattr("cls")?.extract()?),
        "id" => Sel::Id(obj.getattr("id")?.extract()?),
        "root" => Sel::Root,
        "compound" => {
            let mut parts = Vec::new();
            for part in obj.getattr("parts")?.try_iter()? {
                parts.push(compile(&part?, depth + 1)?);
            }
            Sel::Compound(parts)
        }
        "descendant" => Sel::Descendant {
            ancestor: Box::new(compile(&obj.getattr("ancestor")?, depth + 1)?),
            descendant: Box::new(compile(&obj.getattr("descendant")?, depth + 1)?),
        },
        "child" => Sel::Child {
            parent: Box::new(compile(&obj.getattr("parent")?, depth + 1)?),
            child: Box::new(compile(&obj.getattr("child")?, depth + 1)?),
        },
        "sibling" => Sel::Sibling {
            before: Box::new(compile(&obj.getattr("before")?, depth + 1)?),
            after: Box::new(compile(&obj.getattr("after")?, depth + 1)?),
            adjacent: obj.getattr("adjacent")?.is_truthy()?,
        },
        "attr" => {
            let op = obj.getattr("op")?;
            let value = obj.getattr("value")?;
            Sel::Attr {
                attr: obj.getattr("attr")?.extract()?,
                op: if op.is_none() { None } else { Some(op.extract()?) },
                value: if value.is_none() { String::new() } else { value.extract()? },
            }
        }
        "pseudo" => {
            let arg = obj.getattr("arg")?;
            let mut subs = Vec::new();
            let mut text = None;
            if arg.is_instance_of::<PyList>() {
                for sub in arg.try_iter()? {
                    subs.push(compile(&sub?, depth + 1)?);
                }
            } else if !arg.is_none() {
                text = Some(arg.extract()?);
            }
            Sel::Pseudo { name: obj.getattr("name")?.extract()?, arg: text, subs }
        }
        _ => Sel::Never,
    };
    Ok(sel)
}

// -- the rule index --------------------------------------------------------

/// The one feature a rule's terminal selector requires of a node.
///
/// Bucketing rules by it is what keeps a node from being asked about every
/// rule on the page: a text-heavy document has thousands of rules and a node
/// matches a handful.
#[derive(Debug, PartialEq, Eq, Hash, Clone)]
enum Hint {
    Any,
    Root,
    Tag(String),
    Class(String),
    Id(String),
}

fn hint_of(sel: &Sel) -> Hint {
    match sel {
        Sel::Tag(t) => {
            if t == "*" {
                Hint::Any
            } else {
                Hint::Tag(t.clone())
            }
        }
        Sel::Class(c) => Hint::Class(c.clone()),
        Sel::Id(i) => Hint::Id(i.clone()),
        Sel::Root => Hint::Root,
        Sel::Compound(parts) => {
            // From the end, so a trailing :nth-child or [attr] does not hide a
            // bucketing tag/class/id in front of it.
            for part in parts.iter().rev() {
                let h = hint_of(part);
                if h != Hint::Any {
                    return h;
                }
            }
            Hint::Any
        }
        // Every combinator constrains the node being matched through its
        // right-hand side, so that side's hint is the rule's hint. Without
        // these two arms `.menu > li` and `li + li` fell into `Any` and were
        // re-tested against every node in the document; a stylesheet written
        // in modern CSS is mostly combinators, so that was most of the sheet.
        Sel::Descendant { descendant, .. } => hint_of(descendant),
        Sel::Child { child, .. } => hint_of(child),
        Sel::Sibling { after, .. } => hint_of(after),
        _ => Hint::Any,
    }
}

struct Rule {
    prio: Prio,
    sel: Sel,
    /// The declarations this rule sets, already run through `_expand`.
    decls: Vec<(String, String)>,
}

struct RuleIndex {
    rules: Vec<Rule>,
    buckets: HashMap<Hint, Vec<usize>>,
}

impl RuleIndex {
    fn build(rules_obj: &Bound<'_, PyAny>, expanding: &Bound<'_, PyAny>,
             expand: &Bound<'_, PyAny>) -> PyResult<RuleIndex> {
        let mut rules: Vec<Rule> = Vec::new();
        let mut buckets: HashMap<Hint, Vec<usize>> = HashMap::new();
        for item in rules_obj.try_iter()? {
            let item = item?;
            let selector = item.get_item(0)?;
            let body = item.get_item(1)?;
            let sel = compile(&selector, 0)?;
            let prio: Prio = selector.getattr("priority")?.extract()?;
            let mut decls: Vec<(String, String)> = Vec::new();
            for pair in body.call_method0("items")?.try_iter()? {
                let pair = pair?;
                let prop: String = pair.get_item(0)?.extract()?;
                let value: String = pair.get_item(1)?.extract()?;
                // Only a shorthand expands, and Python owns the list of which
                // ones do; everything else is itself. Doing it here means the
                // cascade never calls back into Python for a declaration.
                if expanding.contains(&prop)? {
                    for out in expand.call1((&prop, &value))?.try_iter()? {
                        let out = out?;
                        decls.push((out.get_item(0)?.extract()?, out.get_item(1)?.extract()?));
                    }
                } else {
                    decls.push((prop, value));
                }
            }
            let index = rules.len();
            buckets.entry(hint_of(&sel)).or_default().push(index);
            rules.push(Rule { prio, sel, decls });
        }
        for bucket in buckets.values_mut() {
            bucket.sort_by_key(|&i| (rules[i].prio, i));
        }
        Ok(RuleIndex { rules, buckets })
    }
}

/// Compiled indexes, keyed by the identity of the rules list they came from.
///
/// A tab re-cascades the *same* rules list on every JS-driven DOM mutation,
/// and compiling the selectors is the expensive part of doing that. The
/// strong reference to the list is what makes keying on its address safe: no
/// newer list can be handed the address of one still in here.
static INDEX_CACHE: OnceLock<Mutex<Vec<(Py<PyAny>, Arc<RuleIndex>)>>> = OnceLock::new();
const INDEX_CACHE_MAX: usize = 32;

fn index_for(rules: &Bound<'_, PyAny>, expanding: &Bound<'_, PyAny>,
             expand: &Bound<'_, PyAny>) -> PyResult<Arc<RuleIndex>> {
    let cache = INDEX_CACHE.get_or_init(|| Mutex::new(Vec::new()));
    let key = rules.as_ptr() as usize;
    if let Ok(entries) = cache.lock() {
        for (obj, index) in entries.iter() {
            if obj.as_ptr() as usize == key {
                return Ok(Arc::clone(index));
            }
        }
    }
    let built = Arc::new(RuleIndex::build(rules, expanding, expand)?);
    if let Ok(mut entries) = cache.lock() {
        if entries.len() >= INDEX_CACHE_MAX {
            entries.clear();
        }
        entries.push((rules.clone().unbind(), Arc::clone(&built)));
    }
    Ok(built)
}

// -- the document, flattened ----------------------------------------------

struct Attr {
    text: String,
    /// Whether the attribute's value really was a string. An id selector
    /// compares the value itself, and in Python a non-string never equalled
    /// one; everything else stringifies it, which is what `text` holds.
    stringy: bool,
}

struct CNode {
    obj: Py<PyAny>,
    element: bool,
    tag: String,
    classes: Vec<String>,
    attrs: HashMap<String, Attr>,
    parent: Option<usize>,
    children: Vec<usize>,
    /// Whether this node is inside the subtree being styled. Nodes above the
    /// styling root are mirrored so ancestor selectors can walk into them, but
    /// they take no part in the ancestor feature sets, which is exactly how
    /// the Python behaved: it primed those sets from the styling root down.
    in_subtree: bool,
    /// Whether this node has been through the cascade in this pass. The
    /// ancestor fast path is only safe once it has.
    primed: bool,
    /// The node's computed style dict, once we have written it.
    style: Option<Py<PyDict>>,
    /// The `--custom` properties of that dict, kept in step with it.
    ///
    /// They are duplicated out of the dict because they are the one thing a
    /// *descendant* reads back: custom properties cannot go in the inherited
    /// table (it is a fixed list of known names), so their inheritance is a
    /// walk up the ancestors, once per var() reference. Reading that walk out
    /// of `PyDict`s meant a fresh Python string and a hash per step, several
    /// million times on a page built out of custom properties.
    customs: HashMap<String, String>,
}

impl CNode {
    fn attr(&self, name: &str) -> Option<&Attr> {
        self.attrs.get(name)
    }

    fn attr_text(&self, name: &str) -> Option<&str> {
        self.attrs.get(name).map(|a| a.text.as_str())
    }
}

struct Tree {
    nodes: Vec<CNode>,
    root: usize,
}

/// Which ancestor feature a quick-reject is asking about.
enum Feature<'a> {
    Tag(&'a str),
    Class(&'a str),
    Id(&'a str),
}

impl Tree {
    /// Mirror `node` and everything under it, plus the ancestors above it.
    fn build(node: &Bound<'_, PyAny>, element_cls: &Bound<'_, PyAny>) -> PyResult<Tree> {
        let mut nodes: Vec<CNode> = Vec::new();

        // The chain above the styling root first, so ancestor selectors can
        // walk out of the subtree the way they did when they walked `.parent`.
        let mut above: Vec<Bound<'_, PyAny>> = Vec::new();
        let mut cur = node.getattr("parent")?;
        while !cur.is_none() {
            above.push(cur.clone());
            cur = cur.getattr("parent")?;
            if above.len() > 10000 {
                // A parent chain this long is a cycle, not a document.
                break;
            }
        }
        let mut parent_idx: Option<usize> = None;
        for anc in above.iter().rev() {
            let idx = nodes.len();
            nodes.push(read_node(anc, element_cls, parent_idx, false)?);
            if let Some(p) = parent_idx {
                nodes[p].children.push(idx);
            }
            parent_idx = Some(idx);
        }

        let root = nodes.len();
        nodes.push(read_node(node, element_cls, parent_idx, true)?);
        if let Some(p) = parent_idx {
            nodes[p].children.push(root);
        }

        // Then the subtree, breadth by breadth. Iterative because a document
        // can nest deeper than any recursion limit is willing to.
        let mut queue = vec![root];
        while let Some(i) = queue.pop() {
            let obj = nodes[i].obj.clone_ref(node.py());
            let children = obj.bind(node.py()).getattr("children")?;
            for child in children.try_iter()? {
                let child = child?;
                let idx = nodes.len();
                nodes.push(read_node(&child, element_cls, Some(i), true)?);
                nodes[i].children.push(idx);
                queue.push(idx);
            }
        }
        Ok(Tree { nodes, root })
    }

    /// Does any ancestor inside the styled subtree carry this feature?
    ///
    /// The Python primed a frozenset of ancestor tags, classes and ids on each
    /// node as it walked. The set is exactly the ancestors between the node and
    /// the styling root, so walking them is the same answer without the sets.
    fn anc_has(&self, i: usize, feature: Feature) -> bool {
        let mut p = self.nodes[i].parent;
        while let Some(pi) = p {
            let a = &self.nodes[pi];
            if !a.in_subtree {
                break;
            }
            if a.element {
                match feature {
                    Feature::Tag(t) => {
                        if a.tag == t {
                            return true;
                        }
                    }
                    Feature::Class(c) => {
                        if a.classes.iter().any(|x| x == c) {
                            return true;
                        }
                    }
                    Feature::Id(id) => {
                        // An empty id was never added to the set.
                        if let Some(v) = a.attr_text("id") {
                            if !v.is_empty() && v == id {
                                return true;
                            }
                        }
                    }
                }
            }
            p = a.parent;
        }
        false
    }

    /// A cheap necessary condition for `sel` to match some ancestor. A true
    /// answer still needs the real walk; a false one saves it.
    fn ancestor_possible(&self, sel: &Sel, i: usize) -> bool {
        match sel {
            Sel::Tag(t) => t == "*" || self.anc_has(i, Feature::Tag(t)),
            Sel::Class(c) => self.anc_has(i, Feature::Class(c)),
            Sel::Id(id) => self.anc_has(i, Feature::Id(id)),
            Sel::Compound(parts) => {
                // Every part must be reachable, though not on one ancestor.
                parts.iter().all(|p| self.ancestor_possible(p, i))
            }
            Sel::Descendant { descendant, .. } => self.ancestor_possible(descendant, i),
            _ => true,
        }
    }

    fn matches(&self, sel: &Sel, i: usize) -> bool {
        let n = &self.nodes[i];
        match sel {
            Sel::Never => false,
            Sel::Tag(t) => n.element && (t == "*" || &n.tag == t),
            Sel::Class(c) => n.element && n.classes.iter().any(|x| x == c),
            Sel::Id(id) => {
                n.element
                    && match n.attr("id") {
                        Some(a) => a.stringy && a.text == *id,
                        None => false,
                    }
            }
            Sel::Root => n.element && n.parent.is_none(),
            Sel::Compound(parts) => parts.iter().all(|p| self.matches(p, i)),
            Sel::Descendant { ancestor, descendant } => {
                if !self.matches(descendant, i) {
                    return false;
                }
                if n.primed {
                    match &**ancestor {
                        Sel::Tag(t) if t != "*" => return self.anc_has(i, Feature::Tag(t)),
                        Sel::Class(c) => return self.anc_has(i, Feature::Class(c)),
                        Sel::Id(id) => return self.anc_has(i, Feature::Id(id)),
                        other => {
                            if !self.ancestor_possible(other, i) {
                                return false;
                            }
                        }
                    }
                }
                let mut p = n.parent;
                while let Some(pi) = p {
                    if self.matches(ancestor, pi) {
                        return true;
                    }
                    p = self.nodes[pi].parent;
                }
                false
            }
            Sel::Child { parent, child } => {
                if !self.matches(child, i) {
                    return false;
                }
                match n.parent {
                    Some(pi) => self.nodes[pi].element && self.matches(parent, pi),
                    None => false,
                }
            }
            Sel::Sibling { before, after, adjacent } => {
                if !self.matches(after, i) {
                    return false;
                }
                let pi = match n.parent {
                    Some(pi) if self.nodes[pi].element => pi,
                    _ => return false,
                };
                let mut earlier: Vec<usize> = Vec::new();
                for &c in &self.nodes[pi].children {
                    if c == i {
                        break;
                    }
                    if self.nodes[c].element {
                        earlier.push(c);
                    }
                }
                if *adjacent {
                    return match earlier.last() {
                        Some(&last) => self.matches(before, last),
                        None => false,
                    };
                }
                earlier.iter().any(|&s| self.matches(before, s))
            }
            Sel::Attr { attr, op, value } => {
                if !n.element {
                    return false;
                }
                let val = match n.attr(attr) {
                    Some(a) => &a.text,
                    None => return false,
                };
                let op = match op {
                    None => return true,
                    Some(op) => op.as_str(),
                };
                match op {
                    "=" => val == value,
                    "~=" => val.split_ascii_whitespace().any(|w| w == value),
                    "|=" => val == value || val.starts_with(&format!("{}-", value)),
                    "^=" => val.starts_with(value.as_str()),
                    "$=" => val.ends_with(value.as_str()),
                    "*=" => val.contains(value.as_str()),
                    _ => false,
                }
            }
            Sel::Pseudo { name, arg, subs } => self.matches_pseudo(name, arg, subs, i),
        }
    }

    fn matches_pseudo(&self, name: &str, arg: &Option<String>, subs: &[Sel],
                      i: usize) -> bool {
        let n = &self.nodes[i];
        match name {
            "not" => !subs.iter().any(|s| self.matches(s, i)),
            "is" | "where" => subs.iter().any(|s| self.matches(s, i)),
            "has" => self.has_match(i, subs),
            "first-child" | "last-child" | "only-child" | "nth-child"
            | "nth-last-child" | "first-of-type" | "last-of-type"
            | "only-of-type" | "nth-of-type" | "nth-last-of-type" => {
                self.structural(name, arg, i)
            }
            "empty" => n.element && n.children.is_empty(),
            // `:link` is "a hyperlink not yet visited", and an element is a
            // hyperlink when it is an a, area or link carrying href -- <area>
            // is how an image map's regions are addressed, and it was being
            // left out.
            "link" => {
                n.element
                    && matches!(n.tag.as_str(), "a" | "area" | "link")
                    && n.attr("href").is_some()
            }
            "checked" => {
                if !n.element {
                    return false;
                }
                match n.tag.as_str() {
                    "input" => n.attr("checked").is_some(),
                    // An <option>'s selectedness comes from `selected`; it has
                    // no `checked` attribute at all, so asking for one meant
                    // `option:checked` could never match anything.
                    "option" => n.attr("selected").is_some(),
                    _ => false,
                }
            }
            "disabled" | "enabled" | "required" => {
                if !n.element || !is_form_tag(&n.tag) {
                    return false;
                }
                match name {
                    "disabled" => n.attr("disabled").is_some(),
                    "enabled" => n.attr("disabled").is_none(),
                    _ => n.attr("required").is_some(),
                }
            }
            _ => false,
        }
    }

    /// `:has(sel)` -- true when any descendant matches. Iterative, because a
    /// page is allowed to nest deeper than the stack is.
    fn has_match(&self, i: usize, subs: &[Sel]) -> bool {
        if !self.nodes[i].element {
            return false;
        }
        let mut stack: Vec<usize> = self.nodes[i].children.iter().rev().copied().collect();
        while let Some(c) = stack.pop() {
            if subs.iter().any(|s| self.matches(s, c)) {
                return true;
            }
            if self.nodes[c].element {
                for &g in self.nodes[c].children.iter().rev() {
                    stack.push(g);
                }
            }
        }
        false
    }

    fn structural(&self, name: &str, arg: &Option<String>, i: usize) -> bool {
        let n = &self.nodes[i];
        if !n.element {
            return false;
        }
        let pi = match n.parent {
            Some(pi) => pi,
            None => return false,
        };
        let sibs: Vec<usize> = self.nodes[pi]
            .children
            .iter()
            .copied()
            .filter(|&c| self.nodes[c].element)
            .collect();
        let idx = match sibs.iter().position(|&c| c == i) {
            Some(idx) => idx as i64,
            None => return false,
        };
        let count = sibs.len() as i64;
        if name.contains("of-type") {
            let tsibs: Vec<usize> = sibs
                .into_iter()
                .filter(|&c| self.nodes[c].tag == n.tag)
                .collect();
            let tidx = match tsibs.iter().position(|&c| c == i) {
                Some(t) => t as i64,
                None => return false,
            };
            let tcount = tsibs.len() as i64;
            return match name {
                "first-of-type" => tidx == 0,
                "last-of-type" => tidx == tcount - 1,
                "only-of-type" => tcount == 1,
                "nth-of-type" => match_nth(arg, tidx + 1),
                _ => match_nth(arg, tcount - tidx),
            };
        }
        match name {
            "first-child" => idx == 0,
            "last-child" => idx == count - 1,
            "only-child" => count == 1,
            "nth-child" => match_nth(arg, idx + 1),
            _ => match_nth(arg, count - idx),
        }
    }
}

fn is_form_tag(tag: &str) -> bool {
    matches!(tag, "input" | "button" | "select" | "textarea" | "option" | "fieldset")
}

fn read_node(obj: &Bound<'_, PyAny>, element_cls: &Bound<'_, PyAny>,
             parent: Option<usize>, in_subtree: bool) -> PyResult<CNode> {
    let element = obj.is_instance(element_cls)?;
    let mut tag = String::new();
    let mut attrs: HashMap<String, Attr> = HashMap::new();
    let mut classes: Vec<String> = Vec::new();
    if element {
        tag = obj.getattr("tag")?.extract().unwrap_or_default();
        if let Ok(table) = obj.getattr("attributes") {
            if let Ok(items) = table.call_method0("items") {
                for pair in items.try_iter()? {
                    let pair = pair?;
                    let key: String = match pair.get_item(0)?.extract() {
                        Ok(k) => k,
                        Err(_) => continue,
                    };
                    let raw = pair.get_item(1)?;
                    if raw.is_none() {
                        // Indistinguishable from absent, which is what
                        // `attributes.get(name) is None` decided too.
                        continue;
                    }
                    match raw.extract::<String>() {
                        Ok(text) => {
                            attrs.insert(key, Attr { text, stringy: true });
                        }
                        Err(_) => {
                            let text = raw.str()?.extract()?;
                            attrs.insert(key, Attr { text, stringy: false });
                        }
                    }
                }
            }
        }
        // A class attribute that is not a string has no classes. The Python
        // raised AttributeError trying to split it, which cost the page load
        // rather than the rule.
        if let Some(a) = attrs.get("class") {
            if a.stringy {
                classes = a.text.split_ascii_whitespace().map(str::to_string).collect();
            }
        }
    }
    Ok(CNode {
        obj: obj.clone().unbind(),
        element,
        tag,
        classes,
        attrs,
        parent,
        children: Vec::new(),
        in_subtree,
        primed: false,
        style: None,
        customs: HashMap::new(),
    })
}

// -- :nth-child arithmetic -------------------------------------------------

/// Python's floor division, which rounds towards minus infinity.
fn floordiv(a: i64, b: i64) -> i64 {
    let q = a / b;
    if a % b != 0 && ((a < 0) != (b < 0)) {
        q - 1
    } else {
        q
    }
}

/// Parse an integer the way `int()` did: an optional sign and digits, and
/// nothing else.
fn parse_int(s: &str) -> Option<i64> {
    let s = s.trim();
    if s.is_empty() {
        return None;
    }
    s.parse::<i64>().ok()
}

/// Evaluate an `:nth-child()` expression against a 1-based element index.
fn match_nth(expr: &Option<String>, index: i64) -> bool {
    let expr = match expr {
        Some(e) => e.trim().to_lowercase(),
        None => return false,
    };
    if expr == "odd" {
        return index.rem_euclid(2) == 1;
    }
    if expr == "even" {
        return index.rem_euclid(2) == 0;
    }
    if expr.contains('n') {
        let (a, b) = match parse_an_plus_b(&expr) {
            Some(v) => v,
            None => return false,
        };
        let diff = index - b;
        if a == 0 {
            // `0n+3` is the third child and nothing else. The Python divided
            // by the step without checking it, so this expression raised
            // ZeroDivisionError out of the middle of the cascade and lost the
            // whole page rather than the rule.
            return diff == 0 && index >= 1;
        }
        if diff % a != 0 {
            return false;
        }
        let k = floordiv(diff, a);
        // `n` counts from zero, not one. Requiring `k >= 1` dropped the first
        // term of every `an+b`: `:nth-child(2n+1)` skipped the first child
        // rather than selecting it, `:nth-child(n+3)` started at the fourth,
        // and `:nth-child(-n+3)` -- whose whole range is k in 0..2 -- lost its
        // top element. `odd` and `even` are spelled out above and so were
        // right; only the arithmetic forms were wrong, which is why this hid.
        return k >= 0 && index >= 1;
    }
    match parse_int(&expr) {
        Some(v) => index == v,
        None => false,
    }
}

/// `^([+-]?\d*)n\s*(?:([+-])\s*(\d+))?$`, by hand.
fn parse_an_plus_b(expr: &str) -> Option<(i64, i64)> {
    let bytes = expr.as_bytes();
    let mut i = 0;
    let mut sign = 1i64;
    let mut seen_sign = false;
    if i < bytes.len() && (bytes[i] == b'+' || bytes[i] == b'-') {
        seen_sign = bytes[i] == b'-';
        sign = if seen_sign { -1 } else { 1 };
        seen_sign = true;
        i += 1;
    }
    let start = i;
    while i < bytes.len() && bytes[i].is_ascii_digit() {
        i += 1;
    }
    let digits = expr.get(start..i)?;
    let a = if digits.is_empty() {
        if seen_sign && sign < 0 {
            -1
        } else {
            1
        }
    } else {
        sign.checked_mul(digits.parse::<i64>().ok()?)?
    };
    if i >= bytes.len() || bytes[i] != b'n' {
        return None;
    }
    i += 1;
    while i < bytes.len() && (bytes[i] as char).is_whitespace() {
        i += 1;
    }
    if i >= bytes.len() {
        return Some((a, 0));
    }
    let bsign: i64 = match bytes[i] {
        b'+' => 1,
        b'-' => -1,
        _ => return None,
    };
    i += 1;
    while i < bytes.len() && (bytes[i] as char).is_whitespace() {
        i += 1;
    }
    let start = i;
    while i < bytes.len() && bytes[i].is_ascii_digit() {
        i += 1;
    }
    if i != bytes.len() || start == i {
        return None;
    }
    let b = expr.get(start..i)?.parse::<i64>().ok()?;
    Some((a, bsign.checked_mul(b)?))
}

// -- var() substitution ----------------------------------------------------

/// One `var(--name, fallback)` reference found in a declaration value.
///
/// Byte ranges into the value rather than copies of it: a page built out of
/// custom properties resolves a quarter of a million of these per style pass,
/// and three `String`s apiece was most of the work.
struct VarRef {
    /// The whole `var(...)`, which is the span the replacement stands in for.
    start: usize,
    end: usize,
    name: (usize, usize),
    /// The fallback as written, or `None` when the reference had no comma.
    fallback: Option<(usize, usize)>,
}

/// `var\(\s*(--[A-Za-z0-9_-]+)\s*(?:,\s*([^()]*))?\)`, by hand.
///
/// Scanned rather than matched with a regex for the same reason
/// `parse_an_plus_b` is: the pattern is small, and this runs once per
/// var()-bearing declaration per node, which on a page built out of custom
/// properties is the hottest loop in the cascade.
fn find_vars(value: &str) -> Vec<VarRef> {
    let bytes = value.as_bytes();
    let mut out = Vec::new();
    let mut at = 0usize;
    while let Some(rel) = value[at..].find("var(") {
        let start = at + rel;
        let mut i = start + 4;
        at = start + 4;
        // `\s*`
        while i < bytes.len() && value[i..].starts_with(char::is_whitespace) {
            i += value[i..].chars().next().map_or(1, char::len_utf8);
        }
        // `--[A-Za-z0-9_-]+`
        if !value[i..].starts_with("--") {
            continue;
        }
        let name_start = i;
        i += 2;
        let body_start = i;
        while i < bytes.len()
            && (bytes[i].is_ascii_alphanumeric() || bytes[i] == b'_' || bytes[i] == b'-')
        {
            i += 1;
        }
        if i == body_start {
            continue;
        }
        let name = (name_start, i);
        while i < bytes.len() && value[i..].starts_with(char::is_whitespace) {
            i += value[i..].chars().next().map_or(1, char::len_utf8);
        }
        if i >= bytes.len() {
            continue;
        }
        let fallback = if bytes[i] == b',' {
            i += 1;
            while i < bytes.len() && value[i..].starts_with(char::is_whitespace) {
                i += value[i..].chars().next().map_or(1, char::len_utf8);
            }
            // `[^()]*`, which cannot stretch over a nested `var(`; a nested
            // reference is resolved on an earlier pass of the fixed point,
            // and only then does the outer one match.
            let fstart = i;
            while i < bytes.len() && bytes[i] != b'(' && bytes[i] != b')' {
                i += 1;
            }
            if i >= bytes.len() || bytes[i] != b')' {
                continue;
            }
            Some((fstart, i))
        } else if bytes[i] == b')' {
            None
        } else {
            continue;
        };
        out.push(VarRef { start, end: i + 1, name, fallback });
        at = i + 1;
    }
    out
}

/// The value of a custom property, looked up from `i` outwards. Custom
/// properties are not in the inherited table -- they cannot be, it is a fixed
/// list of known names -- so their inheritance is this walk.
fn custom_property(py: Python<'_>, tree: &Tree, i: usize, name: &str)
                   -> PyResult<Option<String>> {
    let mut cur = Some(i);
    while let Some(ci) = cur {
        let node = &tree.nodes[ci];
        if node.element {
            if node.style.is_some() {
                // This pass wrote the node, so its customs are up to date.
                if let Some(v) = node.customs.get(name) {
                    return Ok(Some(v.clone()));
                }
            } else if let Ok(style) = node.obj.bind(py).getattr("style") {
                // Above the styling root this pass wrote nothing, so the only
                // answer is whatever Python last left on the node.
                if let Ok(v) = style.get_item(name) {
                    // A non-string custom property is not substitutable text.
                    if let Ok(text) = v.extract::<String>() {
                        return Ok(Some(text));
                    }
                }
            }
        }
        cur = node.parent;
    }
    Ok(None)
}

/// Substitute every `var()` in `value`, running to a fixed point so that a
/// nested fallback (`var(--a, var(--b, #fff))`) resolves too.
fn resolve_vars(py: Python<'_>, tree: &Tree, i: usize, value: &str)
                -> PyResult<String> {
    let mut value = value.to_string();
    for _ in 0..10 {
        let refs = find_vars(&value);
        if refs.is_empty() {
            break;
        }
        // Rebuilt left to right in one pass. The references are in source
        // order and none of them overlaps, so substituting each in place is
        // what replacing them one at a time converged on anyway -- and it
        // touches each byte of the value once instead of once per reference.
        let mut out = String::with_capacity(value.len());
        let mut last = 0usize;
        for reference in &refs {
            let name = &value[reference.name.0..reference.name.1];
            let replacement = custom_property(py, tree, i, name)?;
            out.push_str(&value[last..reference.start]);
            match replacement {
                Some(v) => out.push_str(&v),
                None => {
                    if let Some((a, b)) = reference.fallback {
                        out.push_str(value[a..b].trim());
                    }
                }
            }
            last = reference.end;
        }
        out.push_str(&value[last..]);
        if out == value {
            break;
        }
        value = out;
    }
    Ok(value)
}

// -- relative font sizes ---------------------------------------------------

/// `float()`, near enough: everything CSS can put in front of `%` or `em`.
fn py_float(s: &str) -> Option<f64> {
    s.trim().parse::<f64>().ok()
}

/// Format a float the way Python's f-string did: a trailing `.0` on a whole
/// number, because the value ends up in a `px` string layout has to parse.
fn repr_float(v: f64) -> String {
    let s = format!("{}", v);
    if s.contains('.') || s.contains('e') || s.contains("inf") || s.contains("nan") {
        s
    } else {
        format!("{}.0", s)
    }
}

/// Resolve a percent / em / rem / smaller / larger font size.
///
/// Everything here but `rem` is relative to the parent's resolved size;
/// `rem` is relative to the root element's, which is what `root_px` carries.
fn resolve_font_size(py: Python<'_>, tree: &Tree, i: usize, root_px: f64)
                     -> PyResult<()> {
    let style = match &tree.nodes[i].style {
        Some(d) => d.bind(py),
        None => return Ok(()),
    };
    let value: String = match style.get_item("font-size")? {
        Some(v) => v.extract()?,
        None => return Ok(()),
    };
    let mut parent_size = 16.0f64;
    if let Some(pi) = tree.nodes[i].parent {
        if let Some(ps) = parent_font_size(py, tree, pi)? {
            if let Some(stripped) = ps.strip_suffix("px") {
                if let Some(v) = py_float(stripped) {
                    parent_size = v;
                }
            }
        }
    }
    let resolved = if let Some(num) = value.strip_suffix('%') {
        py_float(num).map(|v| parent_size * v / 100.0)
    } else if let Some(num) = value.strip_suffix("rem") {
        // Before `em`, because `rem` ends in one. Falling through to the `em`
        // arm left `1.5r` in front of it, which parsed as nothing and took the
        // "size we cannot read" path -- so every `font-size: 2rem` heading
        // computed to its parent's size instead of twice the root's. That is
        // most headings on a page written this decade.
        py_float(num).map(|v| root_px * v)
    } else if let Some(num) = value.strip_suffix("em") {
        py_float(num).map(|v| parent_size * v)
    } else if value == "smaller" || value == "larger" {
        Some(parent_size * if value == "smaller" { 0.8 } else { 1.2 })
    } else {
        return Ok(());
    };
    match resolved {
        Some(v) => style.set_item("font-size", format!("{:.1}px", v))?,
        // A size we cannot read is the parent's, which is what inheriting it
        // would have given -- and is what `rem` gets, since it ends in `em`.
        None => style.set_item("font-size", format!("{}px", repr_float(parent_size)))?,
    }
    Ok(())
}

/// A node's resolved font size in px, once this pass has written its dict.
fn node_font_px(py: Python<'_>, tree: &Tree, i: usize) -> PyResult<Option<f64>> {
    let dict = match &tree.nodes[i].style {
        Some(d) => d.bind(py),
        None => return Ok(None),
    };
    let value: String = match dict.get_item("font-size")? {
        Some(v) => v.extract()?,
        None => return Ok(None),
    };
    Ok(value.strip_suffix("px").and_then(py_float))
}

/// The parent's font size, from the style dict this pass wrote or, for a node
/// above the styling root, from whatever Python left on it.
fn parent_font_size(py: Python<'_>, tree: &Tree, pi: usize) -> PyResult<Option<String>> {
    if let Some(d) = &tree.nodes[pi].style {
        return match d.bind(py).get_item("font-size")? {
            Some(v) => Ok(Some(v.extract()?)),
            None => Ok(None),
        };
    }
    let style = tree.nodes[pi].obj.bind(py).getattr("style")?;
    match style.get_item("font-size") {
        Ok(v) => Ok(Some(v.extract()?)),
        Err(_) => Ok(None),
    }
}

// -- the cascade -----------------------------------------------------------

/// Compute `.style` for a node and its subtree.
#[pyfunction]
pub fn style(py: Python<'_>, node: &Bound<'_, PyAny>, rules: &Bound<'_, PyAny>)
             -> PyResult<()> {
    // The policy the cascade needs -- what inherits, which shorthands expand,
    // how a var() resolves -- stays in Python, where it reads as a table
    // instead of as code. Only the loop over it is here.
    let module = py.import("feetbrowser.cssparser")?;
    let element_cls = py.import("feetbrowser.htmlparser")?.getattr("Element")?;
    let inherited = module.getattr("INHERITED_PROPERTIES")?;
    let expanding = module.getattr("EXPANDING_SHORTHANDS")?;
    let expand = module.getattr("_expand")?;
    let parse_inline = module.getattr("parse_inline")?;

    let mut defaults: Vec<(String, String)> = Vec::new();
    for pair in inherited.call_method0("items")?.try_iter()? {
        let pair = pair?;
        defaults.push((pair.get_item(0)?.extract()?, pair.get_item(1)?.extract()?));
    }

    let index = index_for(rules, &expanding, &expand)?;
    let mut tree = Tree::build(node, &element_cls)?;

    // What `rem` is relative to: the document root's font size. Node 0 is the
    // topmost node the mirror holds, so on a whole-document pass it is the
    // root and is styled first, and the value below is replaced with its
    // resolved size before any descendant asks. On a subtree restyle node 0 is
    // above the styling root and keeps whatever Python last left on it.
    let mut root_px = 16.0f64;
    if let Some(n) = tree.nodes.first() {
        if let Ok(style) = n.obj.bind(py).getattr("style") {
            if let Ok(Some(v)) = style.get_item("font-size").map(|v| Some(v)) {
                if let Ok(text) = v.extract::<String>() {
                    if let Some(px) = text.strip_suffix("px").and_then(py_float) {
                        root_px = px;
                    }
                }
            }
        }
    }

    // (node, the parent it inherits from). The styling root inherits from
    // nothing even when it has a parent, which is how a subtree restyle keeps
    // its defaults.
    let mut stack: Vec<(usize, Option<usize>)> = vec![(tree.root, None)];
    while let Some((i, parent)) = stack.pop() {
        let dict = PyDict::new(py);

        // 1. Inherited properties, or the defaults at the root.
        let parent_style = match parent {
            Some(pi) => tree.nodes[pi].style.as_ref().map(|d| d.bind(py).clone()),
            None => None,
        };
        for (prop, default) in &defaults {
            let fallback = || PyString::new(py, default).into_any();
            let value = match &parent_style {
                Some(ps) => match ps.get_item(prop.as_str())? {
                    Some(v) => v,
                    None => fallback(),
                },
                None => fallback(),
            };
            dict.set_item(prop.as_str(), value)?;
        }

        // 2. The rules that could match this node, in cascade order.
        let mut candidates: Vec<usize> = Vec::new();
        {
            let n = &tree.nodes[i];
            let mut hints: Vec<Hint> = vec![Hint::Any];
            if n.element {
                hints.push(Hint::Tag(n.tag.clone()));
                if let Some(a) = n.attr("id") {
                    if a.stringy {
                        hints.push(Hint::Id(a.text.clone()));
                    }
                }
                if n.parent.is_none() {
                    hints.push(Hint::Root);
                }
                for cls in &n.classes {
                    hints.push(Hint::Class(cls.clone()));
                }
            }
            for hint in &hints {
                if let Some(bucket) = index.buckets.get(hint) {
                    candidates.extend_from_slice(bucket);
                }
            }
        }
        candidates.sort_by_key(|&r| (index.rules[r].prio, r));

        tree.nodes[i].primed = true;
        let mut customs: HashMap<String, String> = HashMap::new();
        for &r in &candidates {
            let rule = &index.rules[r];
            if !tree.matches(&rule.sel, i) {
                continue;
            }
            for (prop, value) in &rule.decls {
                dict.set_item(prop.as_str(), value.as_str())?;
                if prop.starts_with("--") {
                    customs.insert(prop.clone(), value.clone());
                }
            }
        }

        // 3. The inline style attribute, which outranks all of them.
        let inline = tree.nodes[i]
            .attr("style")
            .filter(|_| tree.nodes[i].element)
            .map(|a| a.text.clone());
        if let Some(text) = inline {
            let parsed = parse_inline.call1((text,))?;
            for pair in parsed.call_method0("items")?.try_iter()? {
                let pair = pair?;
                let prop: String = pair.get_item(0)?.extract()?;
                let value = pair.get_item(1)?;
                if expanding.contains(&prop)? {
                    for out in expand.call1((&prop, &value))?.try_iter()? {
                        let out = out?;
                        dict.set_item(out.get_item(0)?, out.get_item(1)?)?;
                    }
                } else {
                    if prop.starts_with("--") {
                        if let Ok(text) = value.extract::<String>() {
                            customs.insert(prop.clone(), text);
                        }
                    }
                    dict.set_item(prop, value)?;
                }
            }
        }

        let node_obj = tree.nodes[i].obj.bind(py).clone();
        node_obj.setattr("style", &dict)?;
        tree.nodes[i].style = Some(dict.clone().unbind());
        tree.nodes[i].customs = customs;

        // 3b. var(--x) references, which read custom properties off the
        //     ancestors -- so they need the dicts above to be in place, and
        //     they are: this walk is depth-first and top down.
        //     Scanned through `PyString::to_str`, which borrows, so that the
        //     properties that have no var() in them -- almost all of them --
        //     cost a look rather than a copy.
        let mut pending: Vec<(String, String)> = Vec::new();
        for (k, v) in dict.iter() {
            let (key, value) = match (k.cast::<PyString>(), v.cast::<PyString>()) {
                (Ok(k), Ok(v)) => (k, v),
                _ => continue,
            };
            if let (Ok(key), Ok(value)) = (key.to_str(), value.to_str()) {
                if value.contains("var(") {
                    pending.push((key.to_string(), value.to_string()));
                }
            }
        }
        for (prop, value) in pending {
            let resolved = resolve_vars(py, &tree, i, &value)?;
            // Kept in step with the dict, so that a later reference on this
            // node -- or on a descendant -- reads exactly what a lookup
            // through the dict would have read at this point in the walk.
            if prop.starts_with("--") {
                tree.nodes[i].customs.insert(prop.clone(), resolved.clone());
            }
            dict.set_item(prop, resolved)?;
        }

        // 4. Relative font sizes, against the parent's resolved one.
        resolve_font_size(py, &tree, i, root_px)?;
        if i == 0 {
            if let Some(px) = node_font_px(py, &tree, 0)? {
                root_px = px;
            }
        }

        let children: Vec<usize> = tree.nodes[i].children.clone();
        for &c in children.iter().rev() {
            stack.push((c, Some(i)));
        }
    }
    Ok(())
}

// -- tests -----------------------------------------------------------------
//
// What can be tested without a Python interpreter: the two hand-written
// scanners and the bucketing hint. Everything else here needs a document and
// a rules list, and is covered from `tests/test_units.py`.

#[cfg(test)]
mod tests {
    use super::*;

    /// `(whole, name, fallback)` per reference, for readable assertions.
    fn refs(value: &str) -> Vec<(&str, &str, Option<&str>)> {
        find_vars(value)
            .into_iter()
            .map(|r| {
                (
                    &value[r.start..r.end],
                    &value[r.name.0..r.name.1],
                    r.fallback.map(|(a, b)| &value[a..b]),
                )
            })
            .collect()
    }

    #[test]
    fn a_bare_var_reference_is_found() {
        assert_eq!(refs("var(--a)"), vec![("var(--a)", "--a", None)]);
    }

    #[test]
    fn a_reference_is_found_in_the_middle_of_a_value() {
        assert_eq!(
            refs("1px solid var(--edge) inset"),
            vec![("var(--edge)", "--edge", None)]
        );
    }

    #[test]
    fn whitespace_inside_the_parentheses_is_allowed() {
        assert_eq!(refs("var(  --a  )"), vec![("var(  --a  )", "--a", None)]);
    }

    #[test]
    fn a_fallback_is_captured_as_written() {
        assert_eq!(
            refs("var(--a,  navy )"),
            vec![("var(--a,  navy )", "--a", Some("navy "))]
        );
    }

    #[test]
    fn every_reference_in_a_value_is_found_in_order() {
        assert_eq!(
            refs("var(--a) var(--b)"),
            vec![("var(--a)", "--a", None), ("var(--b)", "--b", None)]
        );
    }

    #[test]
    fn the_same_reference_twice_is_found_twice() {
        assert_eq!(refs("var(--a) var(--a)").len(), 2);
    }

    #[test]
    fn a_name_without_the_double_dash_is_not_a_custom_property() {
        assert!(refs("var(a)").is_empty());
        assert!(refs("var(-a)").is_empty());
        assert!(refs("var(--)").is_empty());
    }

    #[test]
    fn an_unclosed_reference_is_not_a_reference() {
        assert!(refs("var(--a").is_empty());
        assert!(refs("var(--a, red").is_empty());
    }

    #[test]
    fn a_fallback_may_not_contain_parentheses() {
        // The outer reference cannot match while the inner one is still
        // there; the inner one is found instead, and the fixed point in
        // resolve_vars brings the outer one into range on the next pass.
        assert_eq!(refs("var(--a, var(--b))"), vec![("var(--b)", "--b", None)]);
    }

    #[test]
    fn a_reference_inside_calc_is_found() {
        assert_eq!(refs("calc(var(--w) * 2)"), vec![("var(--w)", "--w", None)]);
    }

    #[test]
    fn names_may_contain_digits_dashes_and_underscores() {
        assert_eq!(refs("var(--a-b_2)"), vec![("var(--a-b_2)", "--a-b_2", None)]);
    }

    #[test]
    fn an_empty_fallback_is_not_the_same_as_no_fallback() {
        assert_eq!(refs("var(--a,)"), vec![("var(--a,)", "--a", Some(""))]);
        assert_eq!(refs("var(--a)"), vec![("var(--a)", "--a", None)]);
    }

    #[test]
    fn nth_expressions_select_the_right_indices() {
        // Selected indices out of the first six children, so that the n=0 term
        // of each expression is inside the window rather than off its edge --
        // that term is what `k >= 1` used to discard.
        let hits = |e: &str| -> Vec<i64> {
            (1..=6)
                .filter(|i| match_nth(&Some(e.to_string()), *i))
                .collect()
        };
        assert_eq!(hits("odd"), vec![1, 3, 5]);
        assert_eq!(hits("even"), vec![2, 4, 6]);
        assert_eq!(hits("2n"), vec![2, 4, 6]);
        assert_eq!(hits("2n+1"), vec![1, 3, 5]);
        assert_eq!(hits("3n+1"), vec![1, 4]);
        assert_eq!(hits("n"), vec![1, 2, 3, 4, 5, 6]);
        assert_eq!(hits("n+3"), vec![3, 4, 5, 6]);
        assert_eq!(hits("-n+3"), vec![1, 2, 3]);
        assert_eq!(hits("-2n+5"), vec![1, 3, 5]);
        // `0n+3` is the bare index in disguise; both must avoid dividing by
        // the step, and neither may select anything else.
        assert_eq!(hits("0n+3"), vec![3]);
        assert_eq!(hits("3"), vec![3]);
        // Nothing before the first child, whatever the offset asks for.
        assert!(hits("n+9").is_empty());
        assert!(hits("-n").is_empty());
        assert!(!match_nth(&Some("2n+1".into()), 0));
        assert!(!match_nth(&None, 1));
    }

    #[test]
    fn a_zero_step_nth_is_one_index_and_does_not_divide_by_zero() {
        assert!(match_nth(&Some("0n+3".to_string()), 3));
        assert!(!match_nth(&Some("0n+3".to_string()), 6));
    }

    #[test]
    fn a_combinator_takes_its_hint_from_the_right_hand_side() {
        let child = Sel::Child {
            parent: Box::new(Sel::Class("menu".into())),
            child: Box::new(Sel::Tag("li".into())),
        };
        assert_eq!(hint_of(&child), Hint::Tag("li".into()));

        let sibling = Sel::Sibling {
            before: Box::new(Sel::Tag("li".into())),
            after: Box::new(Sel::Class("mark".into())),
            adjacent: true,
        };
        assert_eq!(hint_of(&sibling), Hint::Class("mark".into()));
    }

    #[test]
    fn a_compound_hint_comes_from_its_last_bucketable_part() {
        let compound = Sel::Compound(vec![
            Sel::Tag("a".into()),
            Sel::Class("btn".into()),
            Sel::Pseudo { name: "first-child".into(), arg: None, subs: vec![] },
        ]);
        assert_eq!(hint_of(&compound), Hint::Class("btn".into()));
    }

    #[test]
    fn a_selector_with_nothing_to_bucket_on_falls_back_to_any() {
        assert_eq!(hint_of(&Sel::Tag("*".into())), Hint::Any);
        assert_eq!(
            hint_of(&Sel::Attr { attr: "href".into(), op: None, value: String::new() }),
            Hint::Any
        );
    }
}
