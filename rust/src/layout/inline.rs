//! The inline formatting context.
//!
//! Two things happen here that the Python engine does not do at all.
//!
//! **Leading (§10.8.1).** A line box's height is not a multiple of the font
//! size. Each inline box has a content area of `ascent + descent`, and its
//! `line-height` determines a *leading* of `line-height − (ascent + descent)`,
//! half of which sits above the content area and half below. The line box is
//! then the smallest box containing every inline box's leaded extent, measured
//! from the shared baseline. `layout.py` reads `line-height` exactly once in
//! 3691 lines — in the hit-testing path — and hardcodes `1.25 * ascent` in the
//! two places that decide line positions, so `line-height: 1` and
//! `line-height: 3` produce byte-identical layout.
//!
//! **White space processing (CSS Text §4.1.1).** A run of white space collapses
//! to a single space, *and whether one existed at each boundary is preserved*.
//! `layout.py` does the opposite at both ends: `content.split()` throws away
//! every boundary space, and `_place_word` then adds a space advance after
//! every word unconditionally. The result is that a space appears where the
//! markup had none — `A web browser , often` — and is missing where it did.

use std::rc::Rc;

use super::block;
use super::geom::{BoxEdges, Rect};
use super::style::{
    ComputedStyle, LengthPercentage, Sides, TextAlign, VerticalAlign, WhiteSpace,
};
use super::text::{FontKey, InlineMetrics};
use super::{
    resolve_edges, BoxId, BoxKind, ContainingBlock, ContainingBlockChain, FcResult, Fragment,
    FragmentKind, LayoutContext,
};

// ---------------------------------------------------------------------------
// Inline items
// ---------------------------------------------------------------------------

#[derive(Debug, Clone)]
pub enum InlineItem {
    /// Entering a non-replaced inline box: its start-side margin, border and
    /// padding are added to the line here.
    Open(BoxId),
    Close(BoxId),
    /// A run of text, already white-space processed. Spaces inside it are real
    /// and measured; there are no invented ones.
    Text { owner: BoxId, text: String },
    /// An `inline-block`, `inline-flex`, `inline-grid` or replaced box: laid
    /// out in its own formatting context and placed on the line as one opaque
    /// rectangle.
    Atomic(BoxId),
    /// `<br>`.
    Break,
    /// A floated box encountered in inline content. It is not *on* the line —
    /// it is placed in the block formatting context and shortens the lines it
    /// overlaps (§9.5). Keeping it in the item stream is how a block container
    /// whose only child is a float still lays that float out.
    Float(BoxId),
}

/// Walk a block container's inline content, applying the white-space model.
///
/// `last_was_space` is threaded across item boundaries so that
/// `<b>web browser</b>, often` measures no space at the `</b>` boundary and
/// `A <b>web</b>` measures exactly one.
pub fn collect_inline_items(ctx: &LayoutContext, container: BoxId) -> Vec<InlineItem> {
    let mut items = Vec::new();
    let mut last_was_space = true; // start of the IFC: leading space is dropped
    collect_into(ctx, container, &mut items, &mut last_was_space);
    items
}

fn collect_into(
    ctx: &LayoutContext,
    id: BoxId,
    items: &mut Vec<InlineItem>,
    last_was_space: &mut bool,
) {
    for &child in ctx.tree.children(id) {
        let node = ctx.tree.get(child);
        if node.is_out_of_flow() {
            continue;
        }
        if node.is_floated() {
            items.push(InlineItem::Float(child));
            continue;
        }
        match &node.kind {
            BoxKind::Text(raw) => {
                let ws = node.style.white_space;
                let processed = process_whitespace(raw, ws, last_was_space);
                if !processed.is_empty() {
                    items.push(InlineItem::Text { owner: child, text: processed });
                }
            }
            BoxKind::Replaced { .. } => {
                items.push(InlineItem::Atomic(child));
                *last_was_space = false;
            }
            _ => {
                if node.style.display.is_atomic_inline() {
                    items.push(InlineItem::Atomic(child));
                    *last_was_space = false;
                } else if node.label == "br" {
                    items.push(InlineItem::Break);
                    *last_was_space = true;
                } else {
                    items.push(InlineItem::Open(child));
                    collect_into(ctx, child, items, last_was_space);
                    items.push(InlineItem::Close(child));
                }
            }
        }
    }
}

/// CSS Text §4.1.1 phase I: collapse runs of white space to a single space,
/// keeping the information that one was there.
///
/// The `last_was_space` flag carries the collapse across the boundary between
/// two text nodes, which is what stops `A </b><i>b` from producing two spaces
/// and what stops `</b>,` from producing one.
pub fn process_whitespace(raw: &str, ws: WhiteSpace, last_was_space: &mut bool) -> String {
    if ws.preserves_spaces() {
        *last_was_space = raw.chars().last().is_some_and(|c| c == ' ' || c == '\t');
        return raw.to_string();
    }
    let mut out = String::with_capacity(raw.len());
    for c in raw.chars() {
        let is_space = c == ' ' || c == '\t' || c == '\n' || c == '\r' || c == '\u{0c}';
        if is_space {
            if ws.preserves_newlines() && c == '\n' {
                out.push('\n');
                *last_was_space = true;
                continue;
            }
            if !*last_was_space {
                out.push(' ');
                *last_was_space = true;
            }
        } else {
            out.push(c);
            *last_was_space = false;
        }
    }
    out
}

/// Split processed text into line-breaking units: runs of non-space characters
/// and the single spaces between them. Keeping the spaces as their own tokens
/// is what lets a line drop its trailing space without the word before it
/// changing width.
fn tokenize(text: &str) -> Vec<&str> {
    let mut out = Vec::new();
    let bytes = text.as_bytes();
    let mut i = 0;
    while i < bytes.len() {
        let start = i;
        if bytes[i] == b' ' {
            while i < bytes.len() && bytes[i] == b' ' {
                i += 1;
            }
        } else {
            while i < bytes.len() && bytes[i] != b' ' {
                i += 1;
            }
        }
        out.push(&text[start..i]);
    }
    out
}

// ---------------------------------------------------------------------------
// Line boxes
// ---------------------------------------------------------------------------

/// One thing placed on a line, with the vertical extent it demands above and
/// below the shared baseline.
#[derive(Debug, Clone)]
struct Placed {
    kind: PlacedKind,
    x: f32,
    width: f32,
    above: f32,
    below: f32,
    /// `vertical-align: top` / `bottom` cannot be resolved until the line's
    /// height is known, so they are placed in a second pass.
    align_to_line: Option<bool>,
    height: f32,
}

#[derive(Debug, Clone)]
enum PlacedKind {
    Text { owner: BoxId, text: String },
    Atomic { id: BoxId, frag: Box<Fragment>, baseline: f32 },
}

struct LineBuilder<'a> {
    ctx_width: f32,
    items: Vec<Placed>,
    /// Inline boxes currently open, with the x each started at on this line.
    open: Vec<(BoxId, f32)>,
    strut: InlineMetrics,
    style: Rc<ComputedStyle>,
    _marker: std::marker::PhantomData<&'a ()>,
}

/// Lay out the inline content of a block container.
// `flush_line!` resets the line cursor at the end of its body, so the final
// expansion always leaves those stores dead. Restructuring to avoid it would
// mean duplicating the flush at every call site.
#[allow(unused_assignments)]
pub fn layout_inline_context(
    ctx: &mut LayoutContext,
    container: BoxId,
    content_cb: &ContainingBlock,
    chain: &ContainingBlockChain,
    bfc: &mut block::BfcState,
    statics: &mut Vec<(BoxId, f32, f32)>,
) -> FcResult {
    let style = ctx.tree.style(container).clone();
    let items = collect_inline_items(ctx, container);
    let fonts = ctx.fonts;

    // §10.8.1: "each line box starts with a zero-width inline box with the
    // element's font and line height properties". The strut is why an empty
    // `<p>` is still one line tall and why a line containing only a small
    // `<sup>` does not shrink to the height of the superscript.
    let strut = InlineMetrics::for_style(&style, fonts);

    let mut lines: Vec<Fragment> = Vec::new();
    let mut float_frags: Vec<Fragment> = Vec::new();
    let mut y = content_cb.y;
    let mut cur: Vec<Placed> = Vec::new();
    let mut open: Vec<(BoxId, f32)> = Vec::new();
    let mut open_frags: Vec<(BoxId, f32, BoxEdges)> = Vec::new();
    let mut x = 0.0f32;
    let mut first_line = true;
    let mut at_line_start = true;
    let mut last_baseline: Option<f32> = None;

    // The band available to a line, once floats in this BFC have taken their
    // share. A line box is shortened by floats even though the block box that
    // contains it is not (§9.5).
    let band = |bfc: &block::BfcState, y: f32, h: f32| -> (f32, f32) {
        let (l, r) = bfc.floats.band(y, h, content_cb.x, content_cb.x + content_cb.width);
        (l - content_cb.x, r - content_cb.x)
    };

    let indent = style.text_indent.resolve(content_cb.width);
    let (mut band_l, mut band_r) = band(bfc, y, strut.height());
    if first_line {
        x = indent;
    }
    let _ = &mut open_frags;

    macro_rules! flush_line {
        ($forced:expr) => {{
            // Trailing spaces at the end of a line are not rendered.
            while let Some(last) = cur.last() {
                if let PlacedKind::Text { text, .. } = &last.kind {
                    if text.trim_end().is_empty() {
                        cur.pop();
                        continue;
                    }
                }
                break;
            }
            let line_width: f32 = cur.iter().map(|p| p.width).sum::<f32>();
            let (above, below) = line_extent(&cur, &strut);
            let avail = band_r - band_l;
            let start_offset = match style.text_align {
                TextAlign::Left | TextAlign::Justify => 0.0,
                // §16.2: the line is centred in the *content* box. Measuring
                // against the border box instead — as `layout.py` does — puts
                // every centred line in a padded box off by padding/2.
                TextAlign::Center => ((avail - line_width) / 2.0).max(0.0),
                TextAlign::Right => (avail - line_width).max(0.0),
            };
            let baseline_y = y + above;
            let mut line = Fragment::new(FragmentKind::Line, "#line".to_string());
            line.border_box =
                Rect::new(content_cb.x + band_l, y, avail.max(line_width), above + below);
            line.baseline = Some(above);
            for p in cur.drain(..) {
                let px = content_cb.x + band_l + start_offset + p.x;
                match p.kind {
                    PlacedKind::Text { owner, text } => {
                        let mut f = Fragment::new(FragmentKind::Text(text), "#text".to_string());
                        f.box_id = Some(owner);
                        f.dom = ctx.tree.get(owner).dom;
                        f.border_box = Rect::new(px, baseline_y - p.above, p.width, p.height);
                        f.baseline = Some(p.above);
                        line.children.push(f);
                    }
                    PlacedKind::Atomic { id, mut frag, baseline } => {
                        let top = baseline_y - p.above;
                        let (dx, dy) = (px - frag.border_box.x, top - frag.border_box.y);
                        block::translate(&mut frag, dx, dy);
                        let _ = (id, baseline);
                        line.children.push(*frag);
                    }
                }
            }
            last_baseline = Some(baseline_y - content_cb.y);
            y += above + below;
            lines.push(line);
            first_line = false;
            at_line_start = true;
            let _: bool = $forced;
            let (l, r) = band(bfc, y, strut.height());
            band_l = l;
            band_r = r;
            x = 0.0;
        }};
    }

    for item in &items {
        match item {
            InlineItem::Open(id) => {
                let st = ctx.tree.style(*id);
                let e = resolve_edges(st, content_cb);
                x += e.margin.left + e.border.left + e.padding.left;
                open.push((*id, x));
            }
            InlineItem::Close(id) => {
                let st = ctx.tree.style(*id);
                let e = resolve_edges(st, content_cb);
                x += e.margin.right + e.border.right + e.padding.right;
                open.retain(|(b, _)| b != id);
            }
            InlineItem::Break => {
                flush_line!(true);
            }
            InlineItem::Float(id) => {
                // §9.5: the float's top is the top of the current line box. It
                // does not join the line; it narrows this and later lines.
                let f = block::layout_float(ctx, *id, content_cb, chain, bfc, statics);
                float_frags.push(f);
                let (l, r) = band(bfc, y, strut.height());
                band_l = l;
                band_r = r;
                if at_line_start {
                    x = if first_line { indent } else { 0.0 };
                }
            }
            InlineItem::Text { owner, text } => {
                let st = ctx.tree.style(*owner).clone();
                let key = FontKey::of(&st);
                let m = InlineMetrics::for_style(&st, fonts);
                let metrics = fonts.metrics(&key);
                for tok in tokenize(text) {
                    let w = fonts.advance(&key, tok);
                    let is_space = tok.starts_with(' ');
                    if is_space && at_line_start {
                        continue;
                    }
                    if !is_space
                        && st.white_space.allows_wrap()
                        && !at_line_start
                        && x + w > band_r - band_l + 0.01
                    {
                        flush_line!(false);
                    }
                    if is_space && at_line_start {
                        continue;
                    }
                    let (above, below) = align_text(&st, &m, &metrics);
                    cur.push(Placed {
                        kind: PlacedKind::Text { owner: *owner, text: tok.to_string() },
                        x,
                        width: w,
                        above,
                        below,
                        align_to_line: None,
                        height: above + below,
                    });
                    x += w;
                    at_line_start = false;
                }
            }
            InlineItem::Atomic(id) => {
                let avail = band_r - band_l - x;
                let (frag, baseline) =
                    block::layout_atomic_inline(ctx, *id, content_cb, chain, avail.max(0.0));
                let st = ctx.tree.style(*id).clone();
                let e = resolve_edges(&st, content_cb);
                let outer_w = frag.border_box.width + e.margin.inline_sum();
                let outer_h = frag.border_box.height + e.margin.block_sum();
                if !at_line_start && x + outer_w > band_r - band_l + 0.01 {
                    flush_line!(false);
                }
                // The baseline of an atomic inline is measured from its border
                // box top; the margin sits above that.
                let b = baseline + e.margin.top;
                let (above, below) = align_atomic(&st, &style, b, outer_h, fonts);
                cur.push(Placed {
                    kind: PlacedKind::Atomic {
                        id: *id,
                        frag: Box::new(frag),
                        baseline: b,
                    },
                    x: x + e.margin.left,
                    width: outer_w,
                    above,
                    below,
                    align_to_line: None,
                    height: outer_h,
                });
                x += outer_w;
                at_line_start = false;
            }
        }
    }
    if !cur.is_empty() {
        flush_line!(false);
    }

    lines.extend(float_frags);
    FcResult {
        content_height: (y - content_cb.y).max(0.0),
        baseline: last_baseline,
        fragments: lines,
    }
}

/// The extent of a line box above and below its baseline: the maximum demanded
/// by any inline box on it, including the strut.
fn line_extent(items: &[Placed], strut: &InlineMetrics) -> (f32, f32) {
    let mut above = strut.above_baseline;
    let mut below = strut.below_baseline;
    for p in items {
        above = above.max(p.above);
        below = below.max(p.below);
    }
    (above, below)
}

/// Where a text run sits relative to the baseline, after `vertical-align`.
fn align_text(
    style: &ComputedStyle,
    m: &InlineMetrics,
    metrics: &super::text::FontMetrics,
) -> (f32, f32) {
    let shift = vertical_shift(style, m.height(), m.above_baseline, metrics, style.font_size);
    (m.above_baseline + shift, m.below_baseline - shift)
}

/// Where an atomic inline sits. Its "baseline" is `baseline` below its own top
/// margin edge, and its height is the margin box height.
fn align_atomic(
    style: &ComputedStyle,
    parent: &ComputedStyle,
    baseline: f32,
    height: f32,
    fonts: &dyn super::text::FontSource,
) -> (f32, f32) {
    let pm = fonts.metrics(&FontKey::of(parent));
    match style.vertical_align {
        VerticalAlign::Top | VerticalAlign::Bottom => (baseline, height - baseline),
        VerticalAlign::Middle => {
            // §10.8.1: align the vertical midpoint of the box with the baseline
            // plus half the parent's x-height.
            let x_height = pm.ascent * 0.5;
            let above = height / 2.0 + x_height / 2.0;
            (above, height - above)
        }
        VerticalAlign::TextTop => (pm.ascent, height - pm.ascent),
        VerticalAlign::TextBottom => (height - pm.descent, pm.descent),
        _ => {
            let shift = vertical_shift(style, height, baseline, &pm, parent.font_size);
            (baseline + shift, height - baseline - shift)
        }
    }
}

/// The upward shift `vertical-align` asks for, in pixels.
fn vertical_shift(
    style: &ComputedStyle,
    height: f32,
    _above: f32,
    parent_metrics: &super::text::FontMetrics,
    parent_font_size: f32,
) -> f32 {
    match style.vertical_align {
        VerticalAlign::Baseline => 0.0,
        // CSS leaves the sub/super offsets to the UA; these are the
        // conventional fractions of the parent's font size.
        VerticalAlign::Sub => -parent_font_size * 0.2,
        VerticalAlign::Super => parent_font_size * 0.33,
        VerticalAlign::Middle => {
            let x_height = parent_metrics.ascent * 0.5;
            x_height / 2.0 + height / 2.0 - height
        }
        VerticalAlign::TextTop | VerticalAlign::TextBottom => 0.0,
        VerticalAlign::Top | VerticalAlign::Bottom => 0.0,
        // A percentage resolves against the box's own `line-height` (§10.8.1).
        VerticalAlign::Length(LengthPercentage::Px(v)) => v,
        VerticalAlign::Length(LengthPercentage::Percent(p)) => height * p / 100.0,
    }
}

/// The intrinsic inline sizes of an inline formatting context.
///
/// `max-content` is the width the content would take with no wrapping at all;
/// `min-content` is the widest thing that cannot be broken. Both include the
/// padding, border and margin of every inline box and atomic inline on the way
/// — which is the part `_measure_width` omits, and the reason every flex row
/// with padded items comes out too narrow.
pub fn inline_intrinsic(ctx: &mut LayoutContext, container: BoxId, cb: &ContainingBlock) -> (f32, f32) {
    let items = collect_inline_items(ctx, container);
    let fonts = ctx.fonts;
    let mut max_total = 0.0f32;
    let mut max_run = 0.0f32; // widest run of max-content between forced breaks
    let mut min = 0.0f32;
    let mut cur_word = 0.0f32;
    // Padding and border of an open inline box are unbreakable: they belong to
    // whichever line the box's first and last content lands on.
    let mut pending_open = 0.0f32;

    for item in &items {
        match item {
            InlineItem::Open(id) => {
                let e = resolve_edges(ctx.tree.style(*id), cb);
                let w = e.margin.left + e.border.left + e.padding.left;
                max_total += w;
                pending_open += w;
            }
            InlineItem::Close(id) => {
                let e = resolve_edges(ctx.tree.style(*id), cb);
                let w = e.margin.right + e.border.right + e.padding.right;
                max_total += w;
                cur_word += w;
                min = min.max(cur_word);
            }
            // A float is out of the inline flow: it contributes nothing to the
            // line's own intrinsic size. (CSS Sizing takes floats into a
            // container's intrinsic size at the block level, not here.)
            InlineItem::Float(_) => {}
            InlineItem::Break => {
                max_run = max_run.max(max_total);
                max_total = 0.0;
                min = min.max(cur_word);
                cur_word = 0.0;
                pending_open = 0.0;
            }
            InlineItem::Text { owner, text } => {
                let st = ctx.tree.style(*owner);
                let key = FontKey::of(st);
                for tok in tokenize(text) {
                    let w = fonts.advance(&key, tok);
                    max_total += w;
                    if tok.starts_with(' ') {
                        min = min.max(cur_word);
                        cur_word = 0.0;
                        pending_open = 0.0;
                    } else {
                        cur_word += w + std::mem::take(&mut pending_open);
                    }
                }
                min = min.max(cur_word);
            }
            InlineItem::Atomic(id) => {
                let (a_min, a_max) = outer_intrinsic(ctx, *id, cb);
                let e = resolve_edges(ctx.tree.style(*id), cb);
                let m = e.margin.inline_sum();
                max_total += a_max + m;
                // An atomic inline is unbreakable, so it joins the current run.
                cur_word += a_min + m + std::mem::take(&mut pending_open);
                min = min.max(cur_word);
            }
        }
    }
    max_run = max_run.max(max_total);
    (min, max_run)
}

use super::intrinsic::outer_intrinsic;

/// Kept so callers can build a zero-size edge set without importing `Sides`.
pub fn no_edges() -> BoxEdges {
    BoxEdges { margin: Sides::ZERO, border: Sides::ZERO, padding: Sides::ZERO }
}
