//! The HTML tree construction stage, WHATWG HTML §13.2.6.
//!
//! The tokenizer decides what the bytes *are*; this decides what tree they
//! *mean*, and almost all of the difficulty in parsing HTML lives here. The
//! spec expresses it as a state machine over 23 "insertion modes", plus three
//! algorithms that are not modes at all and do the real work:
//!
//! * **The appropriate place for inserting a node** ([`TreeBuilder::insertion_place`]),
//!   which is where *foster parenting* lives: content that has no business
//!   inside a `<table>` gets re-homed to just before the table instead.
//! * **Reconstructing the active formatting elements**
//!   ([`TreeBuilder::reconstruct_active_formatting`]), which is why `<b>` keeps
//!   applying across a `<p>` boundary that closed it.
//! * **The adoption agency algorithm** ([`TreeBuilder::adoption_agency`]),
//!   which untangles `<b>1<i>2</b>3</i>` into two `<i>` elements so that the
//!   "3" stays italic.
//!
//! Those three are the reason a hand-rolled parser gets 7 of 10 tree-construction
//! cases right and then stalls: each of them moves nodes that are *already in
//! the tree*, which an append-only parser has no vocabulary for. They are also
//! why Phase 1 built `insert_before`-with-a-reference, `move_subtree`,
//! `move_children` and `clone_shallow` before any of this existed.
//!
//! # Layering
//!
//! This module owns the parse; [`crate::domtree::Dom`] owns the tree. Nothing
//! here does its own link surgery.

use crate::domtree::{Attr, Dom, ElementData, Namespace, NodeData, NodeId};

use super::tokenizer::{DoctypeToken, State, TagAttr, TagToken, Token, Tokenizer};

// ---------------------------------------------------------------------------
// Insertion modes
// ---------------------------------------------------------------------------

/// The insertion modes of §13.2.6.4, in spec order.
///
/// There are 21, not the 23 of the older published text: the "customizable
/// select" revision deleted "in select" and "in select in table" and moved
/// their handling into "in body", so that a `<select>` is parsed like any
/// other element and can contain `<div>`, `<button>`, formatting elements and
/// foreign content. The vendored html5lib-tests fixtures assume the new
/// behaviour; see the note on the `select` rules in [`TreeBuilder::mode_in_body`].
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Mode {
    Initial,
    BeforeHtml,
    BeforeHead,
    InHead,
    InHeadNoscript,
    AfterHead,
    InBody,
    Text,
    InTable,
    InTableText,
    InCaption,
    InColumnGroup,
    InTableBody,
    InRow,
    InCell,
    InTemplate,
    AfterBody,
    InFrameset,
    AfterFrameset,
    AfterAfterBody,
    AfterAfterFrameset,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum QuirksMode {
    NoQuirks,
    LimitedQuirks,
    Quirks,
}

/// An entry in the list of active formatting elements.
///
/// The spec's entries carry "the token for which the element was created", so
/// that reconstruction can build a fresh element with the same attributes.
/// Since parsing never mutates a formatting element's attributes after
/// creation, the element in the arena *is* that record, and
/// [`Dom::clone_shallow`] is the "create an element for the token" step.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Formatting {
    Marker,
    Element(NodeId),
}

/// Which set of elements terminates a scope search. §13.2.4.2.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Scope {
    Standard,
    ListItem,
    Button,
    Table,
}

// ---------------------------------------------------------------------------
// Element name sets
// ---------------------------------------------------------------------------

/// The "special" category, §13.2.4.2. Membership decides when the adoption
/// agency algorithm stops walking and when an unmatched end tag is ignored.
///
/// `select` is deliberately absent. It left the category along with its
/// insertion modes: in `<font><select><option>a</option></font></select>` the
/// `</font>` must not find the select as a "furthest block" and clone the font
/// into it — the select is ordinary content of the font, and the font simply
/// closes around it.
const SPECIAL_HTML: &[&str] = &[
    "address", "applet", "area", "article", "aside", "base", "basefont", "bgsound", "blockquote",
    "body", "br", "button", "caption", "center", "col", "colgroup", "dd", "details", "dir", "div",
    "dl", "dt", "embed", "fieldset", "figcaption", "figure", "footer", "form", "frame", "frameset",
    "h1", "h2", "h3", "h4", "h5", "h6", "head", "header", "hgroup", "hr", "html", "iframe", "img",
    "input", "keygen", "li", "link", "listing", "main", "marquee", "menu", "meta", "nav",
    "noembed", "noframes", "noscript", "object", "ol", "p", "param", "plaintext", "pre", "script",
    "search", "section", "source", "style", "summary", "table", "tbody", "td",
    "template", "textarea", "tfoot", "th", "thead", "title", "tr", "track", "ul", "wbr", "xmp",
];

/// The "formatting" category, §13.2.4.3.
const FORMATTING: &[&str] = &[
    "a", "b", "big", "code", "em", "font", "i", "nobr", "s", "small", "strike", "strong", "tt", "u",
];

/// Elements that terminate every scope search regardless of which scope it is.
const SCOPE_BASE_HTML: &[&str] = &[
    "applet", "caption", "html", "table", "td", "th", "marquee", "object", "template",
];

const MATHML_TEXT_INTEGRATION: &[&str] = &["mi", "mo", "mn", "ms", "mtext"];

const IMPLIED_END_TAGS: &[&str] = &[
    "dd", "dt", "li", "optgroup", "option", "p", "rb", "rp", "rt", "rtc",
];

fn is_special(ns: Namespace, name: &str) -> bool {
    match ns {
        Namespace::Html => SPECIAL_HTML.contains(&name),
        Namespace::MathMl => MATHML_TEXT_INTEGRATION.contains(&name) || name == "annotation-xml",
        Namespace::Svg => matches!(name, "foreignObject" | "desc" | "title"),
        _ => false,
    }
}

fn is_formatting(name: &str) -> bool {
    FORMATTING.contains(&name)
}

// ---------------------------------------------------------------------------
// The tree builder
// ---------------------------------------------------------------------------

pub struct TreeBuilder {
    pub dom: Dom,
    tokenizer: Tokenizer,

    mode: Mode,
    original_mode: Mode,
    template_modes: Vec<Mode>,

    /// The stack of open elements. Index 0 is the *bottom* of the spec's
    /// stack (the `html` element); the last entry is the current node.
    open: Vec<NodeId>,
    /// The list of active formatting elements, oldest first.
    active: Vec<Formatting>,

    root: NodeId,
    head: Option<NodeId>,
    form: Option<NodeId>,

    /// Fragment parsing: the context element, and the root we parse into.
    context: Option<NodeId>,
    fragment: bool,

    scripting: bool,
    frameset_ok: bool,
    foster_parenting: bool,
    ignore_next_lf: bool,
    quirks: QuirksMode,

    /// §13.2.6.4.10 "in table text" buffers characters so that a run which
    /// turns out to be non-whitespace can be foster-parented as a unit.
    pending_table_text: String,
    pending_table_text_ws_only: bool,

    /// Parse errors, counted rather than named. See the note on
    /// `Tokenizer::errors`.
    pub errors: usize,

    stopped: bool,
}

impl TreeBuilder {
    /// A parser for a whole document.
    pub fn new(input: &str, scripting: bool) -> TreeBuilder {
        let dom = Dom::new();
        let root = dom.document();
        TreeBuilder {
            dom,
            tokenizer: Tokenizer::new(input),
            mode: Mode::Initial,
            original_mode: Mode::Initial,
            template_modes: Vec::new(),
            open: Vec::new(),
            active: Vec::new(),
            root,
            head: None,
            form: None,
            context: None,
            fragment: false,
            scripting,
            frameset_ok: true,
            foster_parenting: false,
            ignore_next_lf: false,
            quirks: QuirksMode::NoQuirks,
            pending_table_text: String::new(),
            pending_table_text_ws_only: true,
            errors: 0,
            stopped: false,
        }
    }

    /// The HTML fragment parsing algorithm, §13.2.7.
    ///
    /// `context_ns`/`context_name` name the element the markup is being parsed
    /// as if it were inside. The returned tree's fragment root is
    /// [`TreeBuilder::root`].
    pub fn new_fragment(
        input: &str,
        context_ns: Namespace,
        context_name: &str,
        scripting: bool,
    ) -> TreeBuilder {
        let mut tb = TreeBuilder::new(input, scripting);
        tb.fragment = true;

        // 1-3. A fresh Document, and the context element in it.
        let context = tb.dom.create_element(context_ns, context_name);
        tb.context = Some(context);

        // 4. The fragment we actually parse into, plus an `html` root that
        //    holds the stack up.
        let frag = tb.dom.create_fragment();
        tb.root = frag;
        let html = tb.dom.create_html_element("html");
        tb.dom.append_child(frag, html).expect("fresh fragment");
        tb.open.push(html);

        // 5. If the context is a template, push "in template" onto the stack
        //    of template insertion modes.
        if context_ns.is_html() && context_name == "template" {
            tb.template_modes.push(Mode::InTemplate);
        }

        // 6. Set the tokenizer state per the context element, so that e.g. a
        //    `<title>` context treats its content as RCDATA.
        if context_ns.is_html() {
            tb.tokenizer.state = match context_name {
                "title" | "textarea" => State::Rcdata,
                "style" | "xmp" | "iframe" | "noembed" | "noframes" => State::Rawtext,
                "script" => State::ScriptData,
                "noscript" if scripting => State::Rawtext,
                "plaintext" => State::Plaintext,
                _ => State::Data,
            };
            // Deliberately *not* seeding the last-start-tag name. An
            // "appropriate end tag token" is one matching the last start tag
            // this tokenizer emitted, and a fragment parse has emitted none —
            // so `innerHTML = "<!-- </script> -->"` on a script element keeps
            // the `</script>` as text, which is what browsers do.
        }

        // 7-8. Reset the insertion mode, and find the nearest enclosing form.
        tb.reset_insertion_mode();
        tb.form = tb.nearest_form_ancestor(context);
        tb
    }

    fn nearest_form_ancestor(&self, from: NodeId) -> Option<NodeId> {
        let mut cur = Some(from);
        while let Some(id) = cur {
            if self.dom.is_html_element(id, "form") {
                return Some(id);
            }
            cur = self.dom.parent(id);
        }
        None
    }

    /// The node everything was parsed into: the `Document` for a document
    /// parse, the fragment for a fragment parse.
    pub fn root(&self) -> NodeId {
        self.root
    }

    pub fn quirks_mode(&self) -> QuirksMode {
        self.quirks
    }

    /// Run to completion.
    pub fn parse(mut self) -> TreeBuilder {
        loop {
            self.tokenizer.in_foreign = self.adjusted_current_is_foreign();
            let token = self.tokenizer.next_token();
            let eof = token == Token::Eof;
            self.dispatch(token);
            if eof || self.stopped {
                break;
            }
        }
        self.errors += self.tokenizer.errors;

        // §13.2.7 step 14: a fragment parse returns *the children of the root
        // element*, not the scaffolding `html` element the stack needed. Hoist
        // them onto the fragment and drop the scaffold.
        if self.fragment {
            if let Some(html) = self.dom.first_child(self.root) {
                let root = self.root;
                let _ = self.dom.move_children(html, root, None);
                let _ = self.dom.remove(html);
            }
        }
        self
    }

    #[inline]
    fn error(&mut self) {
        self.errors += 1;
    }

    // -- stack helpers -------------------------------------------------------

    fn current(&self) -> NodeId {
        *self.open.last().expect("stack of open elements is empty")
    }

    fn current_opt(&self) -> Option<NodeId> {
        self.open.last().copied()
    }

    /// §13.2.6.5: the current node, except that in a fragment parse with only
    /// the root on the stack it is the context element.
    fn adjusted_current(&self) -> Option<NodeId> {
        if self.fragment && self.open.len() == 1 {
            self.context
        } else {
            self.current_opt()
        }
    }

    fn adjusted_current_is_foreign(&self) -> bool {
        match self.adjusted_current() {
            Some(id) => !self.dom.namespace(id).unwrap_or(Namespace::Html).is_html(),
            None => false,
        }
    }

    fn name_of(&self, id: NodeId) -> &str {
        self.dom.tag_name(id).unwrap_or("")
    }

    fn ns_of(&self, id: NodeId) -> Namespace {
        self.dom.namespace(id).unwrap_or(Namespace::Html)
    }

    fn is_html(&self, id: NodeId, name: &str) -> bool {
        self.dom.is_html_element(id, name)
    }

    fn stack_index(&self, id: NodeId) -> Option<usize> {
        self.open.iter().rposition(|&n| n == id)
    }

    fn pop(&mut self) -> Option<NodeId> {
        self.open.pop()
    }

    fn pop_until_named(&mut self, name: &str) {
        while let Some(id) = self.open.pop() {
            if self.is_html(id, name) {
                break;
            }
        }
    }

    fn pop_until_one_of(&mut self, names: &[&str]) {
        while let Some(id) = self.open.pop() {
            if self.ns_of(id).is_html() && names.contains(&self.name_of(id)) {
                break;
            }
        }
    }

    fn pop_until_node(&mut self, target: NodeId) {
        while let Some(id) = self.open.pop() {
            if id == target {
                break;
            }
        }
    }

    /// §13.2.4.2.
    fn has_in_scope(&self, target: &str, scope: Scope) -> bool {
        self.has_in_scope_by(scope, &mut |b: &TreeBuilder, id| {
            b.ns_of(id).is_html() && b.name_of(id) == target
        })
    }

    fn has_node_in_scope(&self, target: NodeId, scope: Scope) -> bool {
        self.has_in_scope_by(scope, &mut |_b, id| id == target)
    }

    fn has_in_scope_by(
        &self,
        scope: Scope,
        matches: &mut dyn FnMut(&TreeBuilder, NodeId) -> bool,
    ) -> bool {
        for &id in self.open.iter().rev() {
            if matches(self, id) {
                return true;
            }
            if self.terminates_scope(id, scope) {
                return false;
            }
        }
        false
    }

    fn terminates_scope(&self, id: NodeId, scope: Scope) -> bool {
        let ns = self.ns_of(id);
        let name = self.name_of(id);
        match scope {
            Scope::Table => ns.is_html() && matches!(name, "html" | "table" | "template"),
            _ => {
                if ns.is_html() {
                    if SCOPE_BASE_HTML.contains(&name) {
                        return true;
                    }
                    match scope {
                        Scope::ListItem => matches!(name, "ol" | "ul"),
                        Scope::Button => name == "button",
                        _ => false,
                    }
                } else {
                    match ns {
                        Namespace::MathMl => {
                            MATHML_TEXT_INTEGRATION.contains(&name) || name == "annotation-xml"
                        }
                        Namespace::Svg => matches!(name, "foreignObject" | "desc" | "title"),
                        _ => false,
                    }
                }
            }
        }
    }

    /// §13.2.6.3, with `except` naming a tag that should *not* be closed.
    fn generate_implied_end_tags(&mut self, except: Option<&str>) {
        while let Some(id) = self.current_opt() {
            if !self.ns_of(id).is_html() {
                break;
            }
            let name = self.name_of(id);
            if Some(name) == except || !IMPLIED_END_TAGS.contains(&name) {
                break;
            }
            self.open.pop();
        }
    }

    /// "generate all implied end tags thoroughly": also closes the table-cell
    /// and ruby containers, used when closing `<template>` and cells.
    fn generate_implied_end_tags_thoroughly(&mut self) {
        const THOROUGH: &[&str] = &[
            "caption", "colgroup", "dd", "dt", "li", "optgroup", "option", "p", "rb", "rp", "rt",
            "rtc", "tbody", "td", "tfoot", "th", "thead", "tr",
        ];
        while let Some(id) = self.current_opt() {
            if !(self.ns_of(id).is_html() && THOROUGH.contains(&self.name_of(id))) {
                break;
            }
            self.open.pop();
        }
    }

    // -- the list of active formatting elements ------------------------------

    fn push_active_marker(&mut self) {
        self.active.push(Formatting::Marker);
    }

    fn clear_active_to_marker(&mut self) {
        while let Some(entry) = self.active.pop() {
            if entry == Formatting::Marker {
                break;
            }
        }
    }

    fn active_index(&self, id: NodeId) -> Option<usize> {
        self.active
            .iter()
            .rposition(|e| *e == Formatting::Element(id))
    }

    /// Push onto the list of active formatting elements, applying the Noah's
    /// Ark clause: at most three entries with the same name, namespace and
    /// attributes may sit after the last marker, and the *earliest* is the one
    /// evicted.
    ///
    /// Without this, `<b><b><b><b>...` grows the reconstruction work
    /// quadratically and produces a nesting depth that no real document has.
    fn push_active_formatting(&mut self, element: NodeId) {
        let start = self
            .active
            .iter()
            .rposition(|e| *e == Formatting::Marker)
            .map(|i| i + 1)
            .unwrap_or(0);

        let mut matches: Vec<usize> = Vec::new();
        for i in start..self.active.len() {
            if let Formatting::Element(other) = self.active[i] {
                if self.same_for_noahs_ark(other, element) {
                    matches.push(i);
                }
            }
        }
        if matches.len() >= 3 {
            let earliest = matches[0];
            self.active.remove(earliest);
        }
        self.active.push(Formatting::Element(element));
    }

    fn same_for_noahs_ark(&self, a: NodeId, b: NodeId) -> bool {
        let (Ok(ea), Ok(eb)) = (self.dom.element(a), self.dom.element(b)) else {
            return false;
        };
        if ea.namespace != eb.namespace || ea.name != eb.name || ea.attrs.len() != eb.attrs.len() {
            return false;
        }
        // Attribute *order* is not part of the comparison; the spec compares
        // them as sets.
        ea.attrs.iter().all(|x| {
            eb.attrs
                .iter()
                .any(|y| y.namespace == x.namespace && y.local == x.local && y.value == x.value)
        })
    }

    /// §13.2.4.3 "reconstruct the active formatting elements".
    ///
    /// This is what makes `<b>a<p>b</p>` render "b" in bold: the `<b>` was
    /// popped off the stack when `<p>` opened, but it is still *active*, so a
    /// fresh `<b>` is created inside the `<p>`.
    fn reconstruct_active_formatting(&mut self) {
        let Some(&last) = self.active.last() else {
            return;
        };
        match last {
            Formatting::Marker => return,
            Formatting::Element(id) if self.stack_index(id).is_some() => return,
            _ => {}
        }

        let mut i = self.active.len() - 1;
        // Rewind to the first entry that is a marker or still open.
        loop {
            if i == 0 {
                break;
            }
            i -= 1;
            match self.active[i] {
                Formatting::Marker => {
                    i += 1;
                    break;
                }
                Formatting::Element(id) if self.stack_index(id).is_some() => {
                    i += 1;
                    break;
                }
                _ => {}
            }
        }

        // Advance: recreate every entry from here to the end.
        while i < self.active.len() {
            let Formatting::Element(old) = self.active[i] else {
                unreachable!("markers terminate the rewind")
            };
            let fresh = self.dom.clone_shallow(old).expect("live formatting element");
            self.insert_node_at_appropriate_place(fresh);
            self.open.push(fresh);
            self.active[i] = Formatting::Element(fresh);
            i += 1;
        }
    }

    // -- insertion -----------------------------------------------------------

    /// §13.2.6.1 "the appropriate place for inserting a node", returning
    /// `(parent, before)` in the shape [`Dom::insert_before`] wants.
    ///
    /// The foster-parenting branch is the whole reason this is a function and
    /// not just "append to the current node".
    fn insertion_place(&self, override_target: Option<NodeId>) -> (NodeId, Option<NodeId>) {
        let target = override_target.unwrap_or_else(|| self.current());

        let fostering = self.foster_parenting
            && self.ns_of(target).is_html()
            && matches!(self.name_of(target), "table" | "tbody" | "tfoot" | "thead" | "tr");

        let (parent, before) = if fostering {
            let last_template = self
                .open
                .iter()
                .rposition(|&id| self.is_html(id, "template"));
            let last_table = self.open.iter().rposition(|&id| self.is_html(id, "table"));

            match (last_template, last_table) {
                (Some(t), None) => (self.template_contents(self.open[t]), None),
                (Some(t), Some(tb)) if t > tb => (self.template_contents(self.open[t]), None),
                (_, None) => {
                    // Fragment case: no table on the stack at all.
                    (self.open[0], None)
                }
                (_, Some(tb)) => {
                    let table = self.open[tb];
                    match self.dom.parent(table) {
                        Some(p) => (p, Some(table)),
                        // A table that is not in the tree yet (it was itself
                        // foster-parented out of another table); fall back to
                        // the element above it on the stack.
                        None => (self.open[tb.saturating_sub(1)], None),
                    }
                }
            }
        } else {
            (target, None)
        };

        // "If the adjusted insertion location is inside a template element,
        // let it instead be inside the template element's template contents."
        if before.is_none() && self.is_html(parent, "template") {
            return (self.template_contents(parent), None);
        }
        (parent, before)
    }

    /// A `<template>`'s contents fragment, which the tree builder keeps as the
    /// template element's single child.
    fn template_contents(&self, template: NodeId) -> NodeId {
        self.dom
            .first_child(template)
            .filter(|&c| matches!(self.dom.data(c), Ok(NodeData::Fragment)))
            .unwrap_or(template)
    }

    fn insert_node_at_appropriate_place(&mut self, node: NodeId) {
        let (parent, before) = self.insertion_place(None);
        let _ = self.dom.insert_before(parent, node, before);
    }

    /// §13.2.6.1 "create an element for a token", limited to what this parser
    /// needs: no custom element reactions, no `is` handling.
    fn create_element_for(&mut self, ns: Namespace, tag: &TagToken) -> NodeId {
        let attrs = tag
            .attrs
            .iter()
            .map(|a| Attr::new(a.name.clone(), a.value.clone()))
            .collect();
        self.dom.create_element_with_attrs(ns, tag.name.clone(), attrs)
    }

    fn insert_html_element(&mut self, tag: &TagToken) -> NodeId {
        let el = self.create_element_for(Namespace::Html, tag);
        self.insert_node_at_appropriate_place(el);
        self.open.push(el);
        el
    }

    /// Insert an element without pushing it onto the stack. Used by the void
    /// elements, which are popped immediately anyway.
    fn insert_void_element(&mut self, tag: &TagToken) -> NodeId {
        let el = self.insert_html_element(tag);
        self.open.pop();
        el
    }

    fn insert_foreign_element(&mut self, ns: Namespace, tag: &TagToken) -> NodeId {
        let mut adjusted = tag.clone();
        if ns == Namespace::Svg {
            adjusted.name = adjust_svg_tag_name(&adjusted.name);
        }
        let attrs = adjust_attributes(ns, &adjusted.attrs);
        let el = self
            .dom
            .create_element_with_attrs(ns, adjusted.name.clone(), attrs);
        self.insert_node_at_appropriate_place(el);
        self.open.push(el);
        el
    }

    fn insert_character(&mut self, c: char) {
        let (parent, before) = self.insertion_place(None);
        // Text may not be inserted directly into a Document node; the spec's
        // insertion modes route it away long before here, but a mis-routed
        // character should be dropped, not panic.
        if matches!(self.dom.data(parent), Ok(NodeData::Document)) {
            return;
        }
        let mut buf = [0u8; 4];
        let _ = self.dom.insert_text(parent, c.encode_utf8(&mut buf), before);
    }

    fn insert_text(&mut self, text: &str) {
        if text.is_empty() {
            return;
        }
        let (parent, before) = self.insertion_place(None);
        if matches!(self.dom.data(parent), Ok(NodeData::Document)) {
            return;
        }
        let _ = self.dom.insert_text(parent, text, before);
    }

    /// Insert a comment or a processing instruction.
    ///
    /// The two share a path because every insertion mode puts them in exactly
    /// the same place — they are the only tokens that are inserted verbatim,
    /// wherever they appear, without touching the stack of open elements.
    fn insert_markup(&mut self, token: &Token, target: Option<NodeId>) {
        let node = match token {
            Token::Comment(data) => self.dom.create_comment(data.clone()),
            Token::ProcessingInstruction { target, data } => self
                .dom
                .create_processing_instruction(target.clone(), data.clone()),
            _ => return,
        };
        match target {
            Some(parent) => {
                let _ = self.dom.append_child(parent, node);
            }
            None => self.insert_node_at_appropriate_place(node),
        }
    }

    // -- mode plumbing -------------------------------------------------------

    fn switch_to(&mut self, mode: Mode) {
        self.mode = mode;
    }

    /// §13.2.6.1 "reset the insertion mode appropriately".
    fn reset_insertion_mode(&mut self) {
        let mut last = false;
        for i in (0..self.open.len()).rev() {
            let mut node = self.open[i];
            if i == 0 {
                last = true;
                if let Some(ctx) = self.context {
                    node = ctx;
                }
            }
            if !self.ns_of(node).is_html() {
                if last {
                    self.mode = Mode::InBody;
                    return;
                }
                continue;
            }
            let name = self.name_of(node).to_string();
            let mode = match name.as_str() {
                // No "select" step: since the select insertion modes were
                // deleted, a select on the stack is transparent here and the
                // search continues into whatever encloses it.
                "td" | "th" if !last => Mode::InCell,
                "tr" => Mode::InRow,
                "tbody" | "thead" | "tfoot" => Mode::InTableBody,
                "caption" => Mode::InCaption,
                "colgroup" => Mode::InColumnGroup,
                "table" => Mode::InTable,
                "template" => *self.template_modes.last().unwrap_or(&Mode::InBody),
                "head" if !last => Mode::InHead,
                "body" => Mode::InBody,
                "frameset" => Mode::InFrameset,
                "html" => {
                    if self.head.is_none() {
                        Mode::BeforeHead
                    } else {
                        Mode::AfterHead
                    }
                }
                _ if last => Mode::InBody,
                _ => continue,
            };
            self.mode = mode;
            return;
        }
        self.mode = Mode::InBody;
    }

    /// Switch to RAWTEXT or RCDATA and remember where to come back to.
    fn parse_text(&mut self, tag: &TagToken, state: State) {
        self.insert_html_element(tag);
        self.tokenizer.state = state;
        self.original_mode = self.mode;
        self.switch_to(Mode::Text);
    }

    // -- dispatch ------------------------------------------------------------

    /// §13.2.6.5's tree construction dispatcher: HTML rules or foreign rules?
    fn dispatch(&mut self, token: Token) {
        if self.ignore_next_lf {
            self.ignore_next_lf = false;
            if token == Token::Character('\n') {
                return;
            }
        }

        let use_html_rules = match self.adjusted_current() {
            None => true,
            Some(node) => {
                let ns = self.ns_of(node);
                if ns.is_html() {
                    true
                } else if self.is_mathml_text_integration_point(node) {
                    match &token {
                        Token::StartTag(t) => !matches!(t.name.as_str(), "mglyph" | "malignmark"),
                        Token::Character(_) => true,
                        _ => false,
                    }
                } else if ns == Namespace::MathMl
                    && self.name_of(node) == "annotation-xml"
                    && matches!(&token, Token::StartTag(t) if t.name == "svg")
                {
                    true
                } else if self.is_html_integration_point(node) {
                    matches!(&token, Token::StartTag(_) | Token::Character(_))
                } else {
                    token == Token::Eof
                }
            }
        };

        if use_html_rules {
            self.process(token);
        } else {
            self.foreign_content(token);
        }
    }

    fn is_mathml_text_integration_point(&self, node: NodeId) -> bool {
        self.ns_of(node) == Namespace::MathMl && MATHML_TEXT_INTEGRATION.contains(&self.name_of(node))
    }

    fn is_html_integration_point(&self, node: NodeId) -> bool {
        match self.ns_of(node) {
            Namespace::MathMl => {
                self.name_of(node) == "annotation-xml"
                    && self
                        .dom
                        .get_attribute(node, "encoding")
                        .ok()
                        .flatten()
                        .map(|v| {
                            v.eq_ignore_ascii_case("text/html")
                                || v.eq_ignore_ascii_case("application/xhtml+xml")
                        })
                        .unwrap_or(false)
            }
            Namespace::Svg => matches!(self.name_of(node), "foreignObject" | "desc" | "title"),
            _ => false,
        }
    }

    /// Run the token through the current insertion mode.
    fn process(&mut self, token: Token) {
        match self.mode {
            Mode::Initial => self.mode_initial(token),
            Mode::BeforeHtml => self.mode_before_html(token),
            Mode::BeforeHead => self.mode_before_head(token),
            Mode::InHead => self.mode_in_head(token),
            Mode::InHeadNoscript => self.mode_in_head_noscript(token),
            Mode::AfterHead => self.mode_after_head(token),
            Mode::InBody => self.mode_in_body(token),
            Mode::Text => self.mode_text(token),
            Mode::InTable => self.mode_in_table(token),
            Mode::InTableText => self.mode_in_table_text(token),
            Mode::InCaption => self.mode_in_caption(token),
            Mode::InColumnGroup => self.mode_in_column_group(token),
            Mode::InTableBody => self.mode_in_table_body(token),
            Mode::InRow => self.mode_in_row(token),
            Mode::InCell => self.mode_in_cell(token),
            Mode::InTemplate => self.mode_in_template(token),
            Mode::AfterBody => self.mode_after_body(token),
            Mode::InFrameset => self.mode_in_frameset(token),
            Mode::AfterFrameset => self.mode_after_frameset(token),
            Mode::AfterAfterBody => self.mode_after_after_body(token),
            Mode::AfterAfterFrameset => self.mode_after_after_frameset(token),
        }
    }

    fn reprocess(&mut self, mode: Mode, token: Token) {
        self.switch_to(mode);
        self.process(token);
    }

    // =======================================================================
    // §13.2.6.4.1 "initial"
    // =======================================================================

    fn mode_initial(&mut self, token: Token) {
        match token {
            Token::Character(c) if is_ws(c) => {}
            Token::Comment(_) | Token::ProcessingInstruction { .. } => {
                let doc = self.root;
                self.insert_markup(&token, Some(doc));
            }
            Token::Doctype(d) => {
                let name = d.name.clone().unwrap_or_default();
                if name != "html"
                    || d.public_id.is_some()
                    || d.system_id.as_deref().is_some_and(|s| s != "about:legacy-compat")
                {
                    self.error();
                }
                // The doctype only ever parents to the Document, which is what
                // Phase 1 left to the tree builder to enforce.
                let node = self.dom.create_doctype(
                    name,
                    d.public_id.clone().unwrap_or_default(),
                    d.system_id.clone().unwrap_or_default(),
                );
                if matches!(self.dom.data(self.root), Ok(NodeData::Document)) {
                    let _ = self.dom.append_child(self.root, node);
                }
                self.quirks = quirks_from_doctype(&d);
                self.switch_to(Mode::BeforeHtml);
            }
            other => {
                if !self.fragment {
                    self.error();
                    self.quirks = QuirksMode::Quirks;
                }
                self.reprocess(Mode::BeforeHtml, other);
            }
        }
    }

    // =======================================================================
    // §13.2.6.4.2 "before html"
    // =======================================================================

    fn mode_before_html(&mut self, token: Token) {
        match token {
            Token::Doctype(_) => self.error(),
            Token::Comment(_) | Token::ProcessingInstruction { .. } => {
                let doc = self.root;
                self.insert_markup(&token, Some(doc));
            }
            Token::Character(c) if is_ws(c) => {}
            Token::StartTag(ref t) if t.name == "html" => {
                let el = self.create_element_for(Namespace::Html, t);
                let _ = self.dom.append_child(self.root, el);
                self.open.push(el);
                self.switch_to(Mode::BeforeHead);
            }
            Token::EndTag(ref t) if !matches!(t.name.as_str(), "head" | "body" | "html" | "br") => {
                self.error();
            }
            other => {
                let el = self.dom.create_html_element("html");
                let _ = self.dom.append_child(self.root, el);
                self.open.push(el);
                self.reprocess(Mode::BeforeHead, other);
            }
        }
    }

    // =======================================================================
    // §13.2.6.4.3 "before head"
    // =======================================================================

    fn mode_before_head(&mut self, token: Token) {
        match token {
            Token::Character(c) if is_ws(c) => {}
            Token::Comment(_) | Token::ProcessingInstruction { .. } => self.insert_markup(&token, None),
            Token::Doctype(_) => self.error(),
            Token::StartTag(ref t) if t.name == "html" => self.mode_in_body(token.clone()),
            Token::StartTag(ref t) if t.name == "head" => {
                let head = self.insert_html_element(t);
                self.head = Some(head);
                self.switch_to(Mode::InHead);
            }
            Token::EndTag(ref t) if !matches!(t.name.as_str(), "head" | "body" | "html" | "br") => {
                self.error();
            }
            other => {
                let tag = TagToken {
                    name: "head".into(),
                    attrs: Vec::new(),
                    self_closing: false,
                };
                let head = self.insert_html_element(&tag);
                self.head = Some(head);
                self.reprocess(Mode::InHead, other);
            }
        }
    }

    // =======================================================================
    // §13.2.6.4.4 "in head"
    // =======================================================================

    fn mode_in_head(&mut self, token: Token) {
        match token {
            Token::Character(c) if is_ws(c) => self.insert_character(c),
            Token::Comment(_) | Token::ProcessingInstruction { .. } => self.insert_markup(&token, None),
            Token::Doctype(_) => self.error(),
            Token::StartTag(ref t) if t.name == "html" => self.mode_in_body(token.clone()),
            Token::StartTag(ref t)
                if matches!(t.name.as_str(), "base" | "basefont" | "bgsound" | "link") =>
            {
                self.insert_void_element(t);
            }
            Token::StartTag(ref t) if t.name == "meta" => {
                self.insert_void_element(t);
            }
            Token::StartTag(ref t) if t.name == "title" => {
                let t = t.clone();
                self.parse_text(&t, State::Rcdata);
            }
            Token::StartTag(ref t)
                if t.name == "noscript" && self.scripting
                    || matches!(t.name.as_str(), "noframes" | "style") =>
            {
                let t = t.clone();
                self.parse_text(&t, State::Rawtext);
            }
            Token::StartTag(ref t) if t.name == "noscript" => {
                self.insert_html_element(t);
                self.switch_to(Mode::InHeadNoscript);
            }
            Token::StartTag(ref t) if t.name == "script" => {
                let t = t.clone();
                self.parse_text(&t, State::ScriptData);
            }
            Token::EndTag(ref t) if t.name == "head" => {
                self.open.pop();
                self.switch_to(Mode::AfterHead);
            }
            Token::StartTag(ref t) if t.name == "template" => {
                let t = t.clone();
                let el = self.insert_html_element(&t);
                let contents = self.dom.create_fragment();
                let _ = self.dom.append_child(el, contents);
                self.push_active_marker();
                self.frameset_ok = false;
                self.switch_to(Mode::InTemplate);
                self.template_modes.push(Mode::InTemplate);
            }
            Token::EndTag(ref t) if t.name == "template" => {
                if !self.open.iter().any(|&id| self.is_html(id, "template")) {
                    self.error();
                    return;
                }
                self.generate_implied_end_tags_thoroughly();
                if !self.is_html(self.current(), "template") {
                    self.error();
                }
                self.pop_until_named("template");
                self.clear_active_to_marker();
                self.template_modes.pop();
                self.reset_insertion_mode();
            }
            Token::EndTag(ref t) if matches!(t.name.as_str(), "body" | "html" | "br") => {
                self.open.pop();
                self.reprocess(Mode::AfterHead, token.clone());
            }
            Token::StartTag(ref t) if t.name == "head" => self.error(),
            Token::EndTag(_) => self.error(),
            other => {
                self.open.pop();
                self.reprocess(Mode::AfterHead, other);
            }
        }
    }

    // =======================================================================
    // §13.2.6.4.5 "in head noscript"
    // =======================================================================

    fn mode_in_head_noscript(&mut self, token: Token) {
        match token {
            Token::Doctype(_) => self.error(),
            Token::StartTag(ref t) if t.name == "html" => self.mode_in_body(token.clone()),
            Token::EndTag(ref t) if t.name == "noscript" => {
                self.open.pop();
                self.switch_to(Mode::InHead);
            }
            Token::Character(c) if is_ws(c) => self.mode_in_head(Token::Character(c)),
            Token::Comment(_) | Token::ProcessingInstruction { .. } => self.mode_in_head(token),
            Token::StartTag(ref t)
                if matches!(
                    t.name.as_str(),
                    "basefont" | "bgsound" | "link" | "meta" | "noframes" | "style"
                ) =>
            {
                self.mode_in_head(token.clone());
            }
            Token::EndTag(ref t) if t.name == "br" => {
                self.error();
                self.open.pop();
                self.reprocess(Mode::InHead, token.clone());
            }
            Token::StartTag(ref t) if matches!(t.name.as_str(), "head" | "noscript") => {
                self.error();
            }
            Token::EndTag(_) => self.error(),
            other => {
                self.error();
                self.open.pop();
                self.reprocess(Mode::InHead, other);
            }
        }
    }

    // =======================================================================
    // §13.2.6.4.6 "after head"
    // =======================================================================

    fn mode_after_head(&mut self, token: Token) {
        match token {
            Token::Character(c) if is_ws(c) => self.insert_character(c),
            Token::Comment(_) | Token::ProcessingInstruction { .. } => self.insert_markup(&token, None),
            Token::Doctype(_) => self.error(),
            Token::StartTag(ref t) if t.name == "html" => self.mode_in_body(token.clone()),
            Token::StartTag(ref t) if t.name == "body" => {
                self.insert_html_element(t);
                self.frameset_ok = false;
                self.switch_to(Mode::InBody);
            }
            Token::StartTag(ref t) if t.name == "frameset" => {
                self.insert_html_element(t);
                self.switch_to(Mode::InFrameset);
            }
            Token::StartTag(ref t)
                if matches!(
                    t.name.as_str(),
                    "base"
                        | "basefont"
                        | "bgsound"
                        | "link"
                        | "meta"
                        | "noframes"
                        | "script"
                        | "style"
                        | "template"
                        | "title"
                ) =>
            {
                // "Push the node pointed to by the head element pointer onto
                // the stack of open elements" so `in head` finds it, then
                // remove it again.
                self.error();
                let head = self.head.expect("head pointer set by `before head`");
                self.open.push(head);
                self.mode_in_head(token.clone());
                if let Some(pos) = self.open.iter().rposition(|&id| id == head) {
                    self.open.remove(pos);
                }
            }
            Token::EndTag(ref t) if t.name == "template" => self.mode_in_head(token.clone()),
            Token::EndTag(ref t) if matches!(t.name.as_str(), "body" | "html" | "br") => {
                self.after_head_default(token.clone())
            }
            Token::StartTag(ref t) if t.name == "head" => self.error(),
            Token::EndTag(_) => self.error(),
            other => self.after_head_default(other),
        }
    }

    fn after_head_default(&mut self, token: Token) {
        let tag = TagToken {
            name: "body".into(),
            attrs: Vec::new(),
            self_closing: false,
        };
        self.insert_html_element(&tag);
        self.reprocess(Mode::InBody, token);
    }

    // =======================================================================
    // §13.2.6.4.7 "in body" -- the big one
    // =======================================================================

    fn mode_in_body(&mut self, token: Token) {
        match token {
            Token::Character('\0') => self.error(),
            Token::Character(c) => {
                self.reconstruct_active_formatting();
                self.insert_character(c);
                if !is_ws(c) {
                    self.frameset_ok = false;
                }
            }
            Token::Comment(_) | Token::ProcessingInstruction { .. } => self.insert_markup(&token, None),
            Token::Doctype(_) => self.error(),

            Token::StartTag(ref t) if t.name == "html" => {
                self.error();
                if self.open.iter().any(|&id| self.is_html(id, "template")) {
                    return;
                }
                let top = self.open[0];
                for a in &t.attrs {
                    let _ = self
                        .dom
                        .add_attribute_if_missing(top, Attr::new(a.name.clone(), a.value.clone()));
                }
            }

            Token::StartTag(ref t)
                if matches!(
                    t.name.as_str(),
                    "base"
                        | "basefont"
                        | "bgsound"
                        | "link"
                        | "meta"
                        | "noframes"
                        | "script"
                        | "style"
                        | "template"
                        | "title"
                ) =>
            {
                self.mode_in_head(token.clone())
            }
            Token::EndTag(ref t) if t.name == "template" => self.mode_in_head(token.clone()),

            Token::StartTag(ref t) if t.name == "body" => {
                self.error();
                let has_template = self.open.iter().any(|&id| self.is_html(id, "template"));
                if self.open.len() < 2 || !self.is_html(self.open[1], "body") || has_template {
                    return;
                }
                self.frameset_ok = false;
                let body = self.open[1];
                for a in &t.attrs {
                    let _ = self
                        .dom
                        .add_attribute_if_missing(body, Attr::new(a.name.clone(), a.value.clone()));
                }
            }

            Token::StartTag(ref t) if t.name == "frameset" => {
                self.error();
                if self.open.len() < 2 || !self.is_html(self.open[1], "body") || !self.frameset_ok {
                    return;
                }
                let body = self.open[1];
                if let Some(parent) = self.dom.parent(body) {
                    let _ = self.dom.remove(body);
                    let _ = parent; // the body simply leaves the tree
                }
                self.open.truncate(1);
                self.insert_html_element(t);
                self.switch_to(Mode::InFrameset);
            }

            Token::Eof => {
                if !self.template_modes.is_empty() {
                    self.mode_in_template(Token::Eof);
                    return;
                }
                self.check_body_end_errors();
                self.stopped = true;
            }

            Token::EndTag(ref t) if t.name == "body" => {
                if !self.has_in_scope("body", Scope::Standard) {
                    self.error();
                    return;
                }
                self.check_body_end_errors();
                self.switch_to(Mode::AfterBody);
            }
            Token::EndTag(ref t) if t.name == "html" => {
                if !self.has_in_scope("body", Scope::Standard) {
                    self.error();
                    return;
                }
                self.check_body_end_errors();
                self.reprocess(Mode::AfterBody, token.clone());
            }

            Token::StartTag(ref t)
                if matches!(
                    t.name.as_str(),
                    "address"
                        | "article"
                        | "aside"
                        | "blockquote"
                        | "center"
                        | "details"
                        | "dialog"
                        | "dir"
                        | "div"
                        | "dl"
                        | "fieldset"
                        | "figcaption"
                        | "figure"
                        | "footer"
                        | "header"
                        | "hgroup"
                        | "main"
                        | "menu"
                        | "nav"
                        | "ol"
                        | "p"
                        | "search"
                        | "section"
                        | "summary"
                        | "ul"
                ) =>
            {
                if self.has_in_scope("p", Scope::Button) {
                    self.close_p();
                }
                self.insert_html_element(t);
            }

            Token::StartTag(ref t) if matches!(t.name.as_str(), "h1" | "h2" | "h3" | "h4" | "h5" | "h6") => {
                if self.has_in_scope("p", Scope::Button) {
                    self.close_p();
                }
                let cur = self.current();
                if self.ns_of(cur).is_html()
                    && matches!(self.name_of(cur), "h1" | "h2" | "h3" | "h4" | "h5" | "h6")
                {
                    self.error();
                    self.open.pop();
                }
                self.insert_html_element(t);
            }

            Token::StartTag(ref t) if matches!(t.name.as_str(), "pre" | "listing") => {
                if self.has_in_scope("p", Scope::Button) {
                    self.close_p();
                }
                self.insert_html_element(t);
                self.ignore_next_lf = true;
                self.frameset_ok = false;
            }

            Token::StartTag(ref t) if t.name == "form" => {
                let has_template = self.open.iter().any(|&id| self.is_html(id, "template"));
                if self.form.is_some() && !has_template {
                    self.error();
                    return;
                }
                if self.has_in_scope("p", Scope::Button) {
                    self.close_p();
                }
                let el = self.insert_html_element(t);
                if !has_template {
                    self.form = Some(el);
                }
            }

            Token::StartTag(ref t) if t.name == "li" => {
                self.frameset_ok = false;
                for i in (0..self.open.len()).rev() {
                    let node = self.open[i];
                    if self.is_html(node, "li") {
                        self.generate_implied_end_tags(Some("li"));
                        if !self.is_html(self.current(), "li") {
                            self.error();
                        }
                        self.pop_until_named("li");
                        break;
                    }
                    if is_special(self.ns_of(node), self.name_of(node))
                        && !matches!(self.name_of(node), "address" | "div" | "p")
                    {
                        break;
                    }
                }
                if self.has_in_scope("p", Scope::Button) {
                    self.close_p();
                }
                self.insert_html_element(t);
            }

            Token::StartTag(ref t) if matches!(t.name.as_str(), "dd" | "dt") => {
                self.frameset_ok = false;
                for i in (0..self.open.len()).rev() {
                    let node = self.open[i];
                    let name = self.name_of(node).to_string();
                    if self.ns_of(node).is_html() && matches!(name.as_str(), "dd" | "dt") {
                        self.generate_implied_end_tags(Some(&name));
                        if !self.is_html(self.current(), &name) {
                            self.error();
                        }
                        self.pop_until_named(&name);
                        break;
                    }
                    if is_special(self.ns_of(node), &name)
                        && !matches!(name.as_str(), "address" | "div" | "p")
                    {
                        break;
                    }
                }
                if self.has_in_scope("p", Scope::Button) {
                    self.close_p();
                }
                self.insert_html_element(t);
            }

            Token::StartTag(ref t) if t.name == "plaintext" => {
                if self.has_in_scope("p", Scope::Button) {
                    self.close_p();
                }
                self.insert_html_element(t);
                self.tokenizer.state = State::Plaintext;
            }

            Token::StartTag(ref t) if t.name == "button" => {
                if self.has_in_scope("button", Scope::Standard) {
                    self.error();
                    self.generate_implied_end_tags(None);
                    self.pop_until_named("button");
                }
                self.reconstruct_active_formatting();
                self.insert_html_element(t);
                self.frameset_ok = false;
            }

            Token::EndTag(ref t)
                if matches!(
                    t.name.as_str(),
                    "address"
                        | "article"
                        | "aside"
                        | "blockquote"
                        | "button"
                        | "center"
                        | "details"
                        | "dialog"
                        | "dir"
                        | "div"
                        | "dl"
                        | "fieldset"
                        | "figcaption"
                        | "figure"
                        | "footer"
                        | "header"
                        | "hgroup"
                        | "listing"
                        | "main"
                        | "menu"
                        | "nav"
                        | "ol"
                        | "pre"
                        | "search"
                        | "section"
                        | "summary"
                        | "ul"
                ) =>
            {
                if !self.has_in_scope(&t.name, Scope::Standard) {
                    self.error();
                    return;
                }
                let name = t.name.clone();
                self.generate_implied_end_tags(None);
                if !self.is_html(self.current(), &name) {
                    self.error();
                }
                self.pop_until_named(&name);
            }

            Token::EndTag(ref t) if t.name == "form" => {
                let has_template = self.open.iter().any(|&id| self.is_html(id, "template"));
                if !has_template {
                    let node = self.form.take();
                    let Some(node) = node else {
                        self.error();
                        return;
                    };
                    if self.stack_index(node).is_none()
                        || !self.has_node_in_scope(node, Scope::Standard)
                    {
                        self.error();
                        return;
                    }
                    self.generate_implied_end_tags(None);
                    if self.current() != node {
                        self.error();
                    }
                    if let Some(pos) = self.stack_index(node) {
                        self.open.remove(pos);
                    }
                } else {
                    if !self.has_in_scope("form", Scope::Standard) {
                        self.error();
                        return;
                    }
                    self.generate_implied_end_tags(None);
                    if !self.is_html(self.current(), "form") {
                        self.error();
                    }
                    self.pop_until_named("form");
                }
            }

            Token::EndTag(ref t) if t.name == "p" => {
                if !self.has_in_scope("p", Scope::Button) {
                    self.error();
                    let tag = TagToken {
                        name: "p".into(),
                        attrs: Vec::new(),
                        self_closing: false,
                    };
                    self.insert_html_element(&tag);
                }
                self.close_p();
            }

            Token::EndTag(ref t) if t.name == "li" => {
                if !self.has_in_scope("li", Scope::ListItem) {
                    self.error();
                    return;
                }
                self.generate_implied_end_tags(Some("li"));
                if !self.is_html(self.current(), "li") {
                    self.error();
                }
                self.pop_until_named("li");
            }

            Token::EndTag(ref t) if matches!(t.name.as_str(), "dd" | "dt") => {
                let name = t.name.clone();
                if !self.has_in_scope(&name, Scope::Standard) {
                    self.error();
                    return;
                }
                self.generate_implied_end_tags(Some(&name));
                if !self.is_html(self.current(), &name) {
                    self.error();
                }
                self.pop_until_named(&name);
            }

            Token::EndTag(ref t) if matches!(t.name.as_str(), "h1" | "h2" | "h3" | "h4" | "h5" | "h6") => {
                const HEADINGS: &[&str] = &["h1", "h2", "h3", "h4", "h5", "h6"];
                let in_scope = HEADINGS.iter().any(|h| self.has_in_scope(h, Scope::Standard));
                if !in_scope {
                    self.error();
                    return;
                }
                self.generate_implied_end_tags(None);
                if self.name_of(self.current()) != t.name {
                    self.error();
                }
                self.pop_until_one_of(HEADINGS);
            }

            Token::StartTag(ref t) if t.name == "a" => {
                // "If the list of active formatting elements contains an `a`
                // element between the end of the list and the last marker,
                // then this is a parse error; run the adoption agency
                // algorithm for the token, then remove that element."
                let existing = self.active_after_marker().into_iter().rev().find_map(|e| {
                    match e {
                        Formatting::Element(id) if self.is_html(id, "a") => Some(id),
                        _ => None,
                    }
                });
                if let Some(id) = existing {
                    self.error();
                    self.adoption_agency("a");
                    if let Some(pos) = self.active_index(id) {
                        self.active.remove(pos);
                    }
                    if let Some(pos) = self.stack_index(id) {
                        self.open.remove(pos);
                    }
                }
                self.reconstruct_active_formatting();
                let el = self.insert_html_element(t);
                self.push_active_formatting(el);
            }

            Token::StartTag(ref t) if is_formatting(&t.name) && t.name != "nobr" => {
                self.reconstruct_active_formatting();
                let el = self.insert_html_element(t);
                self.push_active_formatting(el);
            }

            Token::StartTag(ref t) if t.name == "nobr" => {
                self.reconstruct_active_formatting();
                if self.has_in_scope("nobr", Scope::Standard) {
                    self.error();
                    self.adoption_agency("nobr");
                    self.reconstruct_active_formatting();
                }
                let el = self.insert_html_element(t);
                self.push_active_formatting(el);
            }

            Token::EndTag(ref t) if is_formatting(&t.name) => {
                let name = t.name.clone();
                self.adoption_agency(&name);
            }

            Token::StartTag(ref t) if matches!(t.name.as_str(), "applet" | "marquee" | "object") => {
                self.reconstruct_active_formatting();
                self.insert_html_element(t);
                self.push_active_marker();
                self.frameset_ok = false;
            }

            Token::EndTag(ref t) if matches!(t.name.as_str(), "applet" | "marquee" | "object") => {
                let name = t.name.clone();
                if !self.has_in_scope(&name, Scope::Standard) {
                    self.error();
                    return;
                }
                self.generate_implied_end_tags(None);
                if !self.is_html(self.current(), &name) {
                    self.error();
                }
                self.pop_until_named(&name);
                self.clear_active_to_marker();
            }

            Token::StartTag(ref t) if t.name == "table" => {
                if self.quirks != QuirksMode::Quirks && self.has_in_scope("p", Scope::Button) {
                    self.close_p();
                }
                self.insert_html_element(t);
                self.frameset_ok = false;
                self.switch_to(Mode::InTable);
            }

            Token::EndTag(ref t) if t.name == "br" => {
                self.error();
                let tag = TagToken {
                    name: "br".into(),
                    attrs: Vec::new(),
                    self_closing: false,
                };
                self.reconstruct_active_formatting();
                self.insert_void_element(&tag);
                self.frameset_ok = false;
            }

            Token::StartTag(ref t)
                if matches!(t.name.as_str(), "area" | "br" | "embed" | "img" | "keygen" | "wbr") =>
            {
                self.reconstruct_active_formatting();
                self.insert_void_element(t);
                self.frameset_ok = false;
            }

            Token::StartTag(ref t) if t.name == "input" => {
                // The one element a select still refuses to contain: an
                // `<input>` closes it and lands as its sibling.
                if self.has_in_scope("select", Scope::Standard) {
                    self.error();
                    self.pop_until_named("select");
                }
                self.reconstruct_active_formatting();
                let hidden = t
                    .attr("type")
                    .map(|v| v.eq_ignore_ascii_case("hidden"))
                    .unwrap_or(false);
                self.insert_void_element(t);
                if !hidden {
                    self.frameset_ok = false;
                }
            }

            Token::StartTag(ref t) if matches!(t.name.as_str(), "param" | "source" | "track") => {
                self.insert_void_element(t);
            }

            Token::StartTag(ref t) if t.name == "hr" => {
                // An `<hr>` is a separator *between* the groups of a select, so
                // it closes any option or optgroup first.
                if self.is_html(self.current(), "option") {
                    self.open.pop();
                }
                if self.is_html(self.current(), "optgroup") {
                    self.open.pop();
                }
                if self.has_in_scope("p", Scope::Button) {
                    self.close_p();
                }
                self.insert_void_element(t);
                self.frameset_ok = false;
            }

            Token::StartTag(ref t) if t.name == "image" => {
                // "Change the token's tag name to img and reprocess it."
                self.error();
                let mut t = t.clone();
                t.name = "img".into();
                self.mode_in_body(Token::StartTag(t));
            }

            Token::StartTag(ref t) if t.name == "textarea" => {
                let t = t.clone();
                self.insert_html_element(&t);
                self.ignore_next_lf = true;
                self.tokenizer.state = State::Rcdata;
                self.original_mode = self.mode;
                self.frameset_ok = false;
                self.switch_to(Mode::Text);
            }

            Token::StartTag(ref t) if t.name == "xmp" => {
                if self.has_in_scope("p", Scope::Button) {
                    self.close_p();
                }
                self.reconstruct_active_formatting();
                self.frameset_ok = false;
                let t = t.clone();
                self.parse_text(&t, State::Rawtext);
            }

            Token::StartTag(ref t) if t.name == "iframe" => {
                self.frameset_ok = false;
                let t = t.clone();
                self.parse_text(&t, State::Rawtext);
            }

            Token::StartTag(ref t)
                if t.name == "noembed" || (t.name == "noscript" && self.scripting) =>
            {
                let t = t.clone();
                self.parse_text(&t, State::Rawtext);
            }

            // A `<select>` does not switch the insertion mode any more — it is
            // parsed like any other element. What it still cannot do is nest:
            // a second `<select>` closes the first and is itself dropped.
            Token::StartTag(ref t) if t.name == "select" => {
                if self.has_in_scope("select", Scope::Standard) {
                    self.error();
                    self.pop_until_named("select");
                    return;
                }
                self.reconstruct_active_formatting();
                self.insert_html_element(t);
                self.frameset_ok = false;
            }

            Token::EndTag(ref t) if t.name == "select" => {
                if !self.has_in_scope("select", Scope::Standard) {
                    self.error();
                    return;
                }
                self.generate_implied_end_tags(None);
                if !self.is_html(self.current(), "select") {
                    self.error();
                }
                self.pop_until_named("select");
            }

            Token::StartTag(ref t) if matches!(t.name.as_str(), "optgroup" | "option") => {
                if self.is_html(self.current(), "option") {
                    self.open.pop();
                }
                if t.name == "optgroup" && self.is_html(self.current(), "optgroup") {
                    self.open.pop();
                }
                self.reconstruct_active_formatting();
                self.insert_html_element(t);
            }

            Token::StartTag(ref t) if matches!(t.name.as_str(), "rb" | "rtc") => {
                if self.has_in_scope("ruby", Scope::Standard) {
                    self.generate_implied_end_tags(None);
                    if !self.is_html(self.current(), "ruby") {
                        self.error();
                    }
                }
                self.insert_html_element(t);
            }

            Token::StartTag(ref t) if matches!(t.name.as_str(), "rp" | "rt") => {
                if self.has_in_scope("ruby", Scope::Standard) {
                    self.generate_implied_end_tags(Some("rtc"));
                    let cur = self.current();
                    if !(self.is_html(cur, "ruby") || self.is_html(cur, "rtc")) {
                        self.error();
                    }
                }
                self.insert_html_element(t);
            }

            Token::StartTag(ref t) if t.name == "math" => {
                self.reconstruct_active_formatting();
                let mut t = t.clone();
                adjust_mathml_attributes(&mut t.attrs);
                let self_closing = t.self_closing;
                self.insert_foreign_element(Namespace::MathMl, &t);
                if self_closing {
                    self.open.pop();
                }
            }

            Token::StartTag(ref t) if t.name == "svg" => {
                self.reconstruct_active_formatting();
                let mut t = t.clone();
                adjust_svg_attributes(&mut t.attrs);
                let self_closing = t.self_closing;
                self.insert_foreign_element(Namespace::Svg, &t);
                if self_closing {
                    self.open.pop();
                }
            }

            Token::StartTag(ref t)
                if matches!(
                    t.name.as_str(),
                    "caption"
                        | "col"
                        | "colgroup"
                        | "frame"
                        | "head"
                        | "tbody"
                        | "td"
                        | "tfoot"
                        | "th"
                        | "thead"
                        | "tr"
                ) =>
            {
                self.error();
            }

            Token::StartTag(ref t) => {
                let t = t.clone();
                self.reconstruct_active_formatting();
                self.insert_html_element(&t);
            }

            Token::EndTag(ref t) => {
                let name = t.name.clone();
                self.in_body_any_other_end_tag(&name);
            }
        }
    }

    fn active_after_marker(&self) -> Vec<Formatting> {
        let start = self
            .active
            .iter()
            .rposition(|e| *e == Formatting::Marker)
            .map(|i| i + 1)
            .unwrap_or(0);
        self.active[start..].to_vec()
    }

    /// "close a p element", §13.2.6.4.7.
    fn close_p(&mut self) {
        self.generate_implied_end_tags(Some("p"));
        if !self.is_html(self.current(), "p") {
            self.error();
        }
        self.pop_until_named("p");
    }

    /// The "any other end tag" rules of "in body".
    fn in_body_any_other_end_tag(&mut self, name: &str) {
        for i in (0..self.open.len()).rev() {
            let node = self.open[i];
            if self.ns_of(node).is_html() && self.name_of(node) == name {
                self.generate_implied_end_tags(Some(name));
                if self.current() != node {
                    self.error();
                }
                self.pop_until_node(node);
                return;
            }
            if is_special(self.ns_of(node), self.name_of(node)) {
                self.error();
                return;
            }
        }
    }

    /// The EOF / `</body>` check: anything still open that is not one of the
    /// listed elements is a parse error.
    fn check_body_end_errors(&mut self) {
        const OK: &[&str] = &[
            "dd", "dt", "li", "optgroup", "option", "p", "rb", "rp", "rt", "rtc", "tbody", "td",
            "tfoot", "th", "thead", "tr", "body", "html",
        ];
        let bad = self
            .open
            .iter()
            .any(|&id| !(self.ns_of(id).is_html() && OK.contains(&self.name_of(id))));
        if bad {
            self.error();
        }
    }

    // -- the adoption agency algorithm, §13.2.6.4.7 --------------------------

    /// Untangle misnested formatting elements.
    ///
    /// The motivating case is `<b>1<i>2</b>3</i>`. The `</b>` arrives while
    /// `<i>` is still open, so the `<b>` cannot simply be popped — its content
    /// and the `<i>`'s overlap. The algorithm splits the `<i>`: the part
    /// inside `<b>` stays where it is, and a *clone* of `<i>` is created to
    /// hold what comes after, which is why "3" is still italic.
    ///
    /// Every node motion here goes through the arena's `move_subtree` /
    /// `move_children` / `clone_shallow`, which is what Phase 1 built them for.
    fn adoption_agency(&mut self, subject: &str) {
        // 1-2. If the current node is a matching element that is not in the
        //      active list, just pop it.
        let cur = self.current();
        if self.ns_of(cur).is_html() && self.name_of(cur) == subject && self.active_index(cur).is_none()
        {
            self.open.pop();
            return;
        }

        for _outer in 0..8 {
            // 4.3. The last matching entry after the last marker.
            let start = self
                .active
                .iter()
                .rposition(|e| *e == Formatting::Marker)
                .map(|i| i + 1)
                .unwrap_or(0);
            let mut fmt_active_idx = None;
            for i in (start..self.active.len()).rev() {
                if let Formatting::Element(id) = self.active[i] {
                    if self.ns_of(id).is_html() && self.name_of(id) == subject {
                        fmt_active_idx = Some(i);
                        break;
                    }
                }
            }
            let Some(mut fmt_idx) = fmt_active_idx else {
                // No such entry: fall back to the "any other end tag" rules.
                self.in_body_any_other_end_tag(subject);
                return;
            };
            let Formatting::Element(formatting) = self.active[fmt_idx] else {
                unreachable!()
            };

            // 4.4. In the active list but not open: drop it.
            let Some(fmt_stack_idx) = self.stack_index(formatting) else {
                self.error();
                self.active.remove(fmt_idx);
                return;
            };

            // 4.5. Open but not in scope: give up.
            if !self.has_node_in_scope(formatting, Scope::Standard) {
                self.error();
                return;
            }

            // 4.6. Not the current node: an error, but keep going.
            if formatting != self.current() {
                self.error();
            }

            // 4.7. The furthest block: the topmost *special* element below the
            //      formatting element on the stack.
            let furthest = self.open[fmt_stack_idx + 1..]
                .iter()
                .copied()
                .find(|&id| is_special(self.ns_of(id), self.name_of(id)));

            // 4.8. No furthest block: pop up to and including the formatting
            //      element and drop it from the list. This is the common,
            //      well-nested case.
            let Some(furthest) = furthest else {
                self.open.truncate(fmt_stack_idx);
                self.active.remove(fmt_idx);
                return;
            };

            // 4.9-4.11.
            let common_ancestor = self.open[fmt_stack_idx - 1];
            let mut bookmark = fmt_idx;
            let mut node_index = self
                .stack_index(furthest)
                .expect("furthest block came from the stack");
            let mut last_node = furthest;

            // 4.13. Walk up from the furthest block towards the formatting
            //       element, cloning every still-active element on the way and
            //       re-parenting what we have accumulated so far into it.
            let mut inner = 0;
            loop {
                inner += 1;
                if node_index == 0 {
                    break;
                }
                node_index -= 1;
                let node = self.open[node_index];
                if node == formatting {
                    break;
                }

                let mut in_active = self.active_index(node);
                if inner > 3 {
                    if let Some(pos) = in_active {
                        self.active.remove(pos);
                        if bookmark > pos {
                            bookmark -= 1;
                        }
                        if fmt_idx > pos {
                            fmt_idx -= 1;
                        }
                        in_active = None;
                    }
                }

                let Some(active_pos) = in_active else {
                    // Not a formatting element: it just goes away.
                    self.open.remove(node_index);
                    continue;
                };

                // 4.13.6. Replace `node` with a fresh clone in both lists.
                let fresh = self.dom.clone_shallow(node).expect("live node");
                self.active[active_pos] = Formatting::Element(fresh);
                self.open[node_index] = fresh;

                // 4.13.7.
                if last_node == furthest {
                    bookmark = active_pos + 1;
                }

                // 4.13.8. `last_node` becomes a child of the clone.
                let _ = self.dom.move_subtree(fresh, self.root, None);
                let _ = self.dom.remove(fresh);
                let _ = self.dom.append_child(fresh, last_node);
                last_node = fresh;
            }

            // 4.14. Re-home the accumulated subtree, honouring foster
            //       parenting relative to the common ancestor.
            let (parent, before) = self.insertion_place(Some(common_ancestor));
            let _ = self.dom.insert_before(parent, last_node, before);

            // 4.15-4.17. A fresh copy of the formatting element adopts
            //            everything the furthest block was holding.
            let fresh_fmt = self.dom.clone_shallow(formatting).expect("live node");
            let _ = self.dom.move_children(furthest, fresh_fmt, None);
            let _ = self.dom.append_child(furthest, fresh_fmt);

            // 4.18.
            if let Some(pos) = self.active_index(formatting) {
                self.active.remove(pos);
                if bookmark > pos {
                    bookmark -= 1;
                }
            }
            let bookmark = bookmark.min(self.active.len());
            self.active.insert(bookmark, Formatting::Element(fresh_fmt));

            // 4.19.
            if let Some(pos) = self.stack_index(formatting) {
                self.open.remove(pos);
            }
            let furthest_pos = self
                .stack_index(furthest)
                .expect("furthest block is still open");
            self.open.insert(furthest_pos + 1, fresh_fmt);
        }
    }

    // =======================================================================
    // §13.2.6.4.8 "text"
    // =======================================================================

    fn mode_text(&mut self, token: Token) {
        match token {
            Token::Character(c) => self.insert_character(c),
            Token::Eof => {
                self.error();
                self.open.pop();
                let mode = self.original_mode;
                self.reprocess(mode, Token::Eof);
            }
            Token::EndTag(_) => {
                self.open.pop();
                self.switch_to(self.original_mode);
            }
            _ => {}
        }
    }

    // =======================================================================
    // §13.2.6.4.9 "in table"
    // =======================================================================

    fn mode_in_table(&mut self, token: Token) {
        match token {
            Token::Character(_)
                if {
                    let cur = self.current();
                    self.ns_of(cur).is_html()
                        && matches!(self.name_of(cur), "table" | "tbody" | "template" | "tfoot" | "thead" | "tr")
                } =>
            {
                self.pending_table_text.clear();
                self.pending_table_text_ws_only = true;
                self.original_mode = self.mode;
                self.reprocess(Mode::InTableText, token);
            }
            Token::Comment(_) | Token::ProcessingInstruction { .. } => self.insert_markup(&token, None),
            Token::Doctype(_) => self.error(),
            Token::StartTag(ref t) if t.name == "caption" => {
                self.clear_stack_to_table_context();
                self.push_active_marker();
                self.insert_html_element(t);
                self.switch_to(Mode::InCaption);
            }
            Token::StartTag(ref t) if t.name == "colgroup" => {
                self.clear_stack_to_table_context();
                self.insert_html_element(t);
                self.switch_to(Mode::InColumnGroup);
            }
            Token::StartTag(ref t) if t.name == "col" => {
                self.clear_stack_to_table_context();
                let tag = TagToken {
                    name: "colgroup".into(),
                    attrs: Vec::new(),
                    self_closing: false,
                };
                self.insert_html_element(&tag);
                self.reprocess(Mode::InColumnGroup, token.clone());
            }
            Token::StartTag(ref t) if matches!(t.name.as_str(), "tbody" | "tfoot" | "thead") => {
                self.clear_stack_to_table_context();
                self.insert_html_element(t);
                self.switch_to(Mode::InTableBody);
            }
            Token::StartTag(ref t) if matches!(t.name.as_str(), "td" | "th" | "tr") => {
                self.clear_stack_to_table_context();
                let tag = TagToken {
                    name: "tbody".into(),
                    attrs: Vec::new(),
                    self_closing: false,
                };
                self.insert_html_element(&tag);
                self.reprocess(Mode::InTableBody, token.clone());
            }
            Token::StartTag(ref t) if t.name == "table" => {
                self.error();
                if !self.has_in_scope("table", Scope::Table) {
                    return;
                }
                self.pop_until_named("table");
                self.reset_insertion_mode();
                self.process(token.clone());
            }
            Token::EndTag(ref t) if t.name == "table" => {
                if !self.has_in_scope("table", Scope::Table) {
                    self.error();
                    return;
                }
                self.pop_until_named("table");
                self.reset_insertion_mode();
            }
            Token::EndTag(ref t)
                if matches!(
                    t.name.as_str(),
                    "body" | "caption" | "col" | "colgroup" | "html" | "tbody" | "td" | "tfoot"
                        | "th" | "thead" | "tr"
                ) =>
            {
                self.error();
            }
            Token::StartTag(ref t) if matches!(t.name.as_str(), "style" | "script" | "template") => {
                self.mode_in_head(token.clone())
            }
            Token::EndTag(ref t) if t.name == "template" => self.mode_in_head(token.clone()),
            Token::StartTag(ref t) if t.name == "input" => {
                let hidden = t
                    .attr("type")
                    .map(|v| v.eq_ignore_ascii_case("hidden"))
                    .unwrap_or(false);
                if !hidden {
                    self.in_table_anything_else(token.clone());
                    return;
                }
                self.error();
                self.insert_void_element(t);
            }
            Token::StartTag(ref t) if t.name == "form" => {
                self.error();
                let has_template = self.open.iter().any(|&id| self.is_html(id, "template"));
                if has_template || self.form.is_some() {
                    return;
                }
                let el = self.insert_html_element(t);
                self.form = Some(el);
                self.open.pop();
            }
            Token::Eof => self.mode_in_body(token),
            other => self.in_table_anything_else(other),
        }
    }

    /// "in table"'s anything-else arm turns foster parenting on for the
    /// duration of one token. This is the entire mechanism by which
    /// `<table><span>x</span>` puts the span *before* the table.
    fn in_table_anything_else(&mut self, token: Token) {
        self.error();
        self.foster_parenting = true;
        self.mode_in_body(token);
        self.foster_parenting = false;
    }

    fn clear_stack_to_table_context(&mut self) {
        while let Some(&id) = self.open.last() {
            if self.ns_of(id).is_html()
                && matches!(self.name_of(id), "table" | "template" | "html")
            {
                break;
            }
            self.open.pop();
        }
    }

    fn clear_stack_to_table_body_context(&mut self) {
        while let Some(&id) = self.open.last() {
            if self.ns_of(id).is_html()
                && matches!(
                    self.name_of(id),
                    "tbody" | "tfoot" | "thead" | "template" | "html"
                )
            {
                break;
            }
            self.open.pop();
        }
    }

    fn clear_stack_to_table_row_context(&mut self) {
        while let Some(&id) = self.open.last() {
            if self.ns_of(id).is_html() && matches!(self.name_of(id), "tr" | "template" | "html") {
                break;
            }
            self.open.pop();
        }
    }

    // =======================================================================
    // §13.2.6.4.10 "in table text"
    // =======================================================================

    fn mode_in_table_text(&mut self, token: Token) {
        match token {
            Token::Character('\0') => self.error(),
            Token::Character(c) => {
                if !is_ws(c) {
                    self.pending_table_text_ws_only = false;
                }
                self.pending_table_text.push(c);
            }
            other => {
                let text = std::mem::take(&mut self.pending_table_text);
                if self.pending_table_text_ws_only {
                    self.insert_text(&text);
                } else {
                    // Non-whitespace in a table gets foster-parented, as a run.
                    self.error();
                    self.foster_parenting = true;
                    for c in text.chars() {
                        self.mode_in_body(Token::Character(c));
                    }
                    self.foster_parenting = false;
                }
                self.pending_table_text_ws_only = true;
                let mode = self.original_mode;
                self.reprocess(mode, other);
            }
        }
    }

    // =======================================================================
    // §13.2.6.4.11 "in caption"
    // =======================================================================

    fn mode_in_caption(&mut self, token: Token) {
        match token {
            Token::EndTag(ref t) if t.name == "caption" => {
                if !self.has_in_scope("caption", Scope::Table) {
                    self.error();
                    return;
                }
                self.generate_implied_end_tags(None);
                if !self.is_html(self.current(), "caption") {
                    self.error();
                }
                self.pop_until_named("caption");
                self.clear_active_to_marker();
                self.switch_to(Mode::InTable);
            }
            Token::StartTag(ref t)
                if matches!(
                    t.name.as_str(),
                    "caption" | "col" | "colgroup" | "tbody" | "td" | "tfoot" | "th" | "thead"
                        | "tr"
                ) =>
            {
                self.error();
                if !self.close_caption() {
                    return;
                }
                self.reprocess(Mode::InTable, token.clone());
            }
            Token::EndTag(ref t) if t.name == "table" => {
                self.error();
                if !self.close_caption() {
                    return;
                }
                self.reprocess(Mode::InTable, token.clone());
            }
            Token::EndTag(ref t)
                if matches!(
                    t.name.as_str(),
                    "body" | "col" | "colgroup" | "html" | "tbody" | "td" | "tfoot" | "th"
                        | "thead" | "tr"
                ) =>
            {
                self.error();
            }
            other => self.mode_in_body(other),
        }
    }

    fn close_caption(&mut self) -> bool {
        if !self.has_in_scope("caption", Scope::Table) {
            self.error();
            return false;
        }
        self.generate_implied_end_tags(None);
        if !self.is_html(self.current(), "caption") {
            self.error();
        }
        self.pop_until_named("caption");
        self.clear_active_to_marker();
        true
    }

    // =======================================================================
    // §13.2.6.4.12 "in column group"
    // =======================================================================

    fn mode_in_column_group(&mut self, token: Token) {
        match token {
            Token::Character(c) if is_ws(c) => self.insert_character(c),
            Token::Comment(_) | Token::ProcessingInstruction { .. } => self.insert_markup(&token, None),
            Token::Doctype(_) => self.error(),
            Token::StartTag(ref t) if t.name == "html" => self.mode_in_body(token.clone()),
            Token::StartTag(ref t) if t.name == "col" => {
                self.insert_void_element(t);
            }
            Token::EndTag(ref t) if t.name == "colgroup" => {
                if !self.is_html(self.current(), "colgroup") {
                    self.error();
                    return;
                }
                self.open.pop();
                self.switch_to(Mode::InTable);
            }
            Token::EndTag(ref t) if t.name == "col" => self.error(),
            Token::StartTag(ref t) if t.name == "template" => self.mode_in_head(token.clone()),
            Token::EndTag(ref t) if t.name == "template" => self.mode_in_head(token.clone()),
            Token::Eof => self.mode_in_body(token),
            other => {
                if !self.is_html(self.current(), "colgroup") {
                    self.error();
                    return;
                }
                self.open.pop();
                self.reprocess(Mode::InTable, other);
            }
        }
    }

    // =======================================================================
    // §13.2.6.4.13 "in table body"
    // =======================================================================

    fn mode_in_table_body(&mut self, token: Token) {
        match token {
            Token::StartTag(ref t) if t.name == "tr" => {
                self.clear_stack_to_table_body_context();
                self.insert_html_element(t);
                self.switch_to(Mode::InRow);
            }
            Token::StartTag(ref t) if matches!(t.name.as_str(), "th" | "td") => {
                self.error();
                self.clear_stack_to_table_body_context();
                let tag = TagToken {
                    name: "tr".into(),
                    attrs: Vec::new(),
                    self_closing: false,
                };
                self.insert_html_element(&tag);
                self.reprocess(Mode::InRow, token.clone());
            }
            Token::EndTag(ref t) if matches!(t.name.as_str(), "tbody" | "tfoot" | "thead") => {
                if !self.has_in_scope(&t.name, Scope::Table) {
                    self.error();
                    return;
                }
                self.clear_stack_to_table_body_context();
                self.open.pop();
                self.switch_to(Mode::InTable);
            }
            Token::StartTag(ref t)
                if matches!(
                    t.name.as_str(),
                    "caption" | "col" | "colgroup" | "tbody" | "tfoot" | "thead"
                ) =>
            {
                if !self.any_table_body_in_table_scope() {
                    self.error();
                    return;
                }
                self.clear_stack_to_table_body_context();
                self.open.pop();
                self.reprocess(Mode::InTable, token.clone());
            }
            Token::EndTag(ref t) if t.name == "table" => {
                if !self.any_table_body_in_table_scope() {
                    self.error();
                    return;
                }
                self.clear_stack_to_table_body_context();
                self.open.pop();
                self.reprocess(Mode::InTable, token.clone());
            }
            Token::EndTag(ref t)
                if matches!(
                    t.name.as_str(),
                    "body" | "caption" | "col" | "colgroup" | "html" | "td" | "th" | "tr"
                ) =>
            {
                self.error();
            }
            other => self.mode_in_table(other),
        }
    }

    fn any_table_body_in_table_scope(&self) -> bool {
        ["tbody", "thead", "tfoot"]
            .iter()
            .any(|n| self.has_in_scope(n, Scope::Table))
    }

    // =======================================================================
    // §13.2.6.4.14 "in row"
    // =======================================================================

    fn mode_in_row(&mut self, token: Token) {
        match token {
            Token::StartTag(ref t) if matches!(t.name.as_str(), "th" | "td") => {
                self.clear_stack_to_table_row_context();
                self.insert_html_element(t);
                self.switch_to(Mode::InCell);
                self.push_active_marker();
            }
            Token::EndTag(ref t) if t.name == "tr" => {
                if !self.has_in_scope("tr", Scope::Table) {
                    self.error();
                    return;
                }
                self.clear_stack_to_table_row_context();
                self.open.pop();
                self.switch_to(Mode::InTableBody);
            }
            Token::StartTag(ref t)
                if matches!(
                    t.name.as_str(),
                    "caption" | "col" | "colgroup" | "tbody" | "tfoot" | "thead" | "tr"
                ) =>
            {
                if !self.has_in_scope("tr", Scope::Table) {
                    self.error();
                    return;
                }
                self.clear_stack_to_table_row_context();
                self.open.pop();
                self.reprocess(Mode::InTableBody, token.clone());
            }
            Token::EndTag(ref t) if t.name == "table" => {
                if !self.has_in_scope("tr", Scope::Table) {
                    self.error();
                    return;
                }
                self.clear_stack_to_table_row_context();
                self.open.pop();
                self.reprocess(Mode::InTableBody, token.clone());
            }
            Token::EndTag(ref t) if matches!(t.name.as_str(), "tbody" | "tfoot" | "thead") => {
                if !self.has_in_scope(&t.name, Scope::Table) {
                    self.error();
                    return;
                }
                if !self.has_in_scope("tr", Scope::Table) {
                    return;
                }
                self.clear_stack_to_table_row_context();
                self.open.pop();
                self.reprocess(Mode::InTableBody, token.clone());
            }
            Token::EndTag(ref t)
                if matches!(
                    t.name.as_str(),
                    "body" | "caption" | "col" | "colgroup" | "html" | "td" | "th"
                ) =>
            {
                self.error();
            }
            other => self.mode_in_table(other),
        }
    }

    // =======================================================================
    // §13.2.6.4.15 "in cell"
    // =======================================================================

    fn mode_in_cell(&mut self, token: Token) {
        match token {
            Token::EndTag(ref t) if matches!(t.name.as_str(), "td" | "th") => {
                let name = t.name.clone();
                if !self.has_in_scope(&name, Scope::Table) {
                    self.error();
                    return;
                }
                self.generate_implied_end_tags(None);
                if !self.is_html(self.current(), &name) {
                    self.error();
                }
                self.pop_until_named(&name);
                self.clear_active_to_marker();
                self.switch_to(Mode::InRow);
            }
            Token::StartTag(ref t)
                if matches!(
                    t.name.as_str(),
                    "caption" | "col" | "colgroup" | "tbody" | "td" | "tfoot" | "th" | "thead"
                        | "tr"
                ) =>
            {
                if !(self.has_in_scope("td", Scope::Table) || self.has_in_scope("th", Scope::Table))
                {
                    self.error();
                    return;
                }
                self.close_cell();
                self.reprocess(Mode::InRow, token.clone());
            }
            Token::EndTag(ref t)
                if matches!(t.name.as_str(), "body" | "caption" | "col" | "colgroup" | "html") =>
            {
                self.error();
            }
            Token::EndTag(ref t)
                if matches!(t.name.as_str(), "table" | "tbody" | "tfoot" | "thead" | "tr") =>
            {
                if !self.has_in_scope(&t.name, Scope::Table) {
                    self.error();
                    return;
                }
                self.close_cell();
                self.reprocess(Mode::InRow, token.clone());
            }
            other => self.mode_in_body(other),
        }
    }

    fn close_cell(&mut self) {
        self.generate_implied_end_tags(None);
        let cur = self.current();
        if !(self.is_html(cur, "td") || self.is_html(cur, "th")) {
            self.error();
        }
        self.pop_until_one_of(&["td", "th"]);
        self.clear_active_to_marker();
        self.switch_to(Mode::InRow);
    }

    // §13.2.6.4.16 "in select" and §13.2.6.4.17 "in select in table" used to
    // live here. The customizable-select revision deleted both; `<select>` is
    // now an ordinary element whose children are handled by "in body", which
    // is why a `<div>` or an `<svg>` inside a select survives instead of being
    // dropped. The rules that were unique to those modes — popping an open
    // `<option>`/`<optgroup>`, and closing a select on a nested `<select>` or
    // an `<input>` — moved into `mode_in_body`.

    // =======================================================================
    // §13.2.6.4.18 "in template"
    // =======================================================================

    fn mode_in_template(&mut self, token: Token) {
        match token {
            Token::Character(_)
            | Token::Comment(_)
            | Token::ProcessingInstruction { .. }
            | Token::Doctype(_) => self.mode_in_body(token),
            Token::StartTag(ref t)
                if matches!(
                    t.name.as_str(),
                    "base"
                        | "basefont"
                        | "bgsound"
                        | "link"
                        | "meta"
                        | "noframes"
                        | "script"
                        | "style"
                        | "template"
                        | "title"
                ) =>
            {
                self.mode_in_head(token.clone())
            }
            Token::EndTag(ref t) if t.name == "template" => self.mode_in_head(token.clone()),
            Token::StartTag(ref t)
                if matches!(
                    t.name.as_str(),
                    "caption" | "colgroup" | "tbody" | "tfoot" | "thead"
                ) =>
            {
                self.template_modes.pop();
                self.template_modes.push(Mode::InTable);
                self.reprocess(Mode::InTable, token.clone());
            }
            Token::StartTag(ref t) if t.name == "col" => {
                self.template_modes.pop();
                self.template_modes.push(Mode::InColumnGroup);
                self.reprocess(Mode::InColumnGroup, token.clone());
            }
            Token::StartTag(ref t) if t.name == "tr" => {
                self.template_modes.pop();
                self.template_modes.push(Mode::InTableBody);
                self.reprocess(Mode::InTableBody, token.clone());
            }
            Token::StartTag(ref t) if matches!(t.name.as_str(), "td" | "th") => {
                self.template_modes.pop();
                self.template_modes.push(Mode::InRow);
                self.reprocess(Mode::InRow, token.clone());
            }
            Token::StartTag(_) => {
                self.template_modes.pop();
                self.template_modes.push(Mode::InBody);
                self.reprocess(Mode::InBody, token);
            }
            Token::EndTag(_) => self.error(),
            Token::Eof => {
                if !self.open.iter().any(|&id| self.is_html(id, "template")) {
                    self.stopped = true;
                    return;
                }
                self.error();
                self.pop_until_named("template");
                self.clear_active_to_marker();
                self.template_modes.pop();
                self.reset_insertion_mode();
                self.process(Token::Eof);
            }
        }
    }

    // =======================================================================
    // §13.2.6.4.19 "after body"
    // =======================================================================

    fn mode_after_body(&mut self, token: Token) {
        match token {
            Token::Character(c) if is_ws(c) => self.mode_in_body(Token::Character(c)),
            Token::Comment(_) | Token::ProcessingInstruction { .. } => {
                let target = self.open[0];
                self.insert_markup(&token, Some(target));
            }
            Token::Doctype(_) => self.error(),
            Token::StartTag(ref t) if t.name == "html" => self.mode_in_body(token.clone()),
            Token::EndTag(ref t) if t.name == "html" => {
                if self.fragment {
                    self.error();
                    return;
                }
                self.switch_to(Mode::AfterAfterBody);
            }
            Token::Eof => self.stopped = true,
            other => {
                self.error();
                self.reprocess(Mode::InBody, other);
            }
        }
    }

    // =======================================================================
    // §13.2.6.4.20 "in frameset"
    // =======================================================================

    fn mode_in_frameset(&mut self, token: Token) {
        match token {
            Token::Character(c) if is_ws(c) => self.insert_character(c),
            Token::Comment(_) | Token::ProcessingInstruction { .. } => self.insert_markup(&token, None),
            Token::Doctype(_) => self.error(),
            Token::StartTag(ref t) if t.name == "html" => self.mode_in_body(token.clone()),
            Token::StartTag(ref t) if t.name == "frameset" => {
                self.insert_html_element(t);
            }
            Token::EndTag(ref t) if t.name == "frameset" => {
                if self.is_html(self.current(), "html") {
                    self.error();
                    return;
                }
                self.open.pop();
                if !self.fragment && !self.is_html(self.current(), "frameset") {
                    self.switch_to(Mode::AfterFrameset);
                }
            }
            Token::StartTag(ref t) if t.name == "frame" => {
                self.insert_void_element(t);
            }
            Token::StartTag(ref t) if t.name == "noframes" => self.mode_in_head(token.clone()),
            Token::Eof => {
                if !self.is_html(self.current(), "html") {
                    self.error();
                }
                self.stopped = true;
            }
            _ => self.error(),
        }
    }

    // =======================================================================
    // §13.2.6.4.21 "after frameset"
    // =======================================================================

    fn mode_after_frameset(&mut self, token: Token) {
        match token {
            Token::Character(c) if is_ws(c) => self.insert_character(c),
            Token::Comment(_) | Token::ProcessingInstruction { .. } => self.insert_markup(&token, None),
            Token::Doctype(_) => self.error(),
            Token::StartTag(ref t) if t.name == "html" => self.mode_in_body(token.clone()),
            Token::EndTag(ref t) if t.name == "html" => self.switch_to(Mode::AfterAfterFrameset),
            Token::StartTag(ref t) if t.name == "noframes" => self.mode_in_head(token.clone()),
            Token::Eof => self.stopped = true,
            _ => self.error(),
        }
    }

    // =======================================================================
    // §13.2.6.4.22 "after after body"
    // =======================================================================

    fn mode_after_after_body(&mut self, token: Token) {
        match token {
            Token::Comment(_) | Token::ProcessingInstruction { .. } => {
                let doc = self.root;
                self.insert_markup(&token, Some(doc));
            }
            Token::Doctype(_) => self.mode_in_body(token),
            Token::Character(c) if is_ws(c) => self.mode_in_body(Token::Character(c)),
            Token::StartTag(ref t) if t.name == "html" => self.mode_in_body(token.clone()),
            Token::Eof => self.stopped = true,
            other => {
                self.error();
                self.reprocess(Mode::InBody, other);
            }
        }
    }

    // =======================================================================
    // §13.2.6.4.23 "after after frameset"
    // =======================================================================

    fn mode_after_after_frameset(&mut self, token: Token) {
        match token {
            Token::Comment(_) | Token::ProcessingInstruction { .. } => {
                let doc = self.root;
                self.insert_markup(&token, Some(doc));
            }
            Token::Doctype(_) => self.mode_in_body(token),
            Token::Character(c) if is_ws(c) => self.mode_in_body(Token::Character(c)),
            Token::StartTag(ref t) if t.name == "html" => self.mode_in_body(token.clone()),
            Token::StartTag(ref t) if t.name == "noframes" => self.mode_in_head(token.clone()),
            Token::Eof => self.stopped = true,
            _ => self.error(),
        }
    }

    // =======================================================================
    // §13.2.6.5 "the rules for parsing tokens in foreign content"
    // =======================================================================

    fn foreign_content(&mut self, token: Token) {
        match token {
            Token::Character('\0') => {
                self.error();
                self.insert_character('\u{FFFD}');
            }
            Token::Character(c) => {
                if !is_ws(c) {
                    self.frameset_ok = false;
                }
                self.insert_character(c);
            }
            Token::Comment(_) | Token::ProcessingInstruction { .. } => self.insert_markup(&token, None),
            Token::Doctype(_) => self.error(),

            Token::StartTag(ref t)
                if matches!(
                    t.name.as_str(),
                    "b" | "big"
                        | "blockquote"
                        | "body"
                        | "br"
                        | "center"
                        | "code"
                        | "dd"
                        | "div"
                        | "dl"
                        | "dt"
                        | "em"
                        | "embed"
                        | "h1"
                        | "h2"
                        | "h3"
                        | "h4"
                        | "h5"
                        | "h6"
                        | "head"
                        | "hr"
                        | "i"
                        | "img"
                        | "li"
                        | "listing"
                        | "menu"
                        | "meta"
                        | "nobr"
                        | "ol"
                        | "p"
                        | "pre"
                        | "ruby"
                        | "s"
                        | "small"
                        | "span"
                        | "strong"
                        | "strike"
                        | "sub"
                        | "sup"
                        | "table"
                        | "tt"
                        | "u"
                        | "ul"
                        | "var"
                ) || (t.name == "font"
                    && t.attrs
                        .iter()
                        .any(|a| matches!(a.name.as_str(), "color" | "face" | "size"))) =>
            {
                // A "breakout" tag: markup so unmistakably HTML that the
                // foreign subtree is abandoned rather than allowed to swallow
                // it. Note there is no fragment special case here any more —
                // the spec dropped it, and the pop loop terminates on its own
                // because the bottom of the stack is always an HTML element.
                self.error();
                loop {
                    let Some(&cur) = self.open.last() else { break };
                    if self.ns_of(cur).is_html()
                        || self.is_mathml_text_integration_point(cur)
                        || self.is_html_integration_point(cur)
                    {
                        break;
                    }
                    self.open.pop();
                    if self.open.len() == 1 {
                        break;
                    }
                }
                self.process(token.clone());
            }

            Token::StartTag(_) => self.foreign_start_tag(token),

            Token::EndTag(ref t) if matches!(t.name.as_str(), "br" | "p") => {
                // These are handled by the "any other start tag" branch above
                // in spirit: they break out of foreign content.
                self.error();
                loop {
                    let Some(&cur) = self.open.last() else { break };
                    if self.ns_of(cur).is_html()
                        || self.is_mathml_text_integration_point(cur)
                        || self.is_html_integration_point(cur)
                    {
                        break;
                    }
                    self.open.pop();
                    if self.open.len() == 1 {
                        break;
                    }
                }
                self.process(token.clone());
            }

            Token::EndTag(ref t) if t.name == "script" && {
                let cur = self.current();
                self.ns_of(cur) == Namespace::Svg && self.name_of(cur) == "script"
            } =>
            {
                self.open.pop();
            }

            Token::EndTag(ref t) => {
                let mut i = self.open.len();
                if i == 0 {
                    return;
                }
                i -= 1;
                if !self.name_of(self.open[i]).eq_ignore_ascii_case(&t.name) {
                    self.error();
                }
                loop {
                    if self.open[i] == self.open[0] {
                        return;
                    }
                    if self.name_of(self.open[i]).eq_ignore_ascii_case(&t.name) {
                        let target = self.open[i];
                        self.pop_until_node(target);
                        return;
                    }
                    i -= 1;
                    if self.ns_of(self.open[i]).is_html() {
                        break;
                    }
                }
                self.process(token.clone());
            }

            Token::Eof => {
                self.error();
                self.stopped = true;
            }
        }
    }

    fn foreign_start_tag(&mut self, token: Token) {
        let Token::StartTag(t) = token else { return };
        let adjusted_ns = match self.adjusted_current() {
            Some(id) => self.ns_of(id),
            None => Namespace::Html,
        };
        let mut tag = t.clone();
        match adjusted_ns {
            Namespace::MathMl => adjust_mathml_attributes(&mut tag.attrs),
            Namespace::Svg => adjust_svg_attributes(&mut tag.attrs),
            _ => {}
        }
        let self_closing = tag.self_closing;
        let name_is_script = tag.name == "script"
            || adjust_svg_tag_name(&tag.name) == "script";
        self.insert_foreign_element(adjusted_ns, &tag);
        if self_closing {
            self.open.pop();
            if adjusted_ns == Namespace::Svg && name_is_script {
                // "acknowledge the token's self-closing flag"; nothing more to
                // do since scripts are never executed here.
            }
        }
    }
}

// ---------------------------------------------------------------------------
// Free functions
// ---------------------------------------------------------------------------

#[inline]
fn is_ws(c: char) -> bool {
    matches!(c, '\t' | '\n' | '\u{0C}' | '\r' | ' ')
}

/// §13.2.6.1: how a DOCTYPE token sets the document's quirks mode.
///
/// The only place tree construction consults this is the `<table>` start tag
/// in "in body", which does not close an open `<p>` in quirks mode.
fn quirks_from_doctype(d: &DoctypeToken) -> QuirksMode {
    let name = d.name.as_deref().unwrap_or("");
    let public = d.public_id.as_deref().unwrap_or("").to_ascii_lowercase();
    let system = d.system_id.as_deref().unwrap_or("").to_ascii_lowercase();

    if d.force_quirks
        || name != "html"
        || public == "-//w3o//dtd w3 html strict 3.0//en//"
        || public == "-/w3c/dtd html 4.0 transitional/en"
        || public == "html"
        || system == "http://www.ibm.com/data/dtd/v11/ibmxhtml1-transitional.dtd"
        || QUIRKY_PUBLIC_PREFIXES.iter().any(|p| public.starts_with(p))
    {
        return QuirksMode::Quirks;
    }
    if d.system_id.is_none()
        && (public.starts_with("-//w3c//dtd html 4.01 frameset//")
            || public.starts_with("-//w3c//dtd html 4.01 transitional//"))
    {
        return QuirksMode::Quirks;
    }
    if public.starts_with("-//w3c//dtd xhtml 1.0 frameset//")
        || public.starts_with("-//w3c//dtd xhtml 1.0 transitional//")
        || (d.system_id.is_some()
            && (public.starts_with("-//w3c//dtd html 4.01 frameset//")
                || public.starts_with("-//w3c//dtd html 4.01 transitional//")))
    {
        return QuirksMode::LimitedQuirks;
    }
    QuirksMode::NoQuirks
}

const QUIRKY_PUBLIC_PREFIXES: &[&str] = &[
    "+//silmaril//dtd html pro v0r11 19970101//",
    "-//as//dtd html 3.0 aswedit + extensions//",
    "-//advasoft ltd//dtd html 3.0 aswedit + extensions//",
    "-//ietf//dtd html 2.0 level 1//",
    "-//ietf//dtd html 2.0 level 2//",
    "-//ietf//dtd html 2.0 strict level 1//",
    "-//ietf//dtd html 2.0 strict level 2//",
    "-//ietf//dtd html 2.0 strict//",
    "-//ietf//dtd html 2.0//",
    "-//ietf//dtd html 2.1e//",
    "-//ietf//dtd html 3.0//",
    "-//ietf//dtd html 3.2 final//",
    "-//ietf//dtd html 3.2//",
    "-//ietf//dtd html 3//",
    "-//ietf//dtd html level 0//",
    "-//ietf//dtd html level 1//",
    "-//ietf//dtd html level 2//",
    "-//ietf//dtd html level 3//",
    "-//ietf//dtd html strict level 0//",
    "-//ietf//dtd html strict level 1//",
    "-//ietf//dtd html strict level 2//",
    "-//ietf//dtd html strict level 3//",
    "-//ietf//dtd html strict//",
    "-//ietf//dtd html//",
    "-//metrius//dtd metrius presentational//",
    "-//microsoft//dtd internet explorer 2.0 html strict//",
    "-//microsoft//dtd internet explorer 2.0 html//",
    "-//microsoft//dtd internet explorer 2.0 tables//",
    "-//microsoft//dtd internet explorer 3.0 html strict//",
    "-//microsoft//dtd internet explorer 3.0 html//",
    "-//microsoft//dtd internet explorer 3.0 tables//",
    "-//netscape comm. corp.//dtd html//",
    "-//netscape comm. corp.//dtd strict html//",
    "-//o'reilly and associates//dtd html 2.0//",
    "-//o'reilly and associates//dtd html extended 1.0//",
    "-//o'reilly and associates//dtd html extended relaxed 1.0//",
    "-//sq//dtd html 2.0 hotmetal + extensions//",
    "-//softquad software//dtd hotmetal pro 6.0::19990601::extensions to html 4.0//",
    "-//softquad//dtd hotmetal pro 4.0::19971010::extensions to html 4.0//",
    "-//spyglass//dtd html 2.0 extended//",
    "-//sun microsystems corp.//dtd hotjava html//",
    "-//sun microsystems corp.//dtd hotjava strict html//",
    "-//w3c//dtd html 3 1995-03-24//",
    "-//w3c//dtd html 3.2 draft//",
    "-//w3c//dtd html 3.2 final//",
    "-//w3c//dtd html 3.2//",
    "-//w3c//dtd html 3.2s draft//",
    "-//w3c//dtd html 4.0 frameset//",
    "-//w3c//dtd html 4.0 transitional//",
    "-//w3c//dtd html experimental 19960712//",
    "-//w3c//dtd html experimental 970421//",
    "-//w3c//dtd w3 html//",
    "-//w3o//dtd w3 html 3.0//",
    "-//webtechs//dtd mozilla html 2.0//",
    "-//webtechs//dtd mozilla html//",
];

/// §13.2.6.5 "adjust SVG tag names": the tokenizer lowercased the name, and
/// SVG's element names are camelCase.
fn adjust_svg_tag_name(name: &str) -> String {
    const MAP: &[(&str, &str)] = &[
        ("altglyph", "altGlyph"),
        ("altglyphdef", "altGlyphDef"),
        ("altglyphitem", "altGlyphItem"),
        ("animatecolor", "animateColor"),
        ("animatemotion", "animateMotion"),
        ("animatetransform", "animateTransform"),
        ("clippath", "clipPath"),
        ("feblend", "feBlend"),
        ("fecolormatrix", "feColorMatrix"),
        ("fecomponenttransfer", "feComponentTransfer"),
        ("fecomposite", "feComposite"),
        ("feconvolvematrix", "feConvolveMatrix"),
        ("fediffuselighting", "feDiffuseLighting"),
        ("fedisplacementmap", "feDisplacementMap"),
        ("fedistantlight", "feDistantLight"),
        ("fedropshadow", "feDropShadow"),
        ("feflood", "feFlood"),
        ("fefunca", "feFuncA"),
        ("fefuncb", "feFuncB"),
        ("fefuncg", "feFuncG"),
        ("fefuncr", "feFuncR"),
        ("fegaussianblur", "feGaussianBlur"),
        ("feimage", "feImage"),
        ("femerge", "feMerge"),
        ("femergenode", "feMergeNode"),
        ("femorphology", "feMorphology"),
        ("feoffset", "feOffset"),
        ("fepointlight", "fePointLight"),
        ("fespecularlighting", "feSpecularLighting"),
        ("fespotlight", "feSpotLight"),
        ("fetile", "feTile"),
        ("feturbulence", "feTurbulence"),
        ("foreignobject", "foreignObject"),
        ("glyphref", "glyphRef"),
        ("lineargradient", "linearGradient"),
        ("radialgradient", "radialGradient"),
        ("textpath", "textPath"),
    ];
    MAP.iter()
        .find(|(lower, _)| *lower == name)
        .map(|(_, proper)| (*proper).to_string())
        .unwrap_or_else(|| name.to_string())
}

/// §13.2.6.5 "adjust SVG attributes".
fn adjust_svg_attributes(attrs: &mut [TagAttr]) {
    const MAP: &[&str] = &[
        "attributeName",
        "attributeType",
        "baseFrequency",
        "baseProfile",
        "calcMode",
        "clipPathUnits",
        "diffuseConstant",
        "edgeMode",
        "filterUnits",
        "glyphRef",
        "gradientTransform",
        "gradientUnits",
        "kernelMatrix",
        "kernelUnitLength",
        "keyPoints",
        "keySplines",
        "keyTimes",
        "lengthAdjust",
        "limitingConeAngle",
        "markerHeight",
        "markerUnits",
        "markerWidth",
        "maskContentUnits",
        "maskUnits",
        "numOctaves",
        "pathLength",
        "patternContentUnits",
        "patternTransform",
        "patternUnits",
        "pointsAtX",
        "pointsAtY",
        "pointsAtZ",
        "preserveAlpha",
        "preserveAspectRatio",
        "primitiveUnits",
        "refX",
        "refY",
        "repeatCount",
        "repeatDur",
        "requiredExtensions",
        "requiredFeatures",
        "specularConstant",
        "specularExponent",
        "spreadMethod",
        "startOffset",
        "stdDeviation",
        "stitchTiles",
        "surfaceScale",
        "systemLanguage",
        "tableValues",
        "targetX",
        "targetY",
        "textLength",
        "viewBox",
        "viewTarget",
        "xChannelSelector",
        "yChannelSelector",
        "zoomAndPan",
    ];
    for a in attrs.iter_mut() {
        if let Some(proper) = MAP.iter().find(|p| p.eq_ignore_ascii_case(&a.name)) {
            a.name = (*proper).to_string();
        }
    }
}

/// §13.2.6.5 "adjust MathML attributes".
fn adjust_mathml_attributes(attrs: &mut [TagAttr]) {
    for a in attrs.iter_mut() {
        if a.name == "definitionurl" {
            a.name = "definitionURL".to_string();
        }
    }
}

/// §13.2.6.5 "adjust foreign attributes", which is the step that actually puts
/// attributes into the XLink / XML / XMLNS namespaces.
fn adjust_attributes(_ns: Namespace, attrs: &[TagAttr]) -> Vec<Attr> {
    attrs
        .iter()
        .map(|a| match a.name.as_str() {
            "xlink:actuate" | "xlink:arcrole" | "xlink:href" | "xlink:role" | "xlink:show"
            | "xlink:title" | "xlink:type" => Attr::namespaced(
                Namespace::XLink,
                "xlink",
                a.name["xlink:".len()..].to_string(),
                a.value.clone(),
            ),
            "xml:lang" | "xml:space" => Attr::namespaced(
                Namespace::Xml,
                "xml",
                a.name["xml:".len()..].to_string(),
                a.value.clone(),
            ),
            "xmlns" => Attr {
                namespace: Some(Namespace::XmlNs),
                prefix: None,
                local: "xmlns".to_string(),
                value: a.value.clone(),
            },
            "xmlns:xlink" => {
                Attr::namespaced(Namespace::XmlNs, "xmlns", "xlink", a.value.clone())
            }
            _ => Attr::new(a.name.clone(), a.value.clone()),
        })
        .collect()
}

/// Convenience for callers that only want the tree.
pub fn parse_document(input: &str, scripting: bool) -> (Dom, NodeId) {
    let tb = TreeBuilder::new(input, scripting).parse();
    let root = tb.root();
    (tb.dom, root)
}

/// Parse `input` as if it appeared inside `context_name`.
pub fn parse_fragment(
    input: &str,
    context_ns: Namespace,
    context_name: &str,
    scripting: bool,
) -> (Dom, NodeId) {
    let tb = TreeBuilder::new_fragment(input, context_ns, context_name, scripting).parse();
    let root = tb.root();
    (tb.dom, root)
}

/// Reachable only so `ElementData` stays used when features are trimmed.
#[allow(dead_code)]
fn _element_data_is_used(e: &ElementData) -> &str {
    &e.name
}
