//! An arena-backed DOM tree, owned entirely by Rust.
//!
//! This module is the replacement for the FFI proxy in `dom.rs`. Nothing in
//! here touches Python: the tree is a plain `Vec` of nodes addressed by a
//! `NodeId(u32)` index, with parent/child/sibling links stored as indices.
//!
//! # Why an arena
//!
//! * The DOM is inherently cyclic (parent <-> child). `Rc<RefCell<Node>>`
//!   models that badly: every parent link has to be a `Weak`, every access
//!   costs a runtime borrow check, and a mistake leaks the whole subtree.
//! * Indices are compact and cache-friendly; a whole document is one
//!   allocation that the CPU prefetcher can walk.
//! * A `NodeId` is a bare `u32`. It can cross an FFI boundary unchanged,
//!   which an `Rc` fundamentally cannot.
//!
//! # Slot lifetime
//!
//! Slots are **never reused**. `remove` only detaches (the subtree stays
//! intact and reinsertable); `destroy_subtree` drops a node's payload and
//! leaves a tombstone behind. That costs index space in exchange for a
//! guarantee that matters more: a `NodeId` is either valid or permanently
//! dead, never silently recycled into a different node. Without that, a stale
//! handle held by JS (or by a future FFI consumer) would read as a live node
//! of the wrong identity. Documents are dropped wholesale on navigation, so
//! the arena's high-water mark is bounded by nodes-per-document, not by
//! process lifetime.
//!
//! # Layering
//!
//! This module knows about tree shape and nothing else. It has no opinion on
//! HTML parsing rules, no element-specific behaviour, and no notion of the
//! stack of open elements or the list of active formatting elements. Those
//! belong to the tree builder (Phase 2), which drives this API.

// Phase 1 lands the data structure; Phases 2-6 are its callers. Until the
// tree builder and the interpreter are rewired onto it, most of the surface
// below has no in-crate caller and would otherwise drown the build in
// dead-code warnings.
#![allow(dead_code)]

use std::fmt;

// ---------------------------------------------------------------------------
// Identifiers
// ---------------------------------------------------------------------------

/// A handle to a node in a [`Dom`] arena.
///
/// Deliberately a bare `u32` newtype: cheap to copy, cheap to hash, and
/// representable across an FFI boundary as a plain integer.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub struct NodeId(pub u32);

impl NodeId {
    #[inline]
    fn index(self) -> usize {
        self.0 as usize
    }

    /// The raw integer, for handing across an FFI boundary.
    #[inline]
    pub fn to_raw(self) -> u32 {
        self.0
    }

    /// Rebuild a `NodeId` from a raw integer received across FFI.
    ///
    /// The result is not trusted: every arena accessor validates that the
    /// index is in range and alive before dereferencing it.
    #[inline]
    pub fn from_raw(raw: u32) -> NodeId {
        NodeId(raw)
    }
}

impl fmt::Display for NodeId {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "#{}", self.0)
    }
}

// ---------------------------------------------------------------------------
// Namespaces
// ---------------------------------------------------------------------------

/// The namespaces the tree can represent.
///
/// SVG and MathML content is not *rendered* by this browser, but the tree
/// builder's foreign-content rules ("if the adjusted current node is not in
/// the HTML namespace, ...") are load-bearing even for HTML-only pages: they
/// decide when the tokenizer switches modes and when self-closing tags are
/// honoured. The field has to exist for those rules to be expressible.
///
/// `XLink`, `Xml` and `XmlNs` never appear on elements; they exist because
/// "adjust foreign attributes" assigns them to attributes such as
/// `xlink:href` and `xml:lang`.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum Namespace {
    Html,
    Svg,
    MathMl,
    XLink,
    Xml,
    XmlNs,
}

impl Namespace {
    /// The namespace URI, as `namespaceURI` will need to report it.
    pub fn url(self) -> &'static str {
        match self {
            Namespace::Html => "http://www.w3.org/1999/xhtml",
            Namespace::Svg => "http://www.w3.org/2000/svg",
            Namespace::MathMl => "http://www.w3.org/1998/Math/MathML",
            Namespace::XLink => "http://www.w3.org/1999/xlink",
            Namespace::Xml => "http://www.w3.org/XML/1998/namespace",
            Namespace::XmlNs => "http://www.w3.org/2000/xmlns/",
        }
    }

    #[inline]
    pub fn is_html(self) -> bool {
        matches!(self, Namespace::Html)
    }
}

// ---------------------------------------------------------------------------
// Node payloads
// ---------------------------------------------------------------------------

/// A single attribute.
///
/// Attributes are stored in an ordered `Vec` rather than a map because the
/// order is observable: `element.attributes` is an index-addressable
/// `NamedNodeMap` in source order, and the HTML serializer emits them in that
/// order too. Elements carry a handful of attributes in practice, so linear
/// scan beats hashing.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Attr {
    /// Namespace, for the handful of namespaced attributes that "adjust
    /// foreign attributes" produces. `None` for every ordinary attribute.
    pub namespace: Option<Namespace>,
    /// Prefix as authored, e.g. `Some("xlink")` for `xlink:href`.
    pub prefix: Option<String>,
    /// Local name, e.g. `href`.
    pub local: String,
    pub value: String,
}

impl Attr {
    /// A plain, un-namespaced attribute.
    pub fn new(local: impl Into<String>, value: impl Into<String>) -> Attr {
        Attr {
            namespace: None,
            prefix: None,
            local: local.into(),
            value: value.into(),
        }
    }

    /// A namespaced attribute, as produced by "adjust foreign attributes".
    pub fn namespaced(
        namespace: Namespace,
        prefix: impl Into<String>,
        local: impl Into<String>,
        value: impl Into<String>,
    ) -> Attr {
        Attr {
            namespace: Some(namespace),
            prefix: Some(prefix.into()),
            local: local.into(),
            value: value.into(),
        }
    }

    /// `prefix:local`, or just `local` when there is no prefix.
    pub fn qualified_name(&self) -> String {
        match &self.prefix {
            Some(p) => format!("{}:{}", p, self.local),
            None => self.local.clone(),
        }
    }

    /// Does this attribute answer to `query`?
    ///
    /// `ci` selects ASCII-case-insensitive matching, which the caller sets
    /// for HTML-namespace elements.
    fn matches(&self, query: &str, ci: bool) -> bool {
        match &self.prefix {
            None => str_eq(&self.local, query, ci),
            Some(prefix) => match query.split_once(':') {
                Some((qp, ql)) => str_eq(prefix, qp, ci) && str_eq(&self.local, ql, ci),
                None => false,
            },
        }
    }
}

#[inline]
fn str_eq(a: &str, b: &str, ci: bool) -> bool {
    if ci {
        a.eq_ignore_ascii_case(b)
    } else {
        a == b
    }
}

/// An element's payload.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ElementData {
    pub namespace: Namespace,
    /// Local name. The tree builder lowercases HTML tag names before it gets
    /// here; foreign elements keep their authored case (`clipPath`).
    pub name: String,
    /// Ordered, because the order is observable.
    pub attrs: Vec<Attr>,
}

/// A doctype's payload.
#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct DoctypeData {
    pub name: String,
    pub public_id: String,
    pub system_id: String,
}

/// What a node actually is.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum NodeData {
    Document,
    /// A `DocumentFragment`. Carries no data of its own; it exists to be a
    /// root that is not a document.
    ///
    /// Added in Phase 2, which needs two of them: the scratch root the HTML
    /// fragment parsing algorithm parses into, and a `<template>` element's
    /// `content`. Unlike a `Document`, a fragment *is* insertable as a child
    /// — the template's contents fragment is stored as the template element's
    /// only child, which is how the tree builder reaches it and how the
    /// html5lib test format renders it (as a `content` line).
    Fragment,
    Doctype(DoctypeData),
    Element(ElementData),
    Text(String),
    Comment(String),
    /// A `ProcessingInstruction`, e.g. `<?module-handler data>`.
    ///
    /// Long-standing HTML behaviour turns `<?...>` into a comment; a 2026
    /// revision of §13.2.5 gives well-formed ones a node of their own, which
    /// is what the html5lib fixtures now expect. Placement is identical to a
    /// comment's in every insertion mode.
    ProcessingInstruction(PiData),
}

/// A processing instruction's payload.
#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct PiData {
    /// The name immediately after `<?`, e.g. `module-handler`.
    pub target: String,
    /// Everything between the target and the closing `?>` or `>`.
    pub data: String,
}

impl NodeData {
    /// Can this kind of node hold children? Only documents, fragments and
    /// elements can.
    pub fn can_have_children(&self) -> bool {
        matches!(
            self,
            NodeData::Document | NodeData::Fragment | NodeData::Element(_)
        )
    }

    pub fn is_element(&self) -> bool {
        matches!(self, NodeData::Element(_))
    }

    pub fn is_text(&self) -> bool {
        matches!(self, NodeData::Text(_))
    }

    /// The `nodeType` constant from the DOM spec, for Phase 3.
    pub fn node_type(&self) -> u16 {
        match self {
            NodeData::Element(_) => 1,
            NodeData::ProcessingInstruction(_) => 7,
            NodeData::Text(_) => 3,
            NodeData::Comment(_) => 8,
            NodeData::Document => 9,
            NodeData::Doctype(_) => 10,
            NodeData::Fragment => 11,
        }
    }
}

/// One arena slot's links plus its payload.
#[derive(Debug, Clone)]
struct Node {
    parent: Option<NodeId>,
    first_child: Option<NodeId>,
    last_child: Option<NodeId>,
    prev_sibling: Option<NodeId>,
    next_sibling: Option<NodeId>,
    data: NodeData,
}

impl Node {
    fn detached(data: NodeData) -> Node {
        Node {
            parent: None,
            first_child: None,
            last_child: None,
            prev_sibling: None,
            next_sibling: None,
            data,
        }
    }
}

// ---------------------------------------------------------------------------
// Errors
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum DomError {
    /// The id is out of range or refers to a destroyed slot.
    DeadNode(NodeId),
    /// The operation needed an element and got something else.
    NotAnElement(NodeId),
    /// Text, comment and doctype nodes cannot hold children.
    CannotHaveChildren(NodeId),
    /// Inserting this node here would make the tree cyclic: the node is the
    /// prospective parent, or an ancestor of it.
    HierarchyRequest { parent: NodeId, node: NodeId },
    /// The reference node is not a child of the parent it was given with.
    NotAChild { parent: NodeId, child: NodeId },
}

impl fmt::Display for DomError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            DomError::DeadNode(id) => write!(f, "node {} does not exist", id),
            DomError::NotAnElement(id) => write!(f, "node {} is not an element", id),
            DomError::CannotHaveChildren(id) => {
                write!(f, "node {} cannot have children", id)
            }
            DomError::HierarchyRequest { parent, node } => write!(
                f,
                "inserting {} into {} would create a cycle",
                node, parent
            ),
            DomError::NotAChild { parent, child } => {
                write!(f, "node {} is not a child of {}", child, parent)
            }
        }
    }
}

impl std::error::Error for DomError {}

pub type Result<T> = std::result::Result<T, DomError>;

// ---------------------------------------------------------------------------
// The arena
// ---------------------------------------------------------------------------

/// An arena of DOM nodes.
///
/// Every `Dom` is created with a `Document` node already in it, at
/// [`Dom::document`]. Additional detached roots (extra documents, or the
/// scratch roots a fragment parser needs) can be created freely; the arena
/// does not require a single connected tree.
#[derive(Debug, Clone)]
pub struct Dom {
    /// `None` marks a destroyed slot. Slots are never reused; see the module
    /// docs for why.
    nodes: Vec<Option<Node>>,
    document: NodeId,
}

impl Default for Dom {
    fn default() -> Self {
        Dom::new()
    }
}

impl Dom {
    pub fn new() -> Dom {
        Dom::with_capacity(64)
    }

    pub fn with_capacity(capacity: usize) -> Dom {
        let mut dom = Dom {
            nodes: Vec::with_capacity(capacity),
            document: NodeId(0),
        };
        let doc = dom.push(NodeData::Document);
        dom.document = doc;
        dom
    }

    /// The document node this arena was created around.
    #[inline]
    pub fn document(&self) -> NodeId {
        self.document
    }

    /// Total slots ever allocated, including tombstones.
    #[inline]
    pub fn capacity_used(&self) -> usize {
        self.nodes.len()
    }

    /// Slots currently holding a live node.
    pub fn live_count(&self) -> usize {
        self.nodes.iter().filter(|n| n.is_some()).count()
    }

    // -- slot access -------------------------------------------------------

    fn push(&mut self, data: NodeData) -> NodeId {
        let id = NodeId(self.nodes.len() as u32);
        self.nodes.push(Some(Node::detached(data)));
        id
    }

    #[inline]
    fn slot(&self, id: NodeId) -> Result<&Node> {
        self.nodes
            .get(id.index())
            .and_then(|s| s.as_ref())
            .ok_or(DomError::DeadNode(id))
    }

    #[inline]
    fn slot_mut(&mut self, id: NodeId) -> Result<&mut Node> {
        self.nodes
            .get_mut(id.index())
            .and_then(|s| s.as_mut())
            .ok_or(DomError::DeadNode(id))
    }

    /// Infallible slot access for internal use after validation.
    ///
    /// Every caller has already proved the id is live in this same function
    /// body; a panic here means an arena invariant broke, which is a bug, not
    /// a recoverable condition.
    #[inline]
    fn n(&self, id: NodeId) -> &Node {
        self.nodes[id.index()]
            .as_ref()
            .expect("dangling NodeId in arena internals")
    }

    #[inline]
    fn n_mut(&mut self, id: NodeId) -> &mut Node {
        self.nodes[id.index()]
            .as_mut()
            .expect("dangling NodeId in arena internals")
    }

    /// Is this id in range and not a tombstone?
    #[inline]
    pub fn is_alive(&self, id: NodeId) -> bool {
        self.nodes.get(id.index()).is_some_and(|s| s.is_some())
    }

    pub fn data(&self, id: NodeId) -> Result<&NodeData> {
        Ok(&self.slot(id)?.data)
    }

    pub fn data_mut(&mut self, id: NodeId) -> Result<&mut NodeData> {
        Ok(&mut self.slot_mut(id)?.data)
    }

    /// The element payload, or `NotAnElement`.
    pub fn element(&self, id: NodeId) -> Result<&ElementData> {
        match &self.slot(id)?.data {
            NodeData::Element(e) => Ok(e),
            _ => Err(DomError::NotAnElement(id)),
        }
    }

    pub fn element_mut(&mut self, id: NodeId) -> Result<&mut ElementData> {
        match &mut self.slot_mut(id)?.data {
            NodeData::Element(e) => Ok(e),
            _ => Err(DomError::NotAnElement(id)),
        }
    }

    /// The tag name of an element, or `None` for any other kind of node.
    ///
    /// Convenient for the tree builder, which asks "is the current node a
    /// `<p>`?" constantly and does not want to unwrap a `Result` each time.
    pub fn tag_name(&self, id: NodeId) -> Option<&str> {
        match self.nodes.get(id.index())?.as_ref()?.data {
            NodeData::Element(ref e) => Some(e.name.as_str()),
            _ => None,
        }
    }

    /// The namespace of an element, or `None` for any other kind of node.
    pub fn namespace(&self, id: NodeId) -> Option<Namespace> {
        match self.nodes.get(id.index())?.as_ref()?.data {
            NodeData::Element(ref e) => Some(e.namespace),
            _ => None,
        }
    }

    /// Is this an HTML-namespace element with this exact (lowercase) name?
    /// The single most common question the tree builder asks.
    pub fn is_html_element(&self, id: NodeId, name: &str) -> bool {
        match self.nodes.get(id.index()).and_then(|s| s.as_ref()) {
            Some(Node {
                data: NodeData::Element(e),
                ..
            }) => e.namespace.is_html() && e.name == name,
            _ => false,
        }
    }

    // -- construction ------------------------------------------------------

    /// A detached element in an explicit namespace.
    pub fn create_element(&mut self, namespace: Namespace, name: impl Into<String>) -> NodeId {
        self.push(NodeData::Element(ElementData {
            namespace,
            name: name.into(),
            attrs: Vec::new(),
        }))
    }

    /// A detached HTML element. The overwhelmingly common case.
    pub fn create_html_element(&mut self, name: impl Into<String>) -> NodeId {
        self.create_element(Namespace::Html, name)
    }

    /// A detached HTML element with attributes, in one call, as the tree
    /// builder's "create an element for a token" step wants.
    pub fn create_element_with_attrs(
        &mut self,
        namespace: Namespace,
        name: impl Into<String>,
        attrs: Vec<Attr>,
    ) -> NodeId {
        self.push(NodeData::Element(ElementData {
            namespace,
            name: name.into(),
            attrs,
        }))
    }

    pub fn create_text(&mut self, text: impl Into<String>) -> NodeId {
        self.push(NodeData::Text(text.into()))
    }

    pub fn create_comment(&mut self, text: impl Into<String>) -> NodeId {
        self.push(NodeData::Comment(text.into()))
    }

    pub fn create_processing_instruction(
        &mut self,
        target: impl Into<String>,
        data: impl Into<String>,
    ) -> NodeId {
        self.push(NodeData::ProcessingInstruction(PiData {
            target: target.into(),
            data: data.into(),
        }))
    }

    pub fn create_doctype(
        &mut self,
        name: impl Into<String>,
        public_id: impl Into<String>,
        system_id: impl Into<String>,
    ) -> NodeId {
        self.push(NodeData::Doctype(DoctypeData {
            name: name.into(),
            public_id: public_id.into(),
            system_id: system_id.into(),
        }))
    }

    /// An additional, detached `Document` node.
    ///
    /// Phase 2's fragment-parsing algorithm needs a scratch root that is not
    /// the main document. Since Phase 2 also added [`NodeData::Fragment`],
    /// [`Dom::create_fragment`] is now the better answer for that particular
    /// job; this stays for anything that genuinely wants a second document.
    pub fn create_document(&mut self) -> NodeId {
        self.push(NodeData::Document)
    }

    /// A detached `DocumentFragment`.
    pub fn create_fragment(&mut self) -> NodeId {
        self.push(NodeData::Fragment)
    }

    // -- links -------------------------------------------------------------

    #[inline]
    pub fn parent(&self, id: NodeId) -> Option<NodeId> {
        self.nodes.get(id.index())?.as_ref()?.parent
    }

    #[inline]
    pub fn first_child(&self, id: NodeId) -> Option<NodeId> {
        self.nodes.get(id.index())?.as_ref()?.first_child
    }

    #[inline]
    pub fn last_child(&self, id: NodeId) -> Option<NodeId> {
        self.nodes.get(id.index())?.as_ref()?.last_child
    }

    #[inline]
    pub fn prev_sibling(&self, id: NodeId) -> Option<NodeId> {
        self.nodes.get(id.index())?.as_ref()?.prev_sibling
    }

    #[inline]
    pub fn next_sibling(&self, id: NodeId) -> Option<NodeId> {
        self.nodes.get(id.index())?.as_ref()?.next_sibling
    }

    pub fn has_children(&self, id: NodeId) -> bool {
        self.first_child(id).is_some()
    }

    /// Position of `child` among `parent`'s children. O(n); the tree builder
    /// should prefer sibling links where it can.
    pub fn child_index(&self, parent: NodeId, child: NodeId) -> Option<usize> {
        self.children(parent).position(|c| c == child)
    }

    pub fn child_at(&self, parent: NodeId, index: usize) -> Option<NodeId> {
        self.children(parent).nth(index)
    }

    pub fn child_count(&self, parent: NodeId) -> usize {
        self.children(parent).count()
    }

    /// Is `ancestor` equal to, or an ancestor of, `descendant`?
    ///
    /// Walks up from `descendant`, so it costs O(depth), not O(subtree).
    pub fn contains(&self, ancestor: NodeId, descendant: NodeId) -> bool {
        let mut cur = Some(descendant);
        while let Some(id) = cur {
            if id == ancestor {
                return true;
            }
            cur = self.parent(id);
        }
        false
    }

    /// The root of `id`'s tree: the topmost node reachable by parent links.
    pub fn root(&self, id: NodeId) -> NodeId {
        let mut cur = id;
        while let Some(p) = self.parent(cur) {
            cur = p;
        }
        cur
    }

    // -- traversal ---------------------------------------------------------

    pub fn children(&self, parent: NodeId) -> Children<'_> {
        Children {
            dom: self,
            next: self.first_child(parent),
            next_back: self.last_child(parent),
        }
    }

    pub fn ancestors(&self, id: NodeId) -> Ancestors<'_> {
        Ancestors {
            dom: self,
            next: self.parent(id),
        }
    }

    /// Pre-order traversal of the subtree rooted at `id`, including `id`.
    pub fn traverse(&self, id: NodeId) -> PreOrder<'_> {
        PreOrder {
            dom: self,
            root: id,
            next: if self.is_alive(id) { Some(id) } else { None },
        }
    }

    /// Pre-order traversal of the subtree rooted at `id`, excluding `id`.
    pub fn descendants(&self, id: NodeId) -> PreOrder<'_> {
        let mut it = self.traverse(id);
        it.next();
        it
    }

    /// The next node in document order within the subtree rooted at `root`.
    fn next_preorder(&self, cur: NodeId, root: NodeId) -> Option<NodeId> {
        if let Some(c) = self.first_child(cur) {
            return Some(c);
        }
        let mut node = cur;
        loop {
            if node == root {
                return None;
            }
            if let Some(s) = self.next_sibling(node) {
                return Some(s);
            }
            node = self.parent(node)?;
        }
    }

    /// Concatenation of every descendant text node, in document order.
    pub fn text_content(&self, id: NodeId) -> String {
        let mut out = String::new();
        for node in self.traverse(id) {
            if let NodeData::Text(t) = &self.n(node).data {
                out.push_str(t);
            }
        }
        out
    }

    // -- mutation ----------------------------------------------------------

    /// Detach `node` from its parent, leaving its own subtree intact.
    ///
    /// This is the DOM's `remove`, and deliberately does *not* free anything:
    /// the adoption agency algorithm removes a node from one parent and
    /// reinserts it elsewhere a step later, children and all.
    ///
    /// O(1) thanks to the doubly-linked sibling list. Detaching an already
    /// detached node succeeds and does nothing.
    pub fn remove(&mut self, node: NodeId) -> Result<()> {
        self.slot(node)?;
        self.unlink(node);
        Ok(())
    }

    /// Link surgery for detachment. Assumes `node` is live.
    fn unlink(&mut self, node: NodeId) {
        let Some(parent) = self.n(node).parent else {
            // Already detached. Siblings are None by invariant.
            return;
        };
        let prev = self.n(node).prev_sibling;
        let next = self.n(node).next_sibling;

        match prev {
            Some(p) => self.n_mut(p).next_sibling = next,
            None => self.n_mut(parent).first_child = next,
        }
        match next {
            Some(n) => self.n_mut(n).prev_sibling = prev,
            None => self.n_mut(parent).last_child = prev,
        }

        let slot = self.n_mut(node);
        slot.parent = None;
        slot.prev_sibling = None;
        slot.next_sibling = None;
    }

    /// Append `new_child` to `parent`'s child list.
    ///
    /// If `new_child` already has a parent it is detached from it first, so
    /// this doubles as a move.
    pub fn append_child(&mut self, parent: NodeId, new_child: NodeId) -> Result<()> {
        self.insert_before(parent, new_child, None)
    }

    /// Prepend `new_child` to `parent`'s child list.
    pub fn prepend_child(&mut self, parent: NodeId, new_child: NodeId) -> Result<()> {
        let first = self.first_child(parent);
        self.insert_before(parent, new_child, first)
    }

    /// Insert `new_child` into `parent` immediately before `reference`.
    ///
    /// `reference` of `None` means "append", matching the spec's optional
    /// `child` argument to "insert a node into a parent before a child".
    ///
    /// This is the workhorse. Foster parenting needs to insert at an
    /// arbitrary position (before the `<table>` inside the table's parent),
    /// and the adoption agency algorithm needs to insert a node that is
    /// currently attached somewhere else entirely, so this single entry point
    /// detaches first and inserts second.
    pub fn insert_before(
        &mut self,
        parent: NodeId,
        new_child: NodeId,
        reference: Option<NodeId>,
    ) -> Result<()> {
        // Validate everything before touching a single link, so a rejected
        // insertion leaves the tree exactly as it was.
        if !self.slot(parent)?.data.can_have_children() {
            return Err(DomError::CannotHaveChildren(parent));
        }
        // A document is always a root. Letting one become a child would make
        // `root()` lie and give the tree two documents' worth of ancestry.
        if matches!(self.slot(new_child)?.data, NodeData::Document) {
            return Err(DomError::HierarchyRequest {
                parent,
                node: new_child,
            });
        }

        // A node cannot contain itself or its own ancestor.
        if self.contains(new_child, parent) {
            return Err(DomError::HierarchyRequest {
                parent,
                node: new_child,
            });
        }

        let mut reference = reference;
        if let Some(r) = reference {
            self.slot(r)?;
            if self.n(r).parent != Some(parent) {
                return Err(DomError::NotAChild {
                    parent,
                    child: r,
                });
            }
            // "If reference child is node, set it to node's next sibling."
            // Without this, detaching `new_child` below would strand the
            // reference and we would relink against a detached node.
            if r == new_child {
                reference = self.n(r).next_sibling;
            }
        }

        self.unlink(new_child);

        match reference {
            Some(r) => {
                let prev = self.n(r).prev_sibling;
                {
                    let slot = self.n_mut(new_child);
                    slot.prev_sibling = prev;
                    slot.next_sibling = Some(r);
                }
                self.n_mut(r).prev_sibling = Some(new_child);
                match prev {
                    Some(p) => self.n_mut(p).next_sibling = Some(new_child),
                    None => self.n_mut(parent).first_child = Some(new_child),
                }
            }
            None => {
                let last = self.n(parent).last_child;
                {
                    let slot = self.n_mut(new_child);
                    slot.prev_sibling = last;
                    slot.next_sibling = None;
                }
                match last {
                    Some(l) => self.n_mut(l).next_sibling = Some(new_child),
                    None => self.n_mut(parent).first_child = Some(new_child),
                }
                self.n_mut(parent).last_child = Some(new_child);
            }
        }
        self.n_mut(new_child).parent = Some(parent);
        Ok(())
    }

    /// Insert `new_child` into `parent` immediately after `reference`.
    ///
    /// `reference` of `None` means "prepend".
    pub fn insert_after(
        &mut self,
        parent: NodeId,
        new_child: NodeId,
        reference: Option<NodeId>,
    ) -> Result<()> {
        let before = match reference {
            Some(r) => {
                self.slot(r)?;
                if self.n(r).parent != Some(parent) {
                    return Err(DomError::NotAChild { parent, child: r });
                }
                self.n(r).next_sibling
            }
            None => self.first_child(parent),
        };
        self.insert_before(parent, new_child, before)
    }

    /// Move `node` — and everything under it — to a new position.
    ///
    /// This is the operation the adoption agency algorithm leans on hardest:
    /// it relocates whole subtrees between parents mid-parse. It is a named
    /// alias for [`Dom::insert_before`] rather than a second code path, on
    /// purpose: one implementation means one set of link-surgery bugs to
    /// find, and the aliasing hazards (moving a node next to itself, moving
    /// within the same parent, moving into a node that used to be a sibling)
    /// are all handled once, there.
    #[inline]
    pub fn move_subtree(
        &mut self,
        node: NodeId,
        new_parent: NodeId,
        before: Option<NodeId>,
    ) -> Result<()> {
        self.insert_before(new_parent, node, before)
    }

    /// Move every child of `from` into `to`, preserving order.
    ///
    /// The adoption agency algorithm's penultimate step is literally "take
    /// all of the child nodes of the furthest block and append them to the
    /// new element", so this is a primitive, not a convenience.
    ///
    /// `before` positions the moved run inside `to`; `None` appends.
    pub fn move_children(
        &mut self,
        from: NodeId,
        to: NodeId,
        before: Option<NodeId>,
    ) -> Result<()> {
        self.slot(from)?;
        if !self.slot(to)?.data.can_have_children() {
            return Err(DomError::CannotHaveChildren(to));
        }
        if self.contains(from, to) {
            return Err(DomError::HierarchyRequest {
                parent: to,
                node: from,
            });
        }
        if let Some(r) = before {
            self.slot(r)?;
            if self.n(r).parent != Some(to) {
                return Err(DomError::NotAChild { parent: to, child: r });
            }
        }
        // Snapshot the child list: the links are about to be rewritten under
        // us, so iterating lazily would walk into the destination.
        let kids: Vec<NodeId> = self.children(from).collect();
        for child in kids {
            self.insert_before(to, child, before)?;
        }
        Ok(())
    }

    /// Replace `old_child` with `new_child` in `parent`.
    ///
    /// `old_child` ends up detached with its own subtree intact.
    pub fn replace_child(
        &mut self,
        parent: NodeId,
        new_child: NodeId,
        old_child: NodeId,
    ) -> Result<()> {
        self.slot(old_child)?;
        if self.n(old_child).parent != Some(parent) {
            return Err(DomError::NotAChild {
                parent,
                child: old_child,
            });
        }
        if new_child == old_child {
            return Ok(());
        }
        let after = self.n(old_child).next_sibling;
        self.insert_before(parent, new_child, after)?;
        self.unlink(old_child);
        Ok(())
    }

    /// Detach and drop every node in the subtree rooted at `node`.
    ///
    /// The ids become permanently dead; they are never handed out again.
    pub fn destroy_subtree(&mut self, node: NodeId) -> Result<()> {
        self.slot(node)?;
        self.unlink(node);
        // Iterative, not recursive: a pathological page can nest thousands of
        // elements deep and a recursive free would blow the stack.
        let mut stack = vec![node];
        while let Some(id) = stack.pop() {
            let mut child = self.first_child(id);
            while let Some(c) = child {
                child = self.next_sibling(c);
                stack.push(c);
            }
            self.nodes[id.index()] = None;
        }
        Ok(())
    }

    // -- text --------------------------------------------------------------

    /// Insert text into `parent` before `reference`, merging into an adjacent
    /// text node where the spec calls for it.
    ///
    /// The tree builder's "insert a character" step says: if the node
    /// immediately before the insertion position is a `Text` node, append the
    /// data to it; otherwise create a new `Text` node there. Getting this
    /// wrong does not corrupt the tree, it fragments it — a paragraph becomes
    /// dozens of one-character text nodes, which is both slow and observably
    /// wrong to script walking `childNodes`.
    ///
    /// Returns the id of the text node the data ended up in, which may be a
    /// pre-existing node rather than a new one.
    pub fn insert_text(
        &mut self,
        parent: NodeId,
        text: &str,
        reference: Option<NodeId>,
    ) -> Result<NodeId> {
        if !self.slot(parent)?.data.can_have_children() {
            return Err(DomError::CannotHaveChildren(parent));
        }
        // The insertion position's preceding sibling is the merge candidate.
        let preceding = match reference {
            Some(r) => {
                self.slot(r)?;
                if self.n(r).parent != Some(parent) {
                    return Err(DomError::NotAChild { parent, child: r });
                }
                self.n(r).prev_sibling
            }
            None => self.n(parent).last_child,
        };

        if let Some(prev) = preceding {
            if let NodeData::Text(existing) = &mut self.n_mut(prev).data {
                existing.push_str(text);
                return Ok(prev);
            }
        }

        let node = self.create_text(text);
        self.insert_before(parent, node, reference)?;
        Ok(node)
    }

    /// Append text to `parent`, merging with a trailing text node if there is
    /// one.
    #[inline]
    pub fn append_text(&mut self, parent: NodeId, text: &str) -> Result<NodeId> {
        self.insert_text(parent, text, None)
    }

    // -- cloning -----------------------------------------------------------

    /// Copy a node's payload into a fresh, detached node. Children are not
    /// copied.
    ///
    /// The adoption agency algorithm clones formatting elements — attributes
    /// and all, children explicitly not — twice per iteration, so this is on
    /// a hot path of misnested-markup recovery.
    pub fn clone_shallow(&mut self, node: NodeId) -> Result<NodeId> {
        let data = self.slot(node)?.data.clone();
        Ok(self.push(data))
    }

    /// Copy a whole subtree into a fresh, detached subtree.
    ///
    /// Returns the new root. Used by `cloneNode(true)` and by `<template>`
    /// instantiation later on.
    pub fn clone_deep(&mut self, node: NodeId) -> Result<NodeId> {
        let root = self.clone_shallow(node)?;
        // (source, destination) pairs still needing their children copied.
        let mut stack = vec![(node, root)];
        while let Some((src, dst)) = stack.pop() {
            let kids: Vec<NodeId> = self.children(src).collect();
            for kid in kids {
                let copy = self.clone_shallow(kid)?;
                self.append_child(dst, copy)?;
                stack.push((kid, copy));
            }
        }
        Ok(root)
    }

    // -- attributes --------------------------------------------------------

    /// Attributes in source order.
    pub fn attributes(&self, element: NodeId) -> Result<&[Attr]> {
        Ok(&self.element(element)?.attrs)
    }

    /// Should attribute lookups on this element ignore ASCII case?
    ///
    /// Yes for HTML-namespace elements: `getAttribute("CLASS")` and
    /// `getAttribute("class")` are the same attribute on an HTML element, and
    /// no on foreign elements, where `clipPathUnits` and `clippathunits` are
    /// genuinely different attributes.
    fn attrs_are_case_insensitive(&self, element: NodeId) -> Result<bool> {
        Ok(self.element(element)?.namespace.is_html())
    }

    pub fn get_attribute(&self, element: NodeId, name: &str) -> Result<Option<&str>> {
        let ci = self.attrs_are_case_insensitive(element)?;
        Ok(self
            .element(element)?
            .attrs
            .iter()
            .find(|a| a.matches(name, ci))
            .map(|a| a.value.as_str()))
    }

    pub fn has_attribute(&self, element: NodeId, name: &str) -> Result<bool> {
        Ok(self.get_attribute(element, name)?.is_some())
    }

    /// Set an attribute, preserving position if it already exists.
    ///
    /// Overwriting must not reorder: `attributes` is index-addressable, so
    /// moving an attribute to the end of the list on every assignment would
    /// be observable from script.
    ///
    /// New attribute names are ASCII-lowercased on HTML elements, matching
    /// `setAttribute`'s behaviour for elements in an HTML document.
    pub fn set_attribute(
        &mut self,
        element: NodeId,
        name: &str,
        value: impl Into<String>,
    ) -> Result<()> {
        let ci = self.attrs_are_case_insensitive(element)?;
        let el = self.element_mut(element)?;
        if let Some(existing) = el.attrs.iter_mut().find(|a| a.matches(name, ci)) {
            existing.value = value.into();
            return Ok(());
        }
        let local = if ci {
            name.to_ascii_lowercase()
        } else {
            name.to_string()
        };
        el.attrs.push(Attr {
            namespace: None,
            prefix: None,
            local,
            value: value.into(),
        });
        Ok(())
    }

    /// Add an attribute only if the element does not already have one by that
    /// name.
    ///
    /// The tree builder needs exactly this: a second `<html>` or `<body>`
    /// start tag does not create a node, it merges "every attribute on the
    /// token ... not already present on the top element of the stack".
    ///
    /// Returns whether the attribute was added.
    pub fn add_attribute_if_missing(
        &mut self,
        element: NodeId,
        attr: Attr,
    ) -> Result<bool> {
        let ci = self.attrs_are_case_insensitive(element)?;
        let name = attr.qualified_name();
        let el = self.element_mut(element)?;
        if el.attrs.iter().any(|a| a.matches(&name, ci)) {
            return Ok(false);
        }
        el.attrs.push(attr);
        Ok(true)
    }

    /// Append an attribute verbatim, namespace and prefix intact, without the
    /// name-mangling `set_attribute` applies.
    ///
    /// This is the tokenizer's entry point: it has already normalised names,
    /// and "adjust foreign attributes" has already assigned namespaces.
    pub fn push_attribute(&mut self, element: NodeId, attr: Attr) -> Result<()> {
        self.element_mut(element)?.attrs.push(attr);
        Ok(())
    }

    /// Returns whether an attribute was actually removed.
    pub fn remove_attribute(&mut self, element: NodeId, name: &str) -> Result<bool> {
        let ci = self.attrs_are_case_insensitive(element)?;
        let el = self.element_mut(element)?;
        match el.attrs.iter().position(|a| a.matches(name, ci)) {
            Some(i) => {
                el.attrs.remove(i);
                Ok(true)
            }
            None => Ok(false),
        }
    }

    // -- debugging ---------------------------------------------------------

    /// A compact, deterministic rendering of a subtree.
    ///
    /// Not an HTML serializer — that is Phase 4's problem, and it has to
    /// worry about escaping and void elements. This exists so tests can
    /// assert on tree *shape* in one readable line.
    pub fn to_debug_string(&self, id: NodeId) -> String {
        let mut out = String::new();
        self.debug_into(id, &mut out);
        out
    }

    fn debug_into(&self, id: NodeId, out: &mut String) {
        let Ok(node) = self.slot(id) else {
            out.push_str("<dead>");
            return;
        };
        match &node.data {
            NodeData::Document => {
                out.push_str("#document[");
                for c in self.children(id) {
                    self.debug_into(c, out);
                }
                out.push(']');
            }
            NodeData::Fragment => {
                out.push_str("#fragment[");
                for c in self.children(id) {
                    self.debug_into(c, out);
                }
                out.push(']');
            }
            NodeData::Doctype(d) => {
                out.push_str("<!DOCTYPE ");
                out.push_str(&d.name);
                out.push('>');
            }
            NodeData::Element(e) => {
                out.push('<');
                out.push_str(&e.name);
                for a in &e.attrs {
                    out.push(' ');
                    out.push_str(&a.qualified_name());
                    out.push_str("=\"");
                    out.push_str(&a.value);
                    out.push('"');
                }
                out.push('>');
                for c in self.children(id) {
                    self.debug_into(c, out);
                }
                out.push_str("</");
                out.push_str(&e.name);
                out.push('>');
            }
            NodeData::Text(t) => out.push_str(t),
            NodeData::Comment(t) => {
                out.push_str("<!--");
                out.push_str(t);
                out.push_str("-->");
            }
            NodeData::ProcessingInstruction(p) => {
                out.push_str("<?");
                out.push_str(&p.target);
                out.push(' ');
                out.push_str(&p.data);
                out.push('>');
            }
        }
    }

    /// Panics if any structural invariant is violated.
    ///
    /// Every mutating test calls this. Link surgery is the kind of code where
    /// a bug leaves the tree readable in one direction and corrupt in the
    /// other, and a test that only walks `first_child`/`next_sibling` will
    /// never notice a broken `prev_sibling` chain — which is precisely the
    /// chain the adoption agency algorithm depends on.
    pub fn assert_invariants(&self) {
        for (i, slot) in self.nodes.iter().enumerate() {
            let Some(node) = slot else { continue };
            let id = NodeId(i as u32);

            if !node.data.can_have_children() {
                assert!(
                    node.first_child.is_none() && node.last_child.is_none(),
                    "{} cannot have children but has some",
                    id
                );
            }

            // A detached node has no siblings.
            if node.parent.is_none() {
                assert!(
                    node.prev_sibling.is_none() && node.next_sibling.is_none(),
                    "detached {} still has sibling links",
                    id
                );
            }

            // Forward and backward child chains must agree, and every child
            // must point back at this parent.
            let mut seen = 0usize;
            let mut cur = node.first_child;
            let mut prev: Option<NodeId> = None;
            while let Some(c) = cur {
                assert!(self.is_alive(c), "{} has dead child {}", id, c);
                let child = self.n(c);
                assert_eq!(child.parent, Some(id), "{} does not point back at {}", c, id);
                assert_eq!(child.prev_sibling, prev, "prev_sibling broken at {}", c);
                prev = Some(c);
                cur = child.next_sibling;
                seen += 1;
                assert!(seen <= self.nodes.len(), "cycle in child list of {}", id);
            }
            assert_eq!(node.last_child, prev, "last_child wrong on {}", id);

            // No node may be its own ancestor.
            let mut walker = node.parent;
            let mut depth = 0usize;
            while let Some(p) = walker {
                assert_ne!(p, id, "{} is its own ancestor", id);
                walker = self.n(p).parent;
                depth += 1;
                assert!(depth <= self.nodes.len(), "cycle in ancestor chain of {}", id);
            }
        }
    }
}

// ---------------------------------------------------------------------------
// Iterators
// ---------------------------------------------------------------------------

/// Children of a node, in order. Double-ended, because the tree builder walks
/// backwards nearly as often as forwards.
pub struct Children<'a> {
    dom: &'a Dom,
    next: Option<NodeId>,
    next_back: Option<NodeId>,
}

impl Iterator for Children<'_> {
    type Item = NodeId;

    fn next(&mut self) -> Option<NodeId> {
        let cur = self.next?;
        if Some(cur) == self.next_back {
            self.next = None;
            self.next_back = None;
        } else {
            self.next = self.dom.next_sibling(cur);
        }
        Some(cur)
    }
}

impl DoubleEndedIterator for Children<'_> {
    fn next_back(&mut self) -> Option<NodeId> {
        let cur = self.next_back?;
        if Some(cur) == self.next {
            self.next = None;
            self.next_back = None;
        } else {
            self.next_back = self.dom.prev_sibling(cur);
        }
        Some(cur)
    }
}

/// Ancestors of a node, nearest first. Excludes the node itself.
pub struct Ancestors<'a> {
    dom: &'a Dom,
    next: Option<NodeId>,
}

impl Iterator for Ancestors<'_> {
    type Item = NodeId;

    fn next(&mut self) -> Option<NodeId> {
        let cur = self.next?;
        self.next = self.dom.parent(cur);
        Some(cur)
    }
}

/// Pre-order (document order) traversal of a subtree.
pub struct PreOrder<'a> {
    dom: &'a Dom,
    root: NodeId,
    next: Option<NodeId>,
}

impl Iterator for PreOrder<'_> {
    type Item = NodeId;

    fn next(&mut self) -> Option<NodeId> {
        let cur = self.next?;
        self.next = self.dom.next_preorder(cur, self.root);
        Some(cur)
    }
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    /// `<html><body>...</body></html>` under the document, with `body`
    /// returned. Zero Python involved anywhere in this module's tests.
    fn scaffold() -> (Dom, NodeId, NodeId) {
        let mut dom = Dom::new();
        let doc = dom.document();
        let html = dom.create_html_element("html");
        dom.append_child(doc, html).unwrap();
        let body = dom.create_html_element("body");
        dom.append_child(html, body).unwrap();
        (dom, html, body)
    }

    fn kids(dom: &Dom, parent: NodeId) -> Vec<NodeId> {
        dom.children(parent).collect()
    }

    #[test]
    fn document_exists_and_is_empty() {
        let dom = Dom::new();
        assert!(dom.is_alive(dom.document()));
        assert_eq!(dom.data(dom.document()).unwrap(), &NodeData::Document);
        assert!(!dom.has_children(dom.document()));
        assert_eq!(dom.live_count(), 1);
        dom.assert_invariants();
    }

    #[test]
    fn dead_ids_are_rejected_not_dereferenced() {
        let dom = Dom::new();
        let bogus = NodeId(9999);
        assert!(!dom.is_alive(bogus));
        assert_eq!(dom.data(bogus), Err(DomError::DeadNode(bogus)));
        assert_eq!(dom.parent(bogus), None);
        assert_eq!(dom.tag_name(bogus), None);
        assert_eq!(dom.children(bogus).count(), 0);
    }

    #[test]
    fn append_builds_ordered_child_list() {
        let (mut dom, _html, body) = scaffold();
        let a = dom.create_html_element("a");
        let b = dom.create_html_element("b");
        let c = dom.create_html_element("c");
        for id in [a, b, c] {
            dom.append_child(body, id).unwrap();
        }
        dom.assert_invariants();

        assert_eq!(kids(&dom, body), vec![a, b, c]);
        assert_eq!(dom.first_child(body), Some(a));
        assert_eq!(dom.last_child(body), Some(c));
        assert_eq!(dom.next_sibling(a), Some(b));
        assert_eq!(dom.prev_sibling(c), Some(b));
        assert_eq!(dom.prev_sibling(a), None);
        assert_eq!(dom.next_sibling(c), None);
        assert_eq!(dom.parent(b), Some(body));
        // Backwards iteration must agree with forwards.
        let mut back: Vec<NodeId> = dom.children(body).rev().collect();
        back.reverse();
        assert_eq!(back, vec![a, b, c]);
    }

    #[test]
    fn insert_before_in_the_middle() {
        let (mut dom, _html, body) = scaffold();
        let a = dom.create_html_element("a");
        let c = dom.create_html_element("c");
        dom.append_child(body, a).unwrap();
        dom.append_child(body, c).unwrap();

        let b = dom.create_html_element("b");
        dom.insert_before(body, b, Some(c)).unwrap();
        dom.assert_invariants();

        assert_eq!(kids(&dom, body), vec![a, b, c]);
        assert_eq!(dom.prev_sibling(b), Some(a));
        assert_eq!(dom.next_sibling(b), Some(c));
        assert_eq!(dom.first_child(body), Some(a));
        assert_eq!(dom.last_child(body), Some(c));
    }

    #[test]
    fn insert_before_at_the_front_and_back() {
        let (mut dom, _html, body) = scaffold();
        let b = dom.create_html_element("b");
        dom.append_child(body, b).unwrap();

        let a = dom.create_html_element("a");
        dom.insert_before(body, a, Some(b)).unwrap();
        assert_eq!(dom.first_child(body), Some(a));

        let c = dom.create_html_element("c");
        dom.insert_before(body, c, None).unwrap();
        assert_eq!(dom.last_child(body), Some(c));

        dom.assert_invariants();
        assert_eq!(kids(&dom, body), vec![a, b, c]);
    }

    #[test]
    fn insert_before_with_a_foreign_reference_is_rejected() {
        let (mut dom, html, body) = scaffold();
        let stray = dom.create_html_element("div");
        dom.append_child(html, stray).unwrap();
        let new = dom.create_html_element("span");

        // `stray` is a child of html, not of body.
        assert_eq!(
            dom.insert_before(body, new, Some(stray)),
            Err(DomError::NotAChild {
                parent: body,
                child: stray
            })
        );
        // Rejected insertions must not have half-linked anything.
        assert_eq!(dom.parent(new), None);
        dom.assert_invariants();
    }

    #[test]
    fn insert_before_itself_is_a_no_op() {
        let (mut dom, _html, body) = scaffold();
        let a = dom.create_html_element("a");
        let b = dom.create_html_element("b");
        dom.append_child(body, a).unwrap();
        dom.append_child(body, b).unwrap();

        dom.insert_before(body, a, Some(a)).unwrap();
        dom.assert_invariants();
        assert_eq!(kids(&dom, body), vec![a, b]);
    }

    #[test]
    fn reordering_within_the_same_parent() {
        let (mut dom, _html, body) = scaffold();
        let a = dom.create_html_element("a");
        let b = dom.create_html_element("b");
        let c = dom.create_html_element("c");
        for id in [a, b, c] {
            dom.append_child(body, id).unwrap();
        }
        // Move the last child to the front: the aliasing case where the moved
        // node's old neighbours and its new neighbours overlap.
        dom.insert_before(body, c, Some(a)).unwrap();
        dom.assert_invariants();
        assert_eq!(kids(&dom, body), vec![c, a, b]);
        assert_eq!(dom.last_child(body), Some(b));
        assert_eq!(dom.next_sibling(b), None);
    }

    #[test]
    fn text_and_comment_nodes_reject_children() {
        let (mut dom, _html, body) = scaffold();
        let text = dom.create_text("hi");
        dom.append_child(body, text).unwrap();
        let el = dom.create_html_element("span");
        assert_eq!(
            dom.append_child(text, el),
            Err(DomError::CannotHaveChildren(text))
        );
        dom.assert_invariants();
    }

    #[test]
    fn remove_detaches_but_keeps_the_subtree() {
        let (mut dom, _html, body) = scaffold();
        let a = dom.create_html_element("a");
        let div = dom.create_html_element("div");
        let c = dom.create_html_element("c");
        for id in [a, div, c] {
            dom.append_child(body, id).unwrap();
        }
        let inner = dom.create_html_element("span");
        dom.append_child(div, inner).unwrap();
        dom.append_text(inner, "kept").unwrap();

        dom.remove(div).unwrap();
        dom.assert_invariants();

        // Detached from the old parent, whose links closed over the gap.
        assert_eq!(kids(&dom, body), vec![a, c]);
        assert_eq!(dom.next_sibling(a), Some(c));
        assert_eq!(dom.prev_sibling(c), Some(a));
        assert_eq!(dom.parent(div), None);
        assert_eq!(dom.prev_sibling(div), None);
        assert_eq!(dom.next_sibling(div), None);

        // ...but the subtree under it survived intact.
        assert!(dom.is_alive(inner));
        assert_eq!(dom.parent(inner), Some(div));
        assert_eq!(dom.text_content(div), "kept");

        // And it can go straight back in somewhere else.
        dom.insert_before(body, div, Some(c)).unwrap();
        dom.assert_invariants();
        assert_eq!(kids(&dom, body), vec![a, div, c]);
        assert_eq!(dom.text_content(body), "kept");
    }

    #[test]
    fn removing_a_detached_node_is_harmless() {
        let mut dom = Dom::new();
        let orphan = dom.create_html_element("div");
        dom.remove(orphan).unwrap();
        dom.remove(orphan).unwrap();
        dom.assert_invariants();
    }

    // -- the subtree-move path, which Phase 2 depends on ------------------

    #[test]
    fn move_subtree_between_parents() {
        let (mut dom, _html, body) = scaffold();
        let left = dom.create_html_element("left");
        let right = dom.create_html_element("right");
        dom.append_child(body, left).unwrap();
        dom.append_child(body, right).unwrap();

        // left: <x/><moving><deep>text</deep></moving><y/>
        let x = dom.create_html_element("x");
        let moving = dom.create_html_element("moving");
        let y = dom.create_html_element("y");
        for id in [x, moving, y] {
            dom.append_child(left, id).unwrap();
        }
        let deep = dom.create_html_element("deep");
        dom.append_child(moving, deep).unwrap();
        dom.append_text(deep, "payload").unwrap();

        // right: <p/><q/>
        let p = dom.create_html_element("p");
        let q = dom.create_html_element("q");
        dom.append_child(right, p).unwrap();
        dom.append_child(right, q).unwrap();

        dom.move_subtree(moving, right, Some(q)).unwrap();
        dom.assert_invariants();

        // The source parent closed over the hole in both directions.
        assert_eq!(kids(&dom, left), vec![x, y]);
        assert_eq!(dom.next_sibling(x), Some(y));
        assert_eq!(dom.prev_sibling(y), Some(x));
        assert_eq!(dom.first_child(left), Some(x));
        assert_eq!(dom.last_child(left), Some(y));

        // The destination took it at exactly the requested position.
        assert_eq!(kids(&dom, right), vec![p, moving, q]);
        assert_eq!(dom.parent(moving), Some(right));
        assert_eq!(dom.prev_sibling(moving), Some(p));
        assert_eq!(dom.next_sibling(moving), Some(q));

        // The subtree came along unchanged, and re-roots correctly.
        assert_eq!(dom.parent(deep), Some(moving));
        assert_eq!(dom.text_content(moving), "payload");
        assert_eq!(dom.text_content(left), "");
        assert_eq!(dom.text_content(right), "payload");
        assert_eq!(dom.root(deep), dom.document());
        let ancestry: Vec<NodeId> = dom.ancestors(deep).collect();
        assert_eq!(ancestry[0], moving);
        assert_eq!(ancestry[1], right);
        assert_eq!(ancestry[2], body);
    }

    #[test]
    fn move_subtree_to_a_detached_parent_and_back() {
        let (mut dom, _html, body) = scaffold();
        let attached = dom.create_html_element("div");
        dom.append_child(body, attached).unwrap();
        let child = dom.create_html_element("span");
        dom.append_child(attached, child).unwrap();

        // Into limbo: a node with no parent of its own.
        let limbo = dom.create_html_element("holder");
        dom.move_subtree(attached, limbo, None).unwrap();
        dom.assert_invariants();
        assert!(!dom.has_children(body));
        assert_eq!(dom.parent(attached), Some(limbo));
        assert_eq!(dom.root(child), limbo);

        // And back into the document.
        dom.move_subtree(attached, body, None).unwrap();
        dom.assert_invariants();
        assert_eq!(dom.root(child), dom.document());
        assert_eq!(kids(&dom, body), vec![attached]);
        assert!(!dom.has_children(limbo));
    }

    #[test]
    fn move_subtree_into_its_own_descendant_is_rejected() {
        let (mut dom, _html, body) = scaffold();
        let outer = dom.create_html_element("outer");
        let inner = dom.create_html_element("inner");
        let deepest = dom.create_html_element("deepest");
        dom.append_child(body, outer).unwrap();
        dom.append_child(outer, inner).unwrap();
        dom.append_child(inner, deepest).unwrap();

        assert_eq!(
            dom.move_subtree(outer, deepest, None),
            Err(DomError::HierarchyRequest {
                parent: deepest,
                node: outer
            })
        );
        assert_eq!(
            dom.append_child(outer, outer),
            Err(DomError::HierarchyRequest {
                parent: outer,
                node: outer
            })
        );
        // The tree must be exactly as it was.
        dom.assert_invariants();
        assert_eq!(dom.parent(outer), Some(body));
        assert_eq!(dom.parent(inner), Some(outer));
        assert_eq!(dom.parent(deepest), Some(inner));
    }

    #[test]
    fn a_document_cannot_become_a_child() {
        let (mut dom, _html, body) = scaffold();
        let doc = dom.document();
        assert_eq!(
            dom.append_child(body, doc),
            Err(DomError::HierarchyRequest {
                parent: body,
                node: doc
            })
        );
        // Including a second, detached document used as a fragment root.
        let scratch = dom.create_document();
        assert!(dom.append_child(body, scratch).is_err());
        dom.assert_invariants();
        assert_eq!(dom.root(body), doc);
    }

    #[test]
    fn move_children_preserves_order() {
        let (mut dom, _html, body) = scaffold();
        let from = dom.create_html_element("from");
        let to = dom.create_html_element("to");
        dom.append_child(body, from).unwrap();
        dom.append_child(body, to).unwrap();

        let existing = dom.create_html_element("existing");
        dom.append_child(to, existing).unwrap();

        let a = dom.create_html_element("a");
        let b = dom.create_html_element("b");
        let c = dom.create_html_element("c");
        for id in [a, b, c] {
            dom.append_child(from, id).unwrap();
        }

        dom.move_children(from, to, None).unwrap();
        dom.assert_invariants();

        assert!(!dom.has_children(from));
        assert_eq!(kids(&dom, to), vec![existing, a, b, c]);
        for id in [a, b, c] {
            assert_eq!(dom.parent(id), Some(to));
        }
    }

    #[test]
    fn move_children_at_a_position() {
        let (mut dom, _html, body) = scaffold();
        let from = dom.create_html_element("from");
        let to = dom.create_html_element("to");
        dom.append_child(body, from).unwrap();
        dom.append_child(body, to).unwrap();

        let head = dom.create_html_element("head");
        let tail = dom.create_html_element("tail");
        dom.append_child(to, head).unwrap();
        dom.append_child(to, tail).unwrap();

        let a = dom.create_html_element("a");
        let b = dom.create_html_element("b");
        dom.append_child(from, a).unwrap();
        dom.append_child(from, b).unwrap();

        dom.move_children(from, to, Some(tail)).unwrap();
        dom.assert_invariants();
        assert_eq!(kids(&dom, to), vec![head, a, b, tail]);
    }

    #[test]
    fn replace_child_swaps_in_place() {
        let (mut dom, _html, body) = scaffold();
        let a = dom.create_html_element("a");
        let old = dom.create_html_element("old");
        let c = dom.create_html_element("c");
        for id in [a, old, c] {
            dom.append_child(body, id).unwrap();
        }
        let kept = dom.create_html_element("kept");
        dom.append_child(old, kept).unwrap();

        let new = dom.create_html_element("new");
        dom.replace_child(body, new, old).unwrap();
        dom.assert_invariants();

        assert_eq!(kids(&dom, body), vec![a, new, c]);
        assert_eq!(dom.parent(old), None);
        assert_eq!(dom.parent(kept), Some(old));
    }

    #[test]
    fn destroy_subtree_kills_every_id_in_it() {
        let (mut dom, _html, body) = scaffold();
        let a = dom.create_html_element("a");
        let doomed = dom.create_html_element("doomed");
        dom.append_child(body, a).unwrap();
        dom.append_child(body, doomed).unwrap();
        let child = dom.create_html_element("child");
        dom.append_child(doomed, child).unwrap();
        let grandchild = dom.create_text("bye");
        dom.append_child(child, grandchild).unwrap();

        let before = dom.live_count();
        dom.destroy_subtree(doomed).unwrap();
        dom.assert_invariants();

        assert_eq!(dom.live_count(), before - 3);
        for id in [doomed, child, grandchild] {
            assert!(!dom.is_alive(id));
        }
        assert_eq!(kids(&dom, body), vec![a]);
        assert_eq!(dom.last_child(body), Some(a));

        // Slots are never recycled, so a fresh node gets a fresh id and a
        // stale handle stays dead rather than aliasing the new node.
        let fresh = dom.create_html_element("fresh");
        assert_ne!(fresh, doomed);
        assert!(!dom.is_alive(doomed));
    }

    // -- text ------------------------------------------------------------

    #[test]
    fn appended_text_merges_with_the_trailing_text_node() {
        let (mut dom, _html, body) = scaffold();
        let first = dom.append_text(body, "he").unwrap();
        let second = dom.append_text(body, "llo").unwrap();
        dom.assert_invariants();

        assert_eq!(first, second, "adjacent text must merge, not fragment");
        assert_eq!(dom.child_count(body), 1);
        assert_eq!(dom.data(first).unwrap(), &NodeData::Text("hello".into()));
    }

    #[test]
    fn text_does_not_merge_across_an_element() {
        let (mut dom, _html, body) = scaffold();
        let first = dom.append_text(body, "a").unwrap();
        let br = dom.create_html_element("br");
        dom.append_child(body, br).unwrap();
        let second = dom.append_text(body, "b").unwrap();
        dom.assert_invariants();

        assert_ne!(first, second);
        assert_eq!(kids(&dom, body), vec![first, br, second]);
        assert_eq!(dom.text_content(body), "ab");
    }

    #[test]
    fn inserted_text_merges_with_the_preceding_node_not_the_following() {
        let (mut dom, _html, body) = scaffold();
        let before = dom.append_text(body, "start").unwrap();
        let marker = dom.create_html_element("marker");
        dom.append_child(body, marker).unwrap();
        let after = dom.append_text(body, "end").unwrap();

        // Insert immediately before the marker: the preceding sibling is the
        // "start" text node, so the data joins that one.
        let landed = dom.insert_text(body, "-mid", Some(marker)).unwrap();
        dom.assert_invariants();

        assert_eq!(landed, before);
        assert_eq!(dom.data(before).unwrap(), &NodeData::Text("start-mid".into()));
        assert_eq!(dom.data(after).unwrap(), &NodeData::Text("end".into()));
        assert_eq!(kids(&dom, body), vec![before, marker, after]);
    }

    #[test]
    fn text_inserted_at_the_front_creates_a_node() {
        let (mut dom, _html, body) = scaffold();
        let existing = dom.append_text(body, "tail").unwrap();
        let new = dom.insert_text(body, "head", Some(existing)).unwrap();
        dom.assert_invariants();

        // Nothing precedes the insertion point, so no merge: a new node.
        assert_ne!(new, existing);
        assert_eq!(kids(&dom, body), vec![new, existing]);
        assert_eq!(dom.text_content(body), "headtail");
    }

    // -- attributes ------------------------------------------------------

    #[test]
    fn attribute_order_is_preserved() {
        let mut dom = Dom::new();
        let el = dom.create_html_element("div");
        dom.set_attribute(el, "id", "x").unwrap();
        dom.set_attribute(el, "class", "y").unwrap();
        dom.set_attribute(el, "data-z", "z").unwrap();

        let names: Vec<String> = dom
            .attributes(el)
            .unwrap()
            .iter()
            .map(|a| a.qualified_name())
            .collect();
        assert_eq!(names, vec!["id", "class", "data-z"]);

        // Overwriting must not move the attribute to the end.
        dom.set_attribute(el, "id", "changed").unwrap();
        let names: Vec<String> = dom
            .attributes(el)
            .unwrap()
            .iter()
            .map(|a| a.qualified_name())
            .collect();
        assert_eq!(names, vec!["id", "class", "data-z"]);
        assert_eq!(dom.get_attribute(el, "id").unwrap(), Some("changed"));
    }

    #[test]
    fn html_attribute_lookup_ignores_ascii_case() {
        let mut dom = Dom::new();
        let el = dom.create_html_element("div");
        dom.push_attribute(el, Attr::new("colspan", "2")).unwrap();

        for query in ["colspan", "COLSPAN", "ColSpan"] {
            assert_eq!(dom.get_attribute(el, query).unwrap(), Some("2"), "{}", query);
            assert!(dom.has_attribute(el, query).unwrap());
        }
        // Case-insensitive, not accent- or unicode-insensitive.
        assert_eq!(dom.get_attribute(el, "colspa").unwrap(), None);

        // And a case-varying set finds the existing attribute.
        dom.set_attribute(el, "COLSPAN", "3").unwrap();
        assert_eq!(dom.attributes(el).unwrap().len(), 1);
        assert_eq!(dom.get_attribute(el, "colspan").unwrap(), Some("3"));
    }

    #[test]
    fn foreign_attribute_lookup_is_case_sensitive() {
        let mut dom = Dom::new();
        let el = dom.create_element(Namespace::Svg, "clipPath");
        dom.push_attribute(el, Attr::new("clipPathUnits", "userSpaceOnUse"))
            .unwrap();

        assert_eq!(
            dom.get_attribute(el, "clipPathUnits").unwrap(),
            Some("userSpaceOnUse")
        );
        // On an SVG element these really are different attributes.
        assert_eq!(dom.get_attribute(el, "clippathunits").unwrap(), None);
        dom.set_attribute(el, "clippathunits", "other").unwrap();
        assert_eq!(dom.attributes(el).unwrap().len(), 2);
    }

    #[test]
    fn namespaced_attributes_match_on_the_qualified_name() {
        let mut dom = Dom::new();
        let el = dom.create_element(Namespace::Svg, "use");
        dom.push_attribute(
            el,
            Attr::namespaced(Namespace::XLink, "xlink", "href", "#icon"),
        )
        .unwrap();

        assert_eq!(dom.get_attribute(el, "xlink:href").unwrap(), Some("#icon"));
        assert_eq!(dom.get_attribute(el, "href").unwrap(), None);
        assert_eq!(
            dom.attributes(el).unwrap()[0].namespace,
            Some(Namespace::XLink)
        );
    }

    #[test]
    fn set_attribute_lowercases_new_names_on_html_elements_only() {
        let mut dom = Dom::new();
        let html = dom.create_html_element("div");
        dom.set_attribute(html, "DATA-Foo", "1").unwrap();
        assert_eq!(dom.attributes(html).unwrap()[0].local, "data-foo");

        let svg = dom.create_element(Namespace::Svg, "path");
        dom.set_attribute(svg, "pathLength", "1").unwrap();
        assert_eq!(dom.attributes(svg).unwrap()[0].local, "pathLength");
    }

    #[test]
    fn add_attribute_if_missing_does_not_clobber() {
        let mut dom = Dom::new();
        let el = dom.create_html_element("body");
        dom.set_attribute(el, "class", "original").unwrap();

        // The tree builder's second-<body>-tag merge: existing wins.
        assert!(!dom.add_attribute_if_missing(el, Attr::new("CLASS", "ignored")).unwrap());
        assert!(dom.add_attribute_if_missing(el, Attr::new("id", "added")).unwrap());

        assert_eq!(dom.get_attribute(el, "class").unwrap(), Some("original"));
        assert_eq!(dom.get_attribute(el, "id").unwrap(), Some("added"));
        assert_eq!(dom.attributes(el).unwrap().len(), 2);
    }

    #[test]
    fn remove_attribute_reports_whether_it_removed() {
        let mut dom = Dom::new();
        let el = dom.create_html_element("div");
        dom.set_attribute(el, "a", "1").unwrap();
        dom.set_attribute(el, "b", "2").unwrap();

        assert!(dom.remove_attribute(el, "A").unwrap());
        assert!(!dom.remove_attribute(el, "A").unwrap());
        let names: Vec<String> = dom
            .attributes(el)
            .unwrap()
            .iter()
            .map(|a| a.qualified_name())
            .collect();
        assert_eq!(names, vec!["b"]);
    }

    #[test]
    fn attribute_operations_reject_non_elements() {
        let mut dom = Dom::new();
        let text = dom.create_text("hi");
        assert_eq!(
            dom.get_attribute(text, "id"),
            Err(DomError::NotAnElement(text))
        );
        assert_eq!(
            dom.set_attribute(text, "id", "x"),
            Err(DomError::NotAnElement(text))
        );
    }

    // -- cloning ---------------------------------------------------------

    #[test]
    fn clone_shallow_copies_attributes_but_not_children() {
        let (mut dom, _html, body) = scaffold();
        let src = dom.create_html_element("b");
        dom.set_attribute(src, "class", "bold").unwrap();
        dom.append_child(body, src).unwrap();
        dom.append_text(src, "text").unwrap();

        let copy = dom.clone_shallow(src).unwrap();
        dom.assert_invariants();

        assert_ne!(copy, src);
        assert_eq!(dom.tag_name(copy), Some("b"));
        assert_eq!(dom.get_attribute(copy, "class").unwrap(), Some("bold"));
        assert!(!dom.has_children(copy));
        assert_eq!(dom.parent(copy), None, "clones start detached");
        // Mutating the clone must not touch the original.
        dom.set_attribute(copy, "class", "changed").unwrap();
        assert_eq!(dom.get_attribute(src, "class").unwrap(), Some("bold"));
    }

    #[test]
    fn clone_deep_copies_the_whole_subtree() {
        let (mut dom, _html, body) = scaffold();
        let src = dom.create_html_element("div");
        dom.append_child(body, src).unwrap();
        let p = dom.create_html_element("p");
        dom.append_child(src, p).unwrap();
        dom.append_text(p, "one").unwrap();
        let em = dom.create_html_element("em");
        dom.append_child(p, em).unwrap();
        dom.append_text(em, "two").unwrap();

        let copy = dom.clone_deep(src).unwrap();
        dom.assert_invariants();

        assert_eq!(dom.to_debug_string(copy), dom.to_debug_string(src));
        assert_eq!(dom.text_content(copy), "onetwo");
        assert_eq!(dom.parent(copy), None);
        // Distinct nodes throughout, not shared ids.
        let originals: Vec<NodeId> = dom.traverse(src).collect();
        let copies: Vec<NodeId> = dom.traverse(copy).collect();
        assert_eq!(originals.len(), copies.len());
        for (a, b) in originals.iter().zip(copies.iter()) {
            assert_ne!(a, b);
        }
    }

    // -- traversal -------------------------------------------------------

    #[test]
    fn preorder_is_document_order() {
        let mut dom = Dom::new();
        let root = dom.create_html_element("root");
        let a = dom.create_html_element("a");
        let a1 = dom.create_html_element("a1");
        let a2 = dom.create_html_element("a2");
        let b = dom.create_html_element("b");
        let b1 = dom.create_html_element("b1");
        dom.append_child(root, a).unwrap();
        dom.append_child(a, a1).unwrap();
        dom.append_child(a, a2).unwrap();
        dom.append_child(root, b).unwrap();
        dom.append_child(b, b1).unwrap();

        assert_eq!(
            dom.traverse(root).collect::<Vec<_>>(),
            vec![root, a, a1, a2, b, b1]
        );
        assert_eq!(
            dom.descendants(root).collect::<Vec<_>>(),
            vec![a, a1, a2, b, b1]
        );
        // Traversal is bounded by the subtree it started in.
        assert_eq!(dom.traverse(a).collect::<Vec<_>>(), vec![a, a1, a2]);
        assert_eq!(dom.traverse(b1).collect::<Vec<_>>(), vec![b1]);
    }

    #[test]
    fn child_indexing() {
        let (mut dom, _html, body) = scaffold();
        let a = dom.create_html_element("a");
        let b = dom.create_html_element("b");
        dom.append_child(body, a).unwrap();
        dom.append_child(body, b).unwrap();

        assert_eq!(dom.child_index(body, b), Some(1));
        assert_eq!(dom.child_at(body, 0), Some(a));
        assert_eq!(dom.child_at(body, 2), None);
        assert_eq!(dom.child_index(body, _html), None);
    }

    // -- integration: the shapes Phase 2 will actually produce -------------

    #[test]
    fn foster_parenting_shape() {
        // A stray character token inside a <table> is foster-parented: it is
        // inserted into the table's *parent*, immediately before the table,
        // rather than into the table itself.
        let (mut dom, _html, body) = scaffold();
        dom.append_text(body, "before").unwrap();
        let table = dom.create_html_element("table");
        dom.append_child(body, table).unwrap();
        let tbody = dom.create_html_element("tbody");
        dom.append_child(table, tbody).unwrap();

        let parent_of_table = dom.parent(table).unwrap();
        dom.insert_text(parent_of_table, "stray", Some(table)).unwrap();
        dom.assert_invariants();

        // Merged into the existing text node that precedes the table.
        assert_eq!(dom.child_count(body), 2);
        assert_eq!(
            dom.to_debug_string(body),
            "<body>beforestray<table><tbody></tbody></table></body>"
        );
    }

    #[test]
    fn adoption_agency_shape() {
        // The canonical misnested case: <p>1<b>2<i>3</p>4</i>5
        // Here we drive only the tree mutations the algorithm performs, to
        // prove the arena supports them: clone a formatting element, move a
        // whole subtree to a new parent, bulk-move children into the clone,
        // and append the clone back.
        let (mut dom, _html, body) = scaffold();

        // Start from the mis-parsed shape: <p>1<b>2<i>3</i></b></p>
        let p = dom.create_html_element("p");
        dom.append_child(body, p).unwrap();
        dom.append_text(p, "1").unwrap();
        let b = dom.create_html_element("b");
        dom.set_attribute(b, "class", "fmt").unwrap();
        dom.append_child(p, b).unwrap();
        dom.append_text(b, "2").unwrap();
        let i = dom.create_html_element("i");
        dom.append_child(b, i).unwrap();
        dom.append_text(i, "3").unwrap();

        assert_eq!(
            dom.to_debug_string(body),
            "<body><p>1<b class=\"fmt\">2<i>3</i></b></p></body>"
        );

        // formatting element = <b>, furthest block = <i>, common ancestor =
        // <p>. Step: create a clone of the formatting element.
        let b_clone = dom.clone_shallow(b).unwrap();
        // Step: take all the children of the furthest block and append them
        // to the clone.
        dom.move_children(i, b_clone, None).unwrap();
        // Step: append the clone to the furthest block.
        dom.append_child(i, b_clone).unwrap();
        // Step: insert the furthest block into the common ancestor at the
        // appropriate place -- here, after the (now childless) <b>.
        let after_b = dom.next_sibling(b);
        dom.move_subtree(i, p, after_b).unwrap();
        dom.assert_invariants();

        assert_eq!(
            dom.to_debug_string(body),
            "<body><p>1<b class=\"fmt\">2</b><i><b class=\"fmt\">3</b></i></p></body>"
        );
        // The clone carried the original's attributes, as the spec requires.
        assert_eq!(dom.get_attribute(b_clone, "class").unwrap(), Some("fmt"));
        assert_eq!(dom.parent(i), Some(p));
        assert_eq!(dom.parent(b_clone), Some(i));
        assert_eq!(dom.text_content(p), "123");
    }

    #[test]
    fn a_whole_small_document() {
        let mut dom = Dom::new();
        let doc = dom.document();
        let dt = dom.create_doctype("html", "", "");
        dom.append_child(doc, dt).unwrap();
        let html = dom.create_html_element("html");
        dom.append_child(doc, html).unwrap();
        let head = dom.create_html_element("head");
        let body = dom.create_html_element("body");
        dom.append_child(html, head).unwrap();
        dom.append_child(html, body).unwrap();
        let title = dom.create_html_element("title");
        dom.append_child(head, title).unwrap();
        dom.append_text(title, "Feet").unwrap();
        let comment = dom.create_comment(" hi ");
        dom.append_child(body, comment).unwrap();
        let h1 = dom.create_html_element("h1");
        dom.set_attribute(h1, "id", "top").unwrap();
        dom.append_child(body, h1).unwrap();
        dom.append_text(h1, "Hello").unwrap();
        dom.assert_invariants();

        assert_eq!(
            dom.to_debug_string(doc),
            "#document[<!DOCTYPE html><html><head><title>Feet</title></head>\
             <body><!-- hi --><h1 id=\"top\">Hello</h1></body></html>]"
        );
        assert_eq!(dom.text_content(doc), "FeetHello");
        assert_eq!(dom.data(dt).unwrap().node_type(), 10);
        assert_eq!(dom.data(comment).unwrap().node_type(), 8);
        assert_eq!(dom.data(h1).unwrap().node_type(), 1);
    }
}
