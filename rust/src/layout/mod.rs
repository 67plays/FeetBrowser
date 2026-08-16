//! A CSS 2.1 layout engine built on containing blocks and formatting contexts.
//!
//! # Why this module exists
//!
//! `feetbrowser/layout.py` computes geometry, and much of what it computes is
//! right, but it has no vocabulary. CSS 2.1 defines almost everything it
//! defines in terms of two objects — the *containing block* a box resolves its
//! percentages and offsets against, and the *formatting context* a box
//! participates in — and the Python engine names neither. The consequences are
//! not scattered bugs; they are one absence showing up in a dozen places:
//!
//! * `height: 50%` resolves against the integer literal `0`, because there is
//!   no containing block on the block axis to ask.
//! * Adjoining margins are summed rather than collapsed, because collapsing is
//!   defined *within a block formatting context* and there is no such thing.
//! * `position: absolute` resolves against the immediate parent rather than
//!   the nearest positioned ancestor, because "nearest positioned ancestor" is
//!   the definition of a containing block and nothing computes one.
//! * Flex and grid are separate ad-hoc passes rather than formatting contexts,
//!   so they compose with nothing.
//!
//! So this module leads with the two concepts. [`ContainingBlock`] is a real
//! type that every box carries, and it distinguishes the three chains CSS 2.1
//! §10.1 distinguishes: in-flow boxes resolve against their parent's content
//! box, absolutely positioned boxes against the padding box of the nearest
//! positioned ancestor, and fixed boxes against the viewport. Formatting
//! contexts are the unit of layout: [`block`] implements the block formatting
//! context, [`inline`] the inline formatting context, and each new one — flex,
//! grid, table — plugs into the same interface instead of being a parallel
//! universe.
//!
//! # What is deliberately *not* here
//!
//! The horizontal axis of the Python engine works. `_parent_content_box` is a
//! proper inline-axis containing block, `margin: 0 auto` centring is correct
//! per §10.3.3, and a nested `width: 50%` lands where Chrome puts it. Nothing
//! here is motivated by fixing that; the inline axis is reimplemented only
//! because a containing block that exists on one axis and not the other is not
//! a containing block.
//!
//! # Status
//!
//! Compiled and tested from its own tests, wired to nothing. `layout.py`
//! remains the live path, exactly as the DOM arena in [`footnote::domtree`]
//! and the tree builder in [`footnote`] landed before it.

#![allow(dead_code)]

pub mod block;
pub mod geom;
pub mod inline;
pub mod intrinsic;
pub mod style;
pub mod text;

#[cfg(test)]
mod tests;

use std::rc::Rc;

use footnote::domtree::{Dom, NodeData, NodeId};
use geom::{BoxEdges, Rect};
use style::{ComputedStyle, Display, Float, InnerDisplay, Position, Sides};
use text::FontSource;

// ---------------------------------------------------------------------------
// Containing blocks
// ---------------------------------------------------------------------------

/// The box a given box resolves its percentages and offsets against.
///
/// CSS 2.1 §10.1. The two fields are not symmetric, and the asymmetry is the
/// entire subtlety of percentage resolution:
///
/// * `width` is **always definite**. By the time a block-level box is laid out,
///   its containing block's inline size is known, which is why percentage
///   widths, and percentage padding and margins on *all four sides* (§8.3,
///   §8.4 — they resolve against the width even vertically), always have a
///   base.
/// * `height` is `Option`. A containing block whose own height is `auto`
///   does not have one yet, and §10.5 says a percentage height against such a
///   base "computes to `auto`" — it does not compute to zero. Representing
///   that as `None` rather than `0.0` is the difference between an app shell
///   that fills the viewport and one that collapses to a 21px strip.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct ContainingBlock {
    /// Content-box inline size. Always definite.
    pub width: f32,
    /// Content-box block size, or `None` when it is `auto`/indefinite.
    pub height: Option<f32>,
    /// Document-space origin of the content box, so absolutely positioned
    /// descendants can be placed against it.
    pub x: f32,
    pub y: f32,
}

impl ContainingBlock {
    pub fn new(width: f32, height: Option<f32>) -> ContainingBlock {
        ContainingBlock { width, height, x: 0.0, y: 0.0 }
    }
    pub fn at(x: f32, y: f32, width: f32, height: Option<f32>) -> ContainingBlock {
        ContainingBlock { width, height, x, y }
    }
    /// The viewport, which is the containing block for the root element and for
    /// every `position: fixed` box.
    pub fn viewport(width: f32, height: f32) -> ContainingBlock {
        ContainingBlock { width, height: Some(height), x: 0.0, y: 0.0 }
    }
}

/// The three containing blocks in scope at a point in the tree.
///
/// A box does not have *a* containing block; it has one per positioning scheme,
/// and which one applies depends on its own `position`. Carrying all three
/// together is what makes [`ContainingBlockChain::for_position`] a lookup
/// rather than an ancestor walk at the point of use — and an ancestor walk that
/// nobody wrote is why the Python engine resolves absolute positioning against
/// the immediate parent.
#[derive(Debug, Clone, Copy)]
pub struct ContainingBlockChain {
    /// For `static` and `relative` boxes: the nearest block container ancestor's
    /// content box.
    pub flow: ContainingBlock,
    /// For `absolute` boxes: the *padding* box of the nearest ancestor whose
    /// position is not `static` (§10.1 point 3). Note padding box, not content
    /// box — an absolutely positioned child at `top: 0` sits inside its
    /// ancestor's padding, not below it.
    pub absolute: ContainingBlock,
    /// For `fixed` boxes: the viewport.
    pub fixed: ContainingBlock,
}

impl ContainingBlockChain {
    pub fn root(viewport: ContainingBlock) -> ContainingBlockChain {
        ContainingBlockChain { flow: viewport, absolute: viewport, fixed: viewport }
    }

    pub fn for_position(&self, position: Position) -> ContainingBlock {
        match position {
            Position::Absolute => self.absolute,
            Position::Fixed => self.fixed,
            _ => self.flow,
        }
    }

    /// Descend into a box's content box. If the box is positioned it becomes
    /// the containing block for absolutely positioned descendants, and it is
    /// its *padding* box that does so.
    pub fn descend(
        &self,
        style: &ComputedStyle,
        content: ContainingBlock,
        padding_box: ContainingBlock,
    ) -> ContainingBlockChain {
        ContainingBlockChain {
            flow: content,
            absolute: if style.position.is_positioned() { padding_box } else { self.absolute },
            fixed: self.fixed,
        }
    }
}

// ---------------------------------------------------------------------------
// Formatting contexts
// ---------------------------------------------------------------------------

/// The kind of formatting context a box establishes for its children.
///
/// CSS 2.1 §9.4. This is the axis along which layout algorithms are separated:
/// a box's children are laid out by exactly one of these, and the rules that
/// govern them — how margins interact, where floats may go, what a percentage
/// height means — follow from which one it is.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum FormattingContext {
    /// Block formatting context. Boxes stack vertically, adjoining margins
    /// collapse (§8.3.1), floats are positioned and contained (§9.5, §10.6.3).
    Block,
    /// Inline formatting context. Boxes flow horizontally into line boxes,
    /// leading is distributed per §10.8, baselines align.
    Inline,
    Flex,
    Grid,
    Table,
}

/// What every formatting context returns to the box that established it.
#[derive(Debug, Clone, Default)]
pub struct FcResult {
    /// The block size the content came to, before the box's own `height`
    /// property gets a say.
    pub content_height: f32,
    /// Distance from the content box top to the last line box's baseline, if
    /// the context produced one. Atomic inlines need this to sit on their
    /// parent's baseline; `None` means "align the bottom margin edge instead",
    /// which is what §10.8.1 says for a box with no in-flow line boxes.
    pub baseline: Option<f32>,
    pub fragments: Vec<Fragment>,
}

// ---------------------------------------------------------------------------
// The box tree
// ---------------------------------------------------------------------------

pub type BoxId = usize;

#[derive(Debug, Clone)]
pub enum BoxKind {
    /// A block container: it holds either block-level boxes (and so establishes
    /// or participates in a BFC) or inline-level ones (and so establishes an
    /// IFC), never a mix — the mix is what anonymous boxes exist to prevent.
    Block,
    /// A non-replaced inline box: `<b>`, `<span>`. Contributes its horizontal
    /// padding, border and margin to the line but may be split across lines.
    Inline,
    Text(String),
    /// Content with an intrinsic size the engine does not lay out: `<img>`.
    Replaced { width: f32, height: f32 },
}

#[derive(Debug, Clone)]
pub struct BoxNode {
    pub dom: Option<NodeId>,
    pub style: Rc<ComputedStyle>,
    pub kind: BoxKind,
    pub children: Vec<BoxId>,
    /// Tag name, `#text`, or `#anonymous` — for test assertions and debugging.
    pub label: String,
}

impl BoxNode {
    pub fn is_text(&self) -> bool {
        matches!(self.kind, BoxKind::Text(_))
    }
    pub fn is_anonymous(&self) -> bool {
        self.dom.is_none() && !self.is_text()
    }
    /// Block-level in its parent's formatting context, and in flow there.
    pub fn is_in_flow_block_level(&self) -> bool {
        !self.style.position.is_out_of_flow()
            && self.style.float == Float::None
            && !self.style.display.is_inline_level()
            && !self.is_text()
    }
    pub fn is_out_of_flow(&self) -> bool {
        self.style.position.is_out_of_flow()
    }
    pub fn is_floated(&self) -> bool {
        self.style.float != Float::None && !self.style.position.is_out_of_flow()
    }
}

#[derive(Debug, Clone)]
pub struct BoxTree {
    pub boxes: Vec<BoxNode>,
    pub root: BoxId,
}

impl BoxTree {
    pub fn get(&self, id: BoxId) -> &BoxNode {
        &self.boxes[id]
    }
    pub fn style(&self, id: BoxId) -> &ComputedStyle {
        &self.boxes[id].style
    }
    pub fn children(&self, id: BoxId) -> &[BoxId] {
        &self.boxes[id].children
    }

    /// Which formatting context does this box establish for its children?
    ///
    /// For a `flow` box the answer depends on the children themselves: a block
    /// container whose in-flow children are all inline-level establishes an
    /// inline formatting context, otherwise a block one. Anonymous box
    /// generation (§9.2.1.1) guarantees this is never ambiguous.
    pub fn formatting_context(&self, id: BoxId) -> FormattingContext {
        match self.style(id).display.inner() {
            InnerDisplay::Flex => FormattingContext::Flex,
            InnerDisplay::Grid => FormattingContext::Grid,
            InnerDisplay::Table => FormattingContext::Table,
            InnerDisplay::Flow => {
                let has_block = self.children(id).iter().any(|&c| {
                    let b = self.get(c);
                    b.is_in_flow_block_level()
                });
                if has_block {
                    FormattingContext::Block
                } else {
                    FormattingContext::Inline
                }
            }
        }
    }
}

// ---------------------------------------------------------------------------
// Fragments — the output
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, PartialEq)]
pub enum FragmentKind {
    Box,
    Line,
    Text(String),
}

/// One laid-out box, in document coordinates.
///
/// Positions are absolute rather than parent-relative on purpose. Margin
/// collapsing means a box's own top edge is not known until its descendants
/// have been laid out, so a relative scheme would need a fix-up pass that
/// translates subtrees; absolute coordinates plus a block formatting context
/// that carries its cursor top-down avoids that entirely.
#[derive(Debug, Clone)]
pub struct Fragment {
    pub box_id: Option<BoxId>,
    pub dom: Option<NodeId>,
    pub label: String,
    pub kind: FragmentKind,
    /// The border box: what `getBoundingClientRect` reports.
    pub border_box: Rect,
    pub edges: BoxEdges,
    /// Baseline offset from the top of the border box, when the box has one.
    pub baseline: Option<f32>,
    pub children: Vec<Fragment>,
}

impl Fragment {
    pub fn content_box(&self) -> Rect {
        self.border_box.deflate(self.edges.padding_border())
    }
    pub fn padding_box(&self) -> Rect {
        self.border_box.deflate(self.edges.border)
    }
    pub fn margin_box(&self) -> Rect {
        self.border_box.inflate(self.edges.margin)
    }

    pub fn new(kind: FragmentKind, label: String) -> Fragment {
        Fragment {
            box_id: None,
            dom: None,
            label,
            kind,
            border_box: Rect::default(),
            edges: BoxEdges::default(),
            baseline: None,
            children: Vec::new(),
        }
    }

    /// Depth-first walk, self first.
    pub fn walk<'a>(&'a self, out: &mut Vec<&'a Fragment>) {
        out.push(self);
        for c in &self.children {
            c.walk(out);
        }
    }

    pub fn find_by_label<'a>(&'a self, label: &str) -> Vec<&'a Fragment> {
        let mut all = Vec::new();
        self.walk(&mut all);
        all.into_iter().filter(|f| f.label == label).collect()
    }
}

// ---------------------------------------------------------------------------
// Layout context
// ---------------------------------------------------------------------------

/// Everything a layout pass needs that is not the box tree: fonts, the
/// viewport, and the queue of out-of-flow boxes waiting for their containing
/// block to finish.
pub struct LayoutContext<'a> {
    pub tree: &'a BoxTree,
    pub fonts: &'a dyn FontSource,
    pub viewport: ContainingBlock,
    /// Absolutely positioned boxes discovered during flow layout, held until
    /// the containing block that owns them has a final size. §10.6.4 makes an
    /// abspos box's height depend on `top`/`bottom` against a containing block
    /// that may itself be `auto`, so they cannot be laid out in place.
    pub deferred_abspos: Vec<DeferredAbspos>,
}

#[derive(Debug, Clone)]
pub struct DeferredAbspos {
    pub box_id: BoxId,
    pub containing_block: ContainingBlock,
}

impl<'a> LayoutContext<'a> {
    pub fn new(
        tree: &'a BoxTree,
        fonts: &'a dyn FontSource,
        viewport: ContainingBlock,
    ) -> LayoutContext<'a> {
        LayoutContext { tree, fonts, viewport, deferred_abspos: Vec::new() }
    }
}

/// Resolve a box's margins, borders and padding against a containing block.
///
/// Percentages on *every* side resolve against the containing block's inline
/// size, including `padding-top` and `margin-bottom` (§8.3, §8.4). That is not
/// a quirk to be smoothed over: it is what makes `padding-top: 56.25%` the
/// aspect-ratio box idiom, and resolving it against zero is why every
/// lazy-loaded image placeholder on the corpus sites has no height.
pub fn resolve_edges(style: &ComputedStyle, cb: &ContainingBlock) -> BoxEdges {
    let w = cb.width;
    BoxEdges {
        margin: Sides {
            top: style.margin.top.resolve_or_zero(w),
            right: style.margin.right.resolve_or_zero(w),
            bottom: style.margin.bottom.resolve_or_zero(w),
            left: style.margin.left.resolve_or_zero(w),
        },
        border: style.border_widths(),
        padding: Sides {
            top: style.padding.top.resolve(w).max(0.0),
            right: style.padding.right.resolve(w).max(0.0),
            bottom: style.padding.bottom.resolve(w).max(0.0),
            left: style.padding.left.resolve(w).max(0.0),
        },
    }
}

// ---------------------------------------------------------------------------
// Box tree construction
// ---------------------------------------------------------------------------

/// Build a box tree from a DOM and a per-element computed style.
///
/// `styles` is indexed by `NodeId`; a `None` entry means the element has no
/// style and is skipped, which is how `display: none`, `<head>` and friends
/// leave the tree.
pub struct BoxTreeBuilder<'a> {
    dom: &'a Dom,
    styles: &'a dyn Fn(NodeId) -> Option<Rc<ComputedStyle>>,
    boxes: Vec<BoxNode>,
}

impl<'a> BoxTreeBuilder<'a> {
    pub fn new(dom: &'a Dom, styles: &'a dyn Fn(NodeId) -> Option<Rc<ComputedStyle>>) -> Self {
        BoxTreeBuilder { dom, styles, boxes: Vec::new() }
    }

    pub fn build(mut self, root: NodeId) -> Option<BoxTree> {
        let root_box = self.build_node(root)?;
        Some(BoxTree { boxes: self.boxes, root: root_box })
    }

    fn push(&mut self, node: BoxNode) -> BoxId {
        self.boxes.push(node);
        self.boxes.len() - 1
    }

    fn build_node(&mut self, node: NodeId) -> Option<BoxId> {
        let style = (self.styles)(node)?;
        if style.display == Display::None {
            return None;
        }
        let label = self.dom.tag_name(node).unwrap_or("#anonymous").to_string();

        // Replaced elements: `<img>` with width/height attributes is the only
        // form this engine can size without a decoder.
        if self.dom.is_html_element(node, "img") || self.dom.is_html_element(node, "canvas") {
            let w = self.attr_px(node, "width").unwrap_or(0.0);
            let h = self.attr_px(node, "height").unwrap_or(0.0);
            return Some(self.push(BoxNode {
                dom: Some(node),
                style,
                kind: BoxKind::Replaced { width: w, height: h },
                children: Vec::new(),
                label,
            }));
        }

        let mut children = Vec::new();
        for child in self.dom.children(node).collect::<Vec<_>>() {
            match self.dom.data(child) {
                Ok(NodeData::Text(t)) => {
                    let t = t.clone();
                    if let Some(id) = self.build_text(child, &t, &style) {
                        children.push(id);
                    }
                }
                Ok(NodeData::Element(_)) => {
                    if let Some(id) = self.build_node(child) {
                        children.push(id);
                    }
                }
                _ => {}
            }
        }

        let kind = if style.display.is_inline_level() && !style.display.is_atomic_inline() {
            BoxKind::Inline
        } else {
            BoxKind::Block
        };
        let id = self.push(BoxNode { dom: Some(node), style: style.clone(), kind, children, label });
        self.fix_up_anonymous(id, &style);
        Some(id)
    }

    fn build_text(
        &mut self,
        node: NodeId,
        text: &str,
        parent_style: &Rc<ComputedStyle>,
    ) -> Option<BoxId> {
        // §9.2.2.1: white space that would be collapsed away entirely does not
        // generate a box. Dropping it here is what stops the newlines between
        // two block-level `<div>`s from creating a stray line box.
        if !parent_style.white_space.preserves_spaces() && text.trim().is_empty() {
            return None;
        }
        // A text box inherits its parent's *inherited* properties and nothing
        // else. Cloning the whole style would give the text node its parent's
        // `float` and `position` as well, and a text run that reports itself as
        // floated is skipped by every pass that walks inline content — which is
        // how a float's own text disappears from its intrinsic width.
        let mut style = parent_style.inherited_only();
        // `vertical-align` is not inherited, but the text of an inline box does
        // move with it, so it is carried onto the run deliberately.
        style.vertical_align = parent_style.vertical_align;
        Some(self.push(BoxNode {
            dom: Some(node),
            style: Rc::new(style),
            kind: BoxKind::Text(text.to_string()),
            children: Vec::new(),
            label: "#text".to_string(),
        }))
    }

    fn attr_px(&self, node: NodeId, name: &str) -> Option<f32> {
        self.dom.get_attribute(node, name).ok().flatten()?.trim().parse().ok()
    }

    /// CSS 2.1 §9.2.1.1: "if a block container box has a block-level box
    /// inside it, then we force it to have only block-level boxes inside it" —
    /// consecutive inline-level siblings get wrapped in an anonymous block box.
    ///
    /// Without this a block container has no single answer to "which formatting
    /// context do I establish", and every later rule that says "within a block
    /// formatting context" has nothing to attach to.
    fn fix_up_anonymous(&mut self, id: BoxId, style: &Rc<ComputedStyle>) {
        if style.display.inner() != InnerDisplay::Flow {
            return;
        }
        let children = self.boxes[id].children.clone();
        let has_block = children.iter().any(|&c| self.boxes[c].is_in_flow_block_level());
        if !has_block {
            return;
        }
        let anon_style = Rc::new(style.inherited_only());
        let mut out: Vec<BoxId> = Vec::new();
        let mut run: Vec<BoxId> = Vec::new();
        for &c in &children {
            let b = &self.boxes[c];
            // Out-of-flow boxes and floats do not force a wrapper and do not
            // break a run — they are removed from the flow anyway.
            if b.is_out_of_flow() || b.is_floated() {
                if run.is_empty() {
                    out.push(c);
                } else {
                    run.push(c);
                }
                continue;
            }
            if b.is_in_flow_block_level() {
                if !run.is_empty() {
                    let anon = self.make_anonymous(&anon_style, std::mem::take(&mut run));
                    out.push(anon);
                }
                out.push(c);
            } else {
                run.push(c);
            }
        }
        if !run.is_empty() {
            let anon = self.make_anonymous(&anon_style, run);
            out.push(anon);
        }
        self.boxes[id].children = out;
    }

    fn make_anonymous(&mut self, style: &Rc<ComputedStyle>, children: Vec<BoxId>) -> BoxId {
        self.push(BoxNode {
            dom: None,
            style: style.clone(),
            kind: BoxKind::Block,
            children,
            label: "#anonymous".to_string(),
        })
    }
}

// ---------------------------------------------------------------------------
// Entry point
// ---------------------------------------------------------------------------

/// Lay out a box tree into the viewport and return the root fragment.
///
/// The root element's containing block is the viewport (§10.1 point 1), whose
/// height is definite — which is why `html { height: 100% }` works at all and
/// why the chain `html, body, .shell { height: 100% }` is a chain of definite
/// heights rather than a chain of zeros.
pub fn layout_document(
    tree: &BoxTree,
    fonts: &dyn FontSource,
    viewport_width: f32,
    viewport_height: f32,
) -> Fragment {
    let viewport = ContainingBlock::viewport(viewport_width, viewport_height);
    let mut ctx = LayoutContext::new(tree, fonts, viewport);
    block::layout_root(&mut ctx, tree.root)
}
