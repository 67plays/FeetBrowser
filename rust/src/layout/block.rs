//! The block formatting context.
//!
//! Three rules live here, and all three are stated in CSS 2.1 as properties of
//! a *block formatting context* rather than of a box — which is why an engine
//! without the concept cannot express any of them:
//!
//! * **§8.3.1, margin collapsing.** Adjoining vertical margins collapse to a
//!   single margin whose size is the maximum of the positive parts plus the
//!   minimum of the negative parts. Margins never collapse across a BFC
//!   boundary, which is the whole reason `overflow: hidden` "fixes" a stray
//!   gap.
//! * **§9.5 / §10.6.3, float containment.** Floats belong to the BFC they were
//!   generated in. A BFC root's auto height grows to contain its own floats and
//!   a float from outside never intrudes into it; a plain block does neither.
//!   The Python engine makes *every* block contain its floats, which is
//!   non-conformant but flattering — noted, and reproduced only where the spec
//!   agrees.
//! * **§10.6, heights.** An auto height is the distance to the bottom edge of
//!   the last in-flow child's bottom margin — *excluding* margins that
//!   collapsed through. A definite height is resolved against the containing
//!   block, and §10.5's "computes to auto" case is the one the Python engine
//!   turns into a zero.

use std::rc::Rc;

use super::geom::{BoxEdges, Rect};
use super::inline;
use super::intrinsic;
use super::style::{
    BoxSizing, Clear, ComputedStyle, Display, Float, LengthPercentage, Margin, Position, Sides,
    Size,
};
use super::{
    resolve_edges, BoxId, BoxKind, ContainingBlock, ContainingBlockChain, FcResult, FormattingContext,
    Fragment, FragmentKind, LayoutContext,
};

// ---------------------------------------------------------------------------
// Collapsed margins
// ---------------------------------------------------------------------------

/// A set of adjoining margins, reduced to the two numbers that determine the
/// result.
///
/// CSS 2.1 §8.3.1: "the resulting margin width is the maximum of the adjoining
/// margin widths. In the case of negative margins, the maximum of the absolute
/// values of the negative adjoining margins is deducted from the maximum of the
/// positive adjoining margins." Keeping the positive maximum and the negative
/// minimum separately is exactly enough state to satisfy that, and it is
/// associative, so margins can be adjoined in any order as they are discovered
/// walking down the tree.
#[derive(Debug, Clone, Copy, PartialEq, Default)]
pub struct CollapsedMargin {
    max_positive: f32,
    min_negative: f32,
}

impl CollapsedMargin {
    pub fn zero() -> CollapsedMargin {
        CollapsedMargin { max_positive: 0.0, min_negative: 0.0 }
    }

    pub fn new(margin: f32) -> CollapsedMargin {
        if margin >= 0.0 {
            CollapsedMargin { max_positive: margin, min_negative: 0.0 }
        } else {
            CollapsedMargin { max_positive: 0.0, min_negative: margin }
        }
    }

    pub fn adjoin(&mut self, other: CollapsedMargin) {
        self.max_positive = self.max_positive.max(other.max_positive);
        self.min_negative = self.min_negative.min(other.min_negative);
    }

    pub fn adjoin_margin(&mut self, margin: f32) {
        self.adjoin(CollapsedMargin::new(margin));
    }

    /// The single margin the adjoining set resolves to.
    pub fn solve(&self) -> f32 {
        self.max_positive + self.min_negative
    }

    pub fn is_zero(&self) -> bool {
        self.max_positive == 0.0 && self.min_negative == 0.0
    }
}

// ---------------------------------------------------------------------------
// Floats
// ---------------------------------------------------------------------------

/// The floats belonging to one block formatting context, in document
/// coordinates.
#[derive(Debug, Clone, Default)]
pub struct FloatContext {
    pub left: Vec<Rect>,
    pub right: Vec<Rect>,
}

impl FloatContext {
    /// The band of free inline space at a given block position, within
    /// `[left_edge, right_edge]`.
    pub fn band(&self, y: f32, height: f32, left_edge: f32, right_edge: f32) -> (f32, f32) {
        let bottom = y + height.max(0.001);
        let mut l = left_edge;
        let mut r = right_edge;
        for f in &self.left {
            if f.intersects_vertically(y, bottom) {
                l = l.max(f.right());
            }
        }
        for f in &self.right {
            if f.intersects_vertically(y, bottom) {
                r = r.min(f.x);
            }
        }
        (l, r.max(l))
    }

    /// The next block position at or below `y` where the band changes — used to
    /// step downwards when a box does not fit beside the current floats.
    pub fn next_change(&self, y: f32) -> Option<f32> {
        let mut best: Option<f32> = None;
        for f in self.left.iter().chain(self.right.iter()) {
            let b = f.bottom();
            if b > y + 0.001 {
                best = Some(match best {
                    Some(v) => v.min(b),
                    None => b,
                });
            }
        }
        best
    }

    /// Place a float's margin box: the topmost position at or below `y` where a
    /// box of this size fits, packed against the requested side (§9.5.1).
    pub fn place(
        &mut self,
        side: Float,
        width: f32,
        height: f32,
        mut y: f32,
        left_edge: f32,
        right_edge: f32,
    ) -> Rect {
        loop {
            let (l, r) = self.band(y, height, left_edge, right_edge);
            if r - l >= width || self.next_change(y).is_none() {
                let x = if side == Float::Right { (r - width).max(left_edge) } else { l };
                let rect = Rect::new(x, y, width, height);
                if side == Float::Right {
                    self.right.push(rect);
                } else {
                    self.left.push(rect);
                }
                return rect;
            }
            y = self.next_change(y).unwrap();
        }
    }

    /// The block position below every float that `clear` applies to (§9.5.2).
    pub fn clear_bottom(&self, clear: Clear) -> f32 {
        let mut y = f32::NEG_INFINITY;
        if matches!(clear, Clear::Left | Clear::Both) {
            for f in &self.left {
                y = y.max(f.bottom());
            }
        }
        if matches!(clear, Clear::Right | Clear::Both) {
            for f in &self.right {
                y = y.max(f.bottom());
            }
        }
        y
    }

    pub fn lowest_bottom(&self) -> f32 {
        self.left
            .iter()
            .chain(self.right.iter())
            .map(|f| f.bottom())
            .fold(f32::NEG_INFINITY, f32::max)
    }
}

// ---------------------------------------------------------------------------
// BFC state
// ---------------------------------------------------------------------------

/// The mutable state of one block formatting context as layout walks down it.
///
/// `cur_y` and `pending` together are the "current position": everything above
/// `cur_y` is final, and `pending` holds the margins that are still adjoining
/// and so have not yet decided where the next border edge goes. Carrying them
/// top-down is what lets a box whose own top edge is not yet known — because it
/// has no top border or padding, so its margin is still collapsing with its
/// descendants' — lay its children out in final document coordinates anyway.
/// The alternative, laying subtrees out relatively and translating them
/// afterwards, cannot place floats correctly, because a float's position is
/// defined against the BFC and not against its parent.
pub struct BfcState {
    pub cur_y: f32,
    pub pending: CollapsedMargin,
    /// Document `y` of each point where the pending margin was committed and a
    /// border edge was fixed. A box with no top border or padding takes its own
    /// top edge from the first commit that happens inside it.
    pub commits: Vec<f32>,
    pub floats: FloatContext,
    /// Inline extent of the BFC root's content box, for float placement.
    pub left_edge: f32,
    pub right_edge: f32,
}

impl BfcState {
    pub fn new(y: f32, left_edge: f32, right_edge: f32) -> BfcState {
        BfcState {
            cur_y: y,
            pending: CollapsedMargin::zero(),
            commits: Vec::new(),
            floats: FloatContext::default(),
            left_edge,
            right_edge,
        }
    }

    /// Resolve the adjoining margins and fix a border edge here.
    pub fn commit(&mut self) -> f32 {
        let y = self.cur_y + self.pending.solve();
        self.pending = CollapsedMargin::zero();
        self.cur_y = y;
        self.commits.push(y);
        y
    }

    pub fn mark(&self) -> usize {
        self.commits.len()
    }

    pub fn commit_at(&self, mark: usize) -> Option<f32> {
        self.commits.get(mark).copied()
    }

    /// Where a border edge would land if the pending margins were committed
    /// now, without committing them.
    pub fn hypothetical(&self) -> f32 {
        self.cur_y + self.pending.solve()
    }
}

// ---------------------------------------------------------------------------
// Used widths — §10.3.3
// ---------------------------------------------------------------------------

/// The result of solving the inline-axis equation for a block-level box.
#[derive(Debug, Clone, Copy)]
pub struct UsedInline {
    pub content_width: f32,
    pub margin_left: f32,
    pub margin_right: f32,
}

/// Convert a specified `width` into a content width, honouring `box-sizing`.
///
/// `box-sizing: border-box` means the specified value covers padding and
/// border, so the content width is what is left. The Python engine applies this
/// behaviour unconditionally — every document is treated as `border-box`
/// whether or not it asked — which is right for the modern app-shell half of
/// the web and wrong for the ~90% of pages that never set the property.
fn content_from_specified(specified: f32, style: &ComputedStyle, axis_pb: f32) -> f32 {
    match style.box_sizing {
        BoxSizing::ContentBox => specified,
        BoxSizing::BorderBox => specified - axis_pb,
    }
    .max(0.0)
}

/// CSS 2.1 §10.3.3, the inline-axis equation for a block-level, non-replaced,
/// in-flow box:
///
/// ```text
/// margin-left + border-left + padding-left + width
///   + padding-right + border-right + margin-right = width of containing block
/// ```
///
/// with the rules for `auto` values that follow it: if `width` is not `auto`
/// and the total is over-constrained, the *end* margin is ignored and solved
/// for; if exactly one of the three is `auto` it absorbs the slack; if both
/// margins are `auto` they are set equal, which is what centres a box.
pub fn solve_inline(
    style: &ComputedStyle,
    edges: &BoxEdges,
    cb_width: f32,
    shrink_to_fit: Option<(f32, f32)>,
) -> UsedInline {
    let pb = edges.inline_padding_border();
    let ml_auto = style.margin.left.is_auto();
    let mr_auto = style.margin.right.is_auto();
    let mut ml = edges.margin.left;
    let mut mr = edges.margin.right;

    // A specified length or percentage settles the width outright. The
    // keyword sizes and the `auto` case both need intrinsic sizes, which the
    // caller supplies only for the boxes that actually shrink to fit — floats,
    // inline-blocks, absolutely positioned boxes and table cells. A block-level
    // in-flow box does not shrink to fit; it fills its containing block, so
    // asking for its intrinsic size would be both wrong and quadratic.
    let mut width = style
        .width
        .resolve(Some(cb_width))
        .map(|w| content_from_specified(w, style, pb));

    if width.is_none() {
        let outer_pb = pb;
        match (style.width, shrink_to_fit) {
            (Size::MinContent, Some((min_c, _))) => width = Some((min_c - outer_pb).max(0.0)),
            (Size::MaxContent, Some((_, max_c))) => width = Some((max_c - outer_pb).max(0.0)),
            (_, Some((min_c, max_c))) => {
                let available = (cb_width - ml - mr - pb).max(0.0);
                width = Some(shrink_to_fit_width(min_c, max_c, available, pb));
            }
            _ => {}
        }
    }

    match width {
        None => {
            // `width: auto` absorbs all the slack; auto margins become 0.
            if ml_auto {
                ml = 0.0;
            }
            if mr_auto {
                mr = 0.0;
            }
            let w = (cb_width - ml - mr - pb).max(0.0);
            UsedInline { content_width: w, margin_left: ml, margin_right: mr }
        }
        Some(w) => {
            let slack = cb_width - w - pb - ml - mr;
            match (ml_auto, mr_auto) {
                (true, true) => {
                    // §10.3.3: "their used values are equal" — this is
                    // `margin: 0 auto` centring.
                    let half = (slack + ml + mr) / 2.0;
                    let half = half.max(0.0);
                    UsedInline { content_width: w, margin_left: half, margin_right: half }
                }
                (true, false) => {
                    UsedInline { content_width: w, margin_left: (ml + slack).max(0.0), margin_right: mr }
                }
                (false, true) => {
                    UsedInline { content_width: w, margin_left: ml, margin_right: (mr + slack).max(0.0) }
                }
                (false, false) => {
                    // Over-constrained: the end margin is ignored and solved
                    // for, so the box stays flush with the start edge.
                    UsedInline { content_width: w, margin_left: ml, margin_right: mr + slack }
                }
            }
        }
    }
}

/// Re-solve the inline equation with the width pinned, so that clamping by
/// `min-width`/`max-width` still goes through the `auto` margin rules.
pub fn solve_with_fixed_width(
    style: &ComputedStyle,
    edges: &BoxEdges,
    cb_width: f32,
    content_width: f32,
) -> UsedInline {
    let mut forced = style.clone();
    forced.width = Size::LengthPercentage(LengthPercentage::Px(match style.box_sizing {
        BoxSizing::ContentBox => content_width,
        BoxSizing::BorderBox => content_width + edges.inline_padding_border(),
    }));
    solve_inline(&forced, edges, cb_width, None)
}

/// CSS 2.1 §10.3.5: `min(max(preferred minimum width, available width),
/// preferred width)`, where both preferred widths are *outer* — they include
/// the box's own padding and border, which is why intrinsic sizing has to know
/// about the box model and why a version that sums only text advances gives
/// every float, table cell and flex item the wrong number.
pub fn shrink_to_fit_width(min_content: f32, max_content: f32, available: f32, pb: f32) -> f32 {
    let avail_outer = available + pb;
    let w = max_content.min(avail_outer.max(min_content));
    (w - pb).max(0.0)
}

/// Clamp a content width by `min-width` / `max-width` (§10.4), returning the
/// clamped content width, or `None` if no clamping applied.
fn clamp_width(style: &ComputedStyle, content: f32, cb_width: f32, pb: f32) -> Option<f32> {
    let to_content = |v: f32| match style.box_sizing {
        BoxSizing::ContentBox => v,
        BoxSizing::BorderBox => (v - pb).max(0.0),
    };
    if let Some(max) = style.max_width.resolve(Some(cb_width)) {
        let max = to_content(max);
        if content > max {
            return Some(max);
        }
    }
    if let Some(min) = style.min_width.resolve(Some(cb_width)) {
        let min = to_content(min);
        if content < min {
            return Some(min);
        }
    }
    None
}

/// Clamp a content height by `min-height` / `max-height` (§10.7).
fn clamp_height(style: &ComputedStyle, content: f32, cb_height: Option<f32>, pb: f32) -> f32 {
    let to_content = |v: f32| match style.box_sizing {
        BoxSizing::ContentBox => v,
        BoxSizing::BorderBox => (v - pb).max(0.0),
    };
    let mut h = content;
    if let Some(max) = style.max_height.resolve(cb_height) {
        h = h.min(to_content(max));
    }
    if let Some(min) = style.min_height.resolve(cb_height) {
        h = h.max(to_content(min));
    }
    h
}

// ---------------------------------------------------------------------------
// Entry points
// ---------------------------------------------------------------------------

/// Lay out the root element against the viewport.
pub fn layout_root(ctx: &mut LayoutContext, root: BoxId) -> Fragment {
    let viewport = ctx.viewport;
    let chain = ContainingBlockChain::root(viewport);
    let mut bfc = BfcState::new(0.0, 0.0, viewport.width);
    let mut statics = Vec::new();
    let mut frag = layout_block_level(ctx, root, viewport.x, &chain, &mut bfc, &mut statics);
    // Nothing can escape the initial containing block.
    let _ = bfc.commit();

    // Everything absolutely positioned whose containing block is the initial
    // one, plus every fixed box, lands here.
    let abs = collect_abspos(ctx, root);
    let padding_cb = viewport;
    let mut extra = Vec::new();
    for id in abs {
        let cb = match ctx.tree.style(id).position {
            Position::Fixed => ctx.viewport,
            _ => padding_cb,
        };
        let sp = statics.iter().find(|(b, _, _)| *b == id).map(|(_, x, y)| (*x, *y));
        extra.push(layout_absolute(ctx, id, &cb, sp));
    }
    frag.children.extend(extra);
    frag
}

/// Is this box's content going to produce at least one line box?
///
/// Needed before laying out an inline formatting context, because a block whose
/// inline content is empty collapses through — its top and bottom margins
/// adjoin each other — whereas one with a single line box does not.
fn has_inline_content(ctx: &LayoutContext, id: BoxId) -> bool {
    let node = ctx.tree.get(id);
    match &node.kind {
        BoxKind::Text(t) => !t.trim().is_empty() || node.style.white_space.preserves_spaces(),
        BoxKind::Replaced { .. } => true,
        _ => {
            if node.style.display.is_atomic_inline() && node.dom.is_some() {
                return true;
            }
            node.children.iter().any(|&c| {
                let cn = ctx.tree.get(c);
                !cn.is_out_of_flow() && has_inline_content(ctx, c)
            })
        }
    }
}

/// Lay out one block-level box into the block formatting context `bfc`.
///
/// The box's position is expressed in document coordinates throughout. `x` is
/// the inline-axis origin of the *containing block's content box*, not of this
/// box — the box's own margin decides where it starts within that.
pub fn layout_block_level(
    ctx: &mut LayoutContext,
    id: BoxId,
    cb_x: f32,
    chain: &ContainingBlockChain,
    bfc: &mut BfcState,
    statics: &mut Vec<(BoxId, f32, f32)>,
) -> Fragment {
    let style = ctx.tree.style(id).clone();
    let cb = chain.flow;
    let edges = resolve_edges(&style, &cb);
    let pb_inline = edges.inline_padding_border();
    let pb_block = edges.block_padding_border();

    // --- inline axis, §10.3.3 -------------------------------------------
    // A block-level in-flow box fills its containing block; only the boxes that
    // shrink to fit need intrinsic sizes, and computing them for every block
    // would make layout quadratic for no benefit.
    let stf = if needs_intrinsic(&style) {
        Some(intrinsic::outer_intrinsic(ctx, id, &cb))
    } else {
        None
    };
    let mut used = solve_inline(&style, &edges, cb.width, stf);
    // §10.4: if the used width violates min-width or max-width, the whole
    // inline equation is solved again with that constraint as the width — the
    // auto-margin rules have to be re-applied, not patched afterwards, or a
    // clamped `margin: 0 auto` box stops being centred.
    if let Some(clamped) = clamp_width(&style, used.content_width, cb.width, pb_inline) {
        used = solve_with_fixed_width(&style, &edges, cb.width, clamped);
    }
    let content_width = used.content_width;

    // --- block axis, §10.5 / §10.6 --------------------------------------
    // A percentage height resolves against the containing block's height, and
    // when that height is itself `auto` — `cb.height == None` — the percentage
    // "computes to auto". `Option` rather than a `0.0` sentinel is the whole
    // point: `Some(0.0)` is a real zero-height containing block, `None` is a
    // percentage that never had a base.
    let definite_height = style.height.resolve(cb.height);

    let mut x = cb_x + used.margin_left;
    let establishes_bfc = style.establishes_bfc();
    let own_fc = ctx.tree.formatting_context(id);
    let inline_content = own_fc == FormattingContext::Inline && has_inline_content(ctx, id);

    // --- clearance, §9.5.2 ----------------------------------------------
    bfc.pending.adjoin_margin(edges.margin.top);
    if style.clear != Clear::None {
        let clear_y = bfc.floats.clear_bottom(style.clear);
        if clear_y.is_finite() && clear_y > bfc.hypothetical() {
            bfc.pending = CollapsedMargin::zero();
            bfc.cur_y = clear_y;
        }
    }

    // A box that establishes a BFC must not overlap the floats in the BFC it
    // lives in (§9.5): it is narrowed and shifted rather than flowing under.
    if establishes_bfc {
        let y = bfc.hypothetical();
        let (l, r) = bfc.floats.band(y, definite_height.unwrap_or(1.0), bfc.left_edge, bfc.right_edge);
        if l > cb_x {
            x = l + used.margin_left;
        }
        let _ = r;
    }

    let separates_top = edges.border.top > 0.0
        || edges.padding.top > 0.0
        || establishes_bfc
        || inline_content
        || own_fc != FormattingContext::Block;

    let mark = bfc.mark();
    let mut fixed_top: Option<f32> = None;
    if separates_top {
        let y = bfc.commit();
        fixed_top = Some(y);
        bfc.cur_y = y + edges.border.top + edges.padding.top;
    }

    // --- children --------------------------------------------------------
    let content_x = x + edges.border.left + edges.padding.left;
    let content_cb_height = definite_height.or_else(|| {
        // A definite height established by `min-height` alone still gives
        // percentage children a base; anything else stays indefinite.
        None
    });
    let content_cb = ContainingBlock::at(
        content_x,
        bfc.cur_y,
        content_width,
        content_cb_height,
    );
    let padding_cb = ContainingBlock::at(
        x + edges.border.left,
        fixed_top.unwrap_or(bfc.cur_y) + edges.border.top,
        content_width + edges.padding.inline_sum(),
        definite_height.map(|h| h + edges.padding.block_sum()),
    );
    let child_chain = chain.descend(&style, content_cb, padding_cb);

    let content_top_guess = bfc.cur_y;
    let (children_frags, baseline, inner_height) = if establishes_bfc {
        // A BFC root gets its own cursor and its own float list: nothing inside
        // it collapses with, or flows around, anything outside it.
        let mut own_bfc = BfcState::new(bfc.cur_y, content_x, content_x + content_width);
        let r = layout_children(ctx, id, &content_cb, &child_chain, &mut own_bfc, statics, own_fc);
        let mut h = r.content_height;
        // §10.6.3: a BFC root's auto height contains its floats.
        let lowest = own_bfc.floats.lowest_bottom();
        if lowest.is_finite() {
            h = h.max(lowest - content_top_guess);
        }
        (r.fragments, r.baseline, h)
    } else {
        let r = layout_children(ctx, id, &content_cb, &child_chain, bfc, statics, own_fc);
        (r.fragments, r.baseline, r.content_height)
    };

    // --- resolve the top edge -------------------------------------------
    let border_top_y = match fixed_top {
        Some(t) => t,
        None => match bfc.commit_at(mark) {
            // The first border edge fixed inside us is our own: with no top
            // border or padding, our margin collapsed with our first child's,
            // so the two boxes share a top edge.
            Some(y) => y,
            // Nothing was committed: everything inside collapsed through.
            None => bfc.hypothetical(),
        },
    };
    let content_top = border_top_y + edges.border.top + edges.padding.top;

    // --- resolve the height, §10.6 ---------------------------------------
    let auto_height = if establishes_bfc || own_fc != FormattingContext::Block {
        inner_height
    } else {
        // In-flow children were laid out into the shared BFC, so the content
        // bottom is wherever the cursor got to — deliberately *not* including
        // the still-pending margin, which is §8.3.1's "the bottom margin of an
        // in-flow block box collapses with its last child's".
        (bfc.cur_y - content_top).max(0.0)
    };
    let mut content_height = definite_height.unwrap_or(auto_height);
    content_height = clamp_height(&style, content_height, cb.height, pb_block);

    let separates_bottom = edges.border.bottom > 0.0
        || edges.padding.bottom > 0.0
        || establishes_bfc
        || inline_content
        || own_fc != FormattingContext::Block
        || definite_height.is_some()
        || (content_height - auto_height).abs() > 0.001;

    if !establishes_bfc && own_fc == FormattingContext::Block {
        if separates_bottom {
            bfc.cur_y = content_top + content_height + edges.padding.bottom + edges.border.bottom;
            bfc.pending = CollapsedMargin::zero();
        }
        // Otherwise our bottom margin simply joins whatever is already pending.
    } else {
        bfc.cur_y = content_top + content_height + edges.padding.bottom + edges.border.bottom;
        bfc.pending = CollapsedMargin::zero();
    }
    bfc.pending.adjoin_margin(edges.margin.bottom);

    let border_box = Rect::new(
        x,
        border_top_y,
        content_width + pb_inline,
        content_height + pb_block,
    );

    let mut frag = make_fragment(ctx, id, border_box, edges);
    frag.baseline = baseline.map(|b| b + edges.border.top + edges.padding.top);
    frag.children = children_frags;

    // Relative positioning offsets the box after layout without disturbing
    // anything else (§9.4.3).
    if style.position == Position::Relative {
        let (dx, dy) = relative_offset(&style, &cb);
        translate(&mut frag, dx, dy);
    }

    // Absolutely positioned descendants whose containing block is this box.
    if style.position.is_positioned() {
        drain_abspos(ctx, id, &frag, statics);
        let extra = take_abspos_fragments(ctx, id, &frag, statics);
        frag.children.extend(extra);
    }
    frag
}

fn needs_intrinsic(style: &ComputedStyle) -> bool {
    matches!(style.width, Size::MinContent | Size::MaxContent | Size::FitContent)
}

fn relative_offset(style: &ComputedStyle, cb: &ContainingBlock) -> (f32, f32) {
    let dx = match (style.inset.left.resolve(cb.width), style.inset.right.resolve(cb.width)) {
        (Some(l), _) => l,
        (None, Some(r)) => -r,
        _ => 0.0,
    };
    let base = cb.height.unwrap_or(0.0);
    let dy = match (style.inset.top.resolve(base), style.inset.bottom.resolve(base)) {
        (Some(t), _) => t,
        (None, Some(b)) => -b,
        _ => 0.0,
    };
    (dx, dy)
}

pub fn translate(frag: &mut Fragment, dx: f32, dy: f32) {
    frag.border_box = frag.border_box.translate(dx, dy);
    for c in &mut frag.children {
        translate(c, dx, dy);
    }
}

fn make_fragment(ctx: &LayoutContext, id: BoxId, border_box: Rect, edges: BoxEdges) -> Fragment {
    let node = ctx.tree.get(id);
    Fragment {
        box_id: Some(id),
        dom: node.dom,
        label: node.label.clone(),
        kind: FragmentKind::Box,
        border_box,
        edges,
        baseline: None,
        children: Vec::new(),
    }
}

/// Lay out the children of a block container, dispatching on the formatting
/// context it establishes.
///
/// This is the seam every future formatting context plugs into: flex and grid
/// become new arms here rather than new passes elsewhere, and because they
/// receive the same [`ContainingBlock`] and return the same [`FcResult`], they
/// compose with block and inline layout instead of running beside them.
fn layout_children(
    ctx: &mut LayoutContext,
    id: BoxId,
    content_cb: &ContainingBlock,
    chain: &ContainingBlockChain,
    bfc: &mut BfcState,
    statics: &mut Vec<(BoxId, f32, f32)>,
    fc: FormattingContext,
) -> FcResult {
    match fc {
        FormattingContext::Inline => {
            let r = inline::layout_inline_context(ctx, id, content_cb, chain, bfc, statics);
            bfc.cur_y = content_cb.y + r.content_height;
            r
        }
        // Flex, grid and table are not implemented yet. Laying their children
        // out as a block formatting context is wrong, but it is wrong in a way
        // that keeps the tree traversable and the sizes finite, rather than
        // dropping the subtree.
        _ => layout_block_children(ctx, id, content_cb, chain, bfc, statics),
    }
}

fn layout_block_children(
    ctx: &mut LayoutContext,
    id: BoxId,
    content_cb: &ContainingBlock,
    chain: &ContainingBlockChain,
    bfc: &mut BfcState,
    statics: &mut Vec<(BoxId, f32, f32)>,
) -> FcResult {
    let children = ctx.tree.children(id).to_vec();
    let start_y = bfc.cur_y;
    let mut frags = Vec::new();
    for child in children {
        let node = ctx.tree.get(child);
        if node.is_out_of_flow() {
            // Record the static position — where the box would have gone had it
            // been in flow — so `position: absolute` with no offsets is not a
            // guess.
            statics.push((child, content_cb.x, bfc.hypothetical()));
            continue;
        }
        if node.is_floated() {
            frags.push(layout_float(ctx, child, content_cb, chain, bfc, statics));
            continue;
        }
        frags.push(layout_block_level(ctx, child, content_cb.x, chain, bfc, statics));
    }
    FcResult {
        content_height: (bfc.cur_y - start_y).max(0.0),
        baseline: None,
        fragments: frags,
    }
}

/// §9.5.1: a float is shrink-to-fit sized, placed as high as it will go and as
/// far to its side as it will go, and taken out of the normal flow so the
/// block cursor does not advance past it.
pub fn layout_float(
    ctx: &mut LayoutContext,
    id: BoxId,
    content_cb: &ContainingBlock,
    chain: &ContainingBlockChain,
    bfc: &mut BfcState,
    statics: &mut Vec<(BoxId, f32, f32)>,
) -> Fragment {
    let style = ctx.tree.style(id).clone();
    let edges = resolve_edges(&style, content_cb);
    let pb_inline = edges.inline_padding_border();
    let pb_block = edges.block_padding_border();

    let stf = intrinsic::outer_intrinsic(ctx, id, content_cb);
    let used = solve_inline(&style, &edges, content_cb.width, Some(stf));
    let mut content_width = used.content_width;
    if let Some(c) = clamp_width(&style, content_width, content_cb.width, pb_inline) {
        content_width = c;
    }

    let definite_height = style.height.resolve(content_cb.height);

    // A float always establishes a block formatting context of its own.
    let y0 = bfc.hypothetical() + edges.margin.top;
    let mut own = BfcState::new(y0 + edges.border.top + edges.padding.top, 0.0, content_width);
    let content_cb_inner =
        ContainingBlock::at(0.0, own.cur_y, content_width, definite_height);
    let padding_cb = ContainingBlock::at(
        0.0,
        y0,
        content_width + edges.padding.inline_sum(),
        definite_height.map(|h| h + edges.padding.block_sum()),
    );
    let child_chain = chain.descend(&style, content_cb_inner, padding_cb);
    let fc = ctx.tree.formatting_context(id);
    let r = layout_children(ctx, id, &content_cb_inner, &child_chain, &mut own, statics, fc);
    let mut auto_h = r.content_height;
    let lowest = own.floats.lowest_bottom();
    if lowest.is_finite() {
        auto_h = auto_h.max(lowest - own.cur_y);
    }
    let content_height =
        clamp_height(&style, definite_height.unwrap_or(auto_h), content_cb.height, pb_block);

    let margin_w = content_width + pb_inline + edges.margin.inline_sum();
    let margin_h = content_height + pb_block + edges.margin.block_sum();
    let placed = bfc.floats.place(
        style.float,
        margin_w,
        margin_h,
        y0 - edges.margin.top,
        bfc.left_edge,
        bfc.right_edge,
    );

    let border_box = Rect::new(
        placed.x + edges.margin.left,
        placed.y + edges.margin.top,
        content_width + pb_inline,
        content_height + pb_block,
    );
    let mut frag = make_fragment(ctx, id, border_box, edges);
    frag.children = r.fragments;
    // The subtree was laid out at a provisional origin; move it onto the float.
    let dx = border_box.x + edges.border.left + edges.padding.left - content_cb_inner.x;
    let dy = border_box.y + edges.border.top + edges.padding.top - content_cb_inner.y;
    for c in &mut frag.children {
        translate(c, dx, dy);
    }
    frag
}

// ---------------------------------------------------------------------------
// Absolute positioning — §10.1, §10.3.7, §10.6.4
// ---------------------------------------------------------------------------

/// Every absolutely positioned box whose containing block is `root`: the
/// descendants reachable without passing through another positioned box.
///
/// This ancestor relationship is the whole of §10.1 point 3, and it is the step
/// the Python engine skips — `_layout_absolute` uses the immediate parent box,
/// so a tooltip inside an unpositioned wrapper inside a `position: relative`
/// card is placed against the wrapper.
pub fn collect_abspos(ctx: &LayoutContext, root: BoxId) -> Vec<BoxId> {
    let mut out = Vec::new();
    let mut stack: Vec<BoxId> = ctx.tree.children(root).to_vec();
    stack.reverse();
    while let Some(id) = stack.pop() {
        let style = ctx.tree.style(id);
        if style.position.is_out_of_flow() {
            out.push(id);
            continue;
        }
        if style.position.is_positioned() {
            continue;
        }
        let mut kids = ctx.tree.children(id).to_vec();
        kids.reverse();
        stack.extend(kids);
    }
    out
}

fn drain_abspos(
    _ctx: &mut LayoutContext,
    _id: BoxId,
    _frag: &Fragment,
    _statics: &mut Vec<(BoxId, f32, f32)>,
) {
}

fn take_abspos_fragments(
    ctx: &mut LayoutContext,
    id: BoxId,
    frag: &Fragment,
    statics: &Vec<(BoxId, f32, f32)>,
) -> Vec<Fragment> {
    let abs = collect_abspos(ctx, id);
    if abs.is_empty() {
        return Vec::new();
    }
    // §10.1: the containing block is the *padding* box of the positioned
    // ancestor, and its height is definite because the ancestor has just been
    // laid out.
    let pad = frag.padding_box();
    let cb = ContainingBlock::at(pad.x, pad.y, pad.width, Some(pad.height));
    let mut out = Vec::new();
    for a in abs {
        if ctx.tree.style(a).position == Position::Fixed {
            continue;
        }
        let sp = statics.iter().find(|(b, _, _)| *b == a).map(|(_, x, y)| (*x, *y));
        out.push(layout_absolute(ctx, a, &cb, sp));
    }
    out
}

/// CSS 2.1 §10.3.7 and §10.6.4: solve
/// `left + margin-left + border + padding + width + padding + border +
/// margin-right + right = containing block width`, and the same on the block
/// axis, with the `auto` rules that follow.
pub fn layout_absolute(
    ctx: &mut LayoutContext,
    id: BoxId,
    cb: &ContainingBlock,
    static_pos: Option<(f32, f32)>,
) -> Fragment {
    let style = ctx.tree.style(id).clone();
    let edges = resolve_edges(&style, cb);
    let pb_inline = edges.inline_padding_border();
    let pb_block = edges.block_padding_border();
    let cb_h = cb.height.unwrap_or(0.0);

    let left = style.inset.left.resolve(cb.width);
    let right = style.inset.right.resolve(cb.width);
    let top = style.inset.top.resolve(cb_h);
    let bottom = style.inset.bottom.resolve(cb_h);

    let specified_width = style
        .width
        .resolve(Some(cb.width))
        .map(|w| content_from_specified(w, &style, pb_inline));

    // --- inline axis -----------------------------------------------------
    let (x, content_width) = match (left, specified_width, right) {
        (Some(l), Some(w), _) => (cb.x + l + edges.margin.left, w),
        (Some(l), None, Some(r)) => {
            let w = (cb.width - l - r - edges.margin.inline_sum() - pb_inline).max(0.0);
            (cb.x + l + edges.margin.left, w)
        }
        (Some(l), None, None) => {
            let stf = intrinsic::outer_intrinsic(ctx, id, cb);
            let avail = (cb.width - l - edges.margin.inline_sum() - pb_inline).max(0.0);
            (cb.x + l + edges.margin.left, shrink_to_fit_width(stf.0, stf.1, avail, pb_inline))
        }
        (None, Some(w), Some(r)) => {
            (cb.x + cb.width - r - edges.margin.right - w - pb_inline, w)
        }
        (None, None, Some(r)) => {
            let stf = intrinsic::outer_intrinsic(ctx, id, cb);
            let avail = (cb.width - r - edges.margin.inline_sum() - pb_inline).max(0.0);
            let w = shrink_to_fit_width(stf.0, stf.1, avail, pb_inline);
            (cb.x + cb.width - r - edges.margin.right - w - pb_inline, w)
        }
        (None, w, None) => {
            // Both offsets auto: the box stays at its static position — where
            // it would have been in flow.
            let sx = static_pos.map(|p| p.0).unwrap_or(cb.x);
            let width = match w {
                Some(w) => w,
                None => {
                    let stf = intrinsic::outer_intrinsic(ctx, id, cb);
                    let avail = (cb.width - edges.margin.inline_sum() - pb_inline).max(0.0);
                    shrink_to_fit_width(stf.0, stf.1, avail, pb_inline)
                }
            };
            (sx + edges.margin.left, width)
        }
    };

    // --- block axis ------------------------------------------------------
    let definite_height = style.height.resolve(cb.height);
    let content_x = x + edges.border.left + edges.padding.left;

    // The subtree is laid out at a provisional origin and moved once the block
    // axis is solved, because §10.6.4's `height: auto` case needs the content
    // height before it can place the box.
    let mut own = BfcState::new(0.0, content_x, content_x + content_width);
    let inner_cb = ContainingBlock::at(content_x, 0.0, content_width, definite_height);
    let padding_cb = ContainingBlock::at(
        x + edges.border.left,
        0.0,
        content_width + edges.padding.inline_sum(),
        definite_height.map(|h| h + edges.padding.block_sum()),
    );
    let chain = ContainingBlockChain {
        flow: inner_cb,
        absolute: padding_cb,
        fixed: ctx.viewport,
    };
    let fc = ctx.tree.formatting_context(id);
    let mut statics = Vec::new();
    let r = layout_children(ctx, id, &inner_cb, &chain, &mut own, &mut statics, fc);
    let mut auto_h = r.content_height;
    let lowest = own.floats.lowest_bottom();
    if lowest.is_finite() {
        auto_h = auto_h.max(lowest);
    }
    let content_height =
        clamp_height(&style, definite_height.unwrap_or(auto_h), cb.height, pb_block);

    let y = match (top, bottom) {
        (Some(t), _) => cb.y + t + edges.margin.top,
        (None, Some(b)) => cb.y + cb_h - b - edges.margin.bottom - content_height - pb_block,
        (None, None) => static_pos.map(|p| p.1).unwrap_or(cb.y) + edges.margin.top,
    };

    let border_box = Rect::new(x, y, content_width + pb_inline, content_height + pb_block);
    let mut frag = make_fragment(ctx, id, border_box, edges);
    frag.children = r.fragments;
    frag.baseline = r.baseline.map(|b| b + edges.border.top + edges.padding.top);
    let dy = y + edges.border.top + edges.padding.top;
    for c in &mut frag.children {
        translate(c, 0.0, dy);
    }
    let extra = take_abspos_fragments(ctx, id, &frag, &statics);
    frag.children.extend(extra);
    frag
}

// ---------------------------------------------------------------------------
// Helpers shared with the inline formatting context
// ---------------------------------------------------------------------------

/// Lay out an atomic inline — `inline-block`, `inline-flex`, a replaced box —
/// as an independent formatting context, returning its fragment and the
/// baseline the surrounding line should align it on.
///
/// §10.8.1: "the baseline of an 'inline-block' is the baseline of its last line
/// box in the normal flow, unless it has either no in-flow line boxes or if its
/// 'overflow' property has a computed value other than 'visible', in which case
/// the baseline is the bottom margin edge."
pub fn layout_atomic_inline(
    ctx: &mut LayoutContext,
    id: BoxId,
    cb: &ContainingBlock,
    chain: &ContainingBlockChain,
    available: f32,
) -> (Fragment, f32) {
    let style = ctx.tree.style(id).clone();
    let node_kind = ctx.tree.get(id).kind.clone();
    let edges = resolve_edges(&style, cb);
    let pb_inline = edges.inline_padding_border();
    let pb_block = edges.block_padding_border();

    if let BoxKind::Replaced { width, height } = node_kind {
        let w = style
            .width
            .resolve(Some(cb.width))
            .map(|v| content_from_specified(v, &style, pb_inline))
            .unwrap_or(width);
        let h = style
            .height
            .resolve(cb.height)
            .map(|v| content_from_specified(v, &style, pb_block))
            .unwrap_or(height);
        let border_box = Rect::new(0.0, 0.0, w + pb_inline, h + pb_block);
        let frag = make_fragment(ctx, id, border_box, edges);
        let baseline = border_box.height + edges.margin.bottom;
        return (frag, baseline);
    }

    let stf = intrinsic::outer_intrinsic(ctx, id, cb);
    let mut used = solve_inline(&style, &edges, available.max(0.0), Some(stf));
    if let Some(c) = clamp_width(&style, used.content_width, cb.width, pb_inline) {
        used.content_width = c;
    }
    let content_width = used.content_width;
    let definite_height = style.height.resolve(cb.height);

    let mut own = BfcState::new(0.0, 0.0, content_width);
    let inner_cb = ContainingBlock::at(0.0, 0.0, content_width, definite_height);
    let padding_cb = ContainingBlock::at(
        0.0,
        0.0,
        content_width + edges.padding.inline_sum(),
        definite_height.map(|h| h + edges.padding.block_sum()),
    );
    let child_chain = chain.descend(&style, inner_cb, padding_cb);
    let fc = ctx.tree.formatting_context(id);
    let mut statics = Vec::new();
    let r = layout_children(ctx, id, &inner_cb, &child_chain, &mut own, &mut statics, fc);
    let mut auto_h = r.content_height;
    let lowest = own.floats.lowest_bottom();
    if lowest.is_finite() {
        auto_h = auto_h.max(lowest);
    }
    let content_height =
        clamp_height(&style, definite_height.unwrap_or(auto_h), cb.height, pb_block);

    let border_box = Rect::new(0.0, 0.0, content_width + pb_inline, content_height + pb_block);
    let mut frag = make_fragment(ctx, id, border_box, edges);
    frag.children = r.fragments;
    let dy = edges.border.top + edges.padding.top;
    for c in &mut frag.children {
        translate(c, edges.border.left + edges.padding.left, dy);
    }

    let overflow_visible =
        !style.overflow_x.establishes_bfc() && !style.overflow_y.establishes_bfc();
    let baseline = match r.baseline {
        Some(b) if overflow_visible => b + edges.border.top + edges.padding.top,
        // No in-flow line boxes, or clipped overflow: the bottom margin edge.
        _ => border_box.height + edges.margin.bottom,
    };
    frag.baseline = Some(baseline);
    (frag, baseline)
}

/// Build a style for an anonymous box that inherits from `parent`.
pub fn anonymous_style(parent: &ComputedStyle) -> Rc<ComputedStyle> {
    Rc::new(parent.inherited_only())
}

/// Convenience for tests and callers: a plain block style.
pub fn block_style() -> ComputedStyle {
    ComputedStyle { display: Display::Block, ..ComputedStyle::default() }
}

/// Used by the inline formatting context to know how much a floated box eats
/// out of a line at a given position.
pub fn line_band(bfc: &BfcState, y: f32, height: f32, left: f32, right: f32) -> (f32, f32) {
    bfc.floats.band(y, height, left.max(bfc.left_edge), right.min(bfc.right_edge))
}

/// Never called; keeps the `Margin`/`Sides` imports honest for readers who
/// grep for where auto margins are handled.
#[allow(unused)]
fn _doc_only(m: Margin, s: Sides<f32>) -> bool {
    m.is_auto() && s.inline_sum() == 0.0
}
