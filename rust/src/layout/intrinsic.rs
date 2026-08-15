//! Intrinsic inline sizes: min-content and max-content.
//!
//! CSS Sizing §3. Two numbers per box:
//!
//! * **max-content** — the width the box would take if nothing ever wrapped.
//! * **min-content** — the width of the widest thing in it that *cannot* be
//!   broken: the longest word, the widest atomic inline, the widest child.
//!
//! Both are *outer* sizes: they include the box's own padding and border, and a
//! child's contribution to its parent includes the child's margins as well. The
//! Python engine's `_measure_width` sums text advances, image widths and form
//! control widths and adds nothing else — no descendant `width`, no padding, no
//! border, no margin, with a single font resolved once at the top. Seven call
//! sites consume it: float shrink-to-fit, inline-block sizing, flex base size,
//! flex's second pass, grid `auto` tracks, and table auto columns. All seven
//! get a number that is short by the sum of the box model, which is why
//! `20-flex-center-natural.html` produces cells 27.1px wide — exactly the text
//! advance — where Chrome makes them 73.1px, being 23.1px of text plus 40px of
//! padding plus 10px of border.
//!
//! Percentages contribute nothing here, per CSS Sizing §4.1: a percentage that
//! resolves against a size which is itself being computed is treated as `auto`.
//! That is why the edges below are resolved against a zero-width containing
//! block rather than against the real one.

use super::inline;
use super::style::{BoxSizing, LengthPercentage, Size};
use super::text::FontKey;
use super::{resolve_edges, BoxId, BoxKind, ContainingBlock, FormattingContext, LayoutContext};

/// A containing block that resolves every percentage to zero — the base CSS
/// Sizing prescribes while intrinsic sizes are being computed.
fn indefinite() -> ContainingBlock {
    ContainingBlock::new(0.0, None)
}

/// `(min-content, max-content)` of a box's *border box*.
pub fn outer_intrinsic(ctx: &mut LayoutContext, id: BoxId, cb: &ContainingBlock) -> (f32, f32) {
    let style = ctx.tree.style(id).clone();
    let edges = resolve_edges(&style, &indefinite());
    let pb = edges.inline_padding_border();

    let to_content = |v: f32| match style.box_sizing {
        BoxSizing::ContentBox => v,
        BoxSizing::BorderBox => (v - pb).max(0.0),
    };

    // A definite width settles both numbers outright: a box 200px wide has a
    // min-content and a max-content contribution of 200px whatever is inside
    // it.
    if let Size::LengthPercentage(LengthPercentage::Px(w)) = style.width {
        let c = to_content(w).max(0.0);
        return (c + pb, c + pb);
    }

    let (mut min, mut max) = content_intrinsic(ctx, id, cb);

    if let Size::LengthPercentage(LengthPercentage::Px(w)) = style.max_width {
        let c = to_content(w).max(0.0);
        min = min.min(c);
        max = max.min(c);
    }
    if let Size::LengthPercentage(LengthPercentage::Px(w)) = style.min_width {
        let c = to_content(w).max(0.0);
        min = min.max(c);
        max = max.max(c);
    }
    (min + pb, max + pb)
}

/// `(min-content, max-content)` of a box's *content box*.
pub fn content_intrinsic(ctx: &mut LayoutContext, id: BoxId, cb: &ContainingBlock) -> (f32, f32) {
    let node = ctx.tree.get(id);
    match &node.kind {
        BoxKind::Replaced { width, .. } => {
            let w = *width;
            (w, w)
        }
        BoxKind::Text(text) => {
            let text = text.clone();
            let key = FontKey::of(&node.style);
            let fonts = ctx.fonts;
            let mut min = 0.0f32;
            for word in text.split_whitespace() {
                min = min.max(fonts.advance(&key, word));
            }
            (min, fonts.advance(&key, text.trim()))
        }
        _ => match ctx.tree.formatting_context(id) {
            FormattingContext::Inline => inline::inline_intrinsic(ctx, id, cb),
            // Flex and grid have their own intrinsic rules; until they exist,
            // treating them as block containers gives the max of the children
            // rather than the sum, which is an under-estimate for a flex row.
            // It is stated here rather than hidden so the flex work knows what
            // it is replacing.
            _ => block_intrinsic(ctx, id, cb),
        },
    }
}

/// A block container's intrinsic size is the widest of its children's outer
/// contributions — they stack, so they do not add up.
fn block_intrinsic(ctx: &mut LayoutContext, id: BoxId, cb: &ContainingBlock) -> (f32, f32) {
    let children = ctx.tree.children(id).to_vec();
    let mut min = 0.0f32;
    let mut max = 0.0f32;
    for child in children {
        if ctx.tree.get(child).is_out_of_flow() {
            continue;
        }
        let (cmin, cmax) = outer_intrinsic(ctx, child, cb);
        let e = resolve_edges(ctx.tree.style(child), &indefinite());
        let m = e.margin.inline_sum();
        min = min.max(cmin + m);
        max = max.max(cmax + m);
    }
    (min, max)
}
