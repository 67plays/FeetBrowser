//! Typed computed style.
//!
//! The Python engine reads CSS out of a `dict[str, str]` at the point of use,
//! which is why the same property is parsed differently in six places and why
//! `em` is hardcoded to 16px in `parse_px` — the function has no element to ask
//! for a font size. Here the string-to-value step happens exactly once, at
//! style-computation time, when the element's font size and the viewport are
//! both in hand.
//!
//! The split between what is resolved here and what is left for layout follows
//! CSS 2.1's own division. Absolute units (`px`, `pt`, `em`, `rem`, `vh`, ...)
//! have a computed value in pixels, so they are converted now. Percentages do
//! *not* — their computed value is still a percentage, because the box they
//! resolve against is not known until layout picks a containing block. Keeping
//! `Percent` alive in the computed style is what makes §10.5 expressible: a
//! percentage height against an auto-height containing block has to *become*
//! `auto`, and you cannot notice that if percentages were flattened to numbers
//! by the parser.

use std::collections::HashMap;

/// A computed length: either an absolute number of pixels or a percentage that
/// still needs a containing block.
#[derive(Debug, Clone, Copy, PartialEq)]
pub enum LengthPercentage {
    Px(f32),
    Percent(f32),
}

impl LengthPercentage {
    pub const ZERO: LengthPercentage = LengthPercentage::Px(0.0);

    /// Resolve against a definite base. Used for padding and margins, whose
    /// percentage base is always the containing block's *width* (§8.3, §8.4) —
    /// including on the block axis, which is the rule everyone finds surprising
    /// and which makes `padding-top: 56.25%` an aspect-ratio box.
    pub fn resolve(self, base: f32) -> f32 {
        match self {
            LengthPercentage::Px(v) => v,
            LengthPercentage::Percent(p) => p / 100.0 * base,
        }
    }

    /// Resolve against a base that may be indefinite. `None` means the
    /// percentage cannot be resolved and the caller must fall back to `auto`.
    pub fn resolve_maybe(self, base: Option<f32>) -> Option<f32> {
        match self {
            LengthPercentage::Px(v) => Some(v),
            LengthPercentage::Percent(p) => base.map(|b| p / 100.0 * b),
        }
    }

    pub fn is_zero(self) -> bool {
        matches!(self, LengthPercentage::Px(v) if v == 0.0)
            || matches!(self, LengthPercentage::Percent(p) if p == 0.0)
    }
}

/// `width` / `height` / `min-*` / `max-*`.
#[derive(Debug, Clone, Copy, PartialEq)]
pub enum Size {
    Auto,
    LengthPercentage(LengthPercentage),
    MinContent,
    MaxContent,
    FitContent,
    /// `max-width: none` / `max-height: none`.
    None,
}

impl Size {
    /// Resolve to a definite pixel value, or `None` if the size behaves as
    /// `auto`. §10.5: "if the height of the containing block is not specified
    /// explicitly ... and this element is not absolutely positioned, the value
    /// computes to 'auto'". `base = None` is exactly that case.
    pub fn resolve(self, base: Option<f32>) -> Option<f32> {
        match self {
            Size::LengthPercentage(lp) => lp.resolve_maybe(base),
            _ => None,
        }
    }

    pub fn is_auto(self) -> bool {
        matches!(self, Size::Auto)
    }
}

/// A margin, which unlike padding may be `auto` (§10.3.3 centring) and may be
/// negative.
#[derive(Debug, Clone, Copy, PartialEq)]
pub enum Margin {
    Auto,
    LengthPercentage(LengthPercentage),
}

impl Margin {
    pub const ZERO: Margin = Margin::LengthPercentage(LengthPercentage::Px(0.0));

    pub fn is_auto(self) -> bool {
        matches!(self, Margin::Auto)
    }

    /// `auto` resolves to 0 on the block axis (§10.6.3) and when there is no
    /// free space to distribute.
    pub fn resolve_or_zero(self, base: f32) -> f32 {
        match self {
            Margin::Auto => 0.0,
            Margin::LengthPercentage(lp) => lp.resolve(base),
        }
    }
}

/// `top` / `right` / `bottom` / `left` on a positioned box.
#[derive(Debug, Clone, Copy, PartialEq)]
pub enum Inset {
    Auto,
    LengthPercentage(LengthPercentage),
}

impl Inset {
    pub fn is_auto(self) -> bool {
        matches!(self, Inset::Auto)
    }
    pub fn resolve(self, base: f32) -> Option<f32> {
        match self {
            Inset::Auto => None,
            Inset::LengthPercentage(lp) => Some(lp.resolve(base)),
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Display {
    None,
    Block,
    Inline,
    InlineBlock,
    Flex,
    InlineFlex,
    Grid,
    InlineGrid,
    ListItem,
    Table,
    InlineTable,
    TableRow,
    TableCell,
    TableRowGroup,
}

impl Display {
    /// Does a box with this display participate in an inline formatting
    /// context? `inline-flex` and `inline-grid` do — the Python engine's
    /// `_is_inline_level` tests only `("inline", "inline-block")`, which is why
    /// a Wikipedia language button becomes a full-width block.
    pub fn is_inline_level(self) -> bool {
        matches!(
            self,
            Display::Inline
                | Display::InlineBlock
                | Display::InlineFlex
                | Display::InlineGrid
                | Display::InlineTable
        )
    }

    /// An atomic inline is inline-level on the outside but lays its contents
    /// out in an independent formatting context: it never breaks across lines
    /// and it contributes to the line as a single opaque rectangle.
    pub fn is_atomic_inline(self) -> bool {
        matches!(
            self,
            Display::InlineBlock | Display::InlineFlex | Display::InlineGrid | Display::InlineTable
        )
    }

    /// The inner display type: what formatting context this box establishes for
    /// its children, if it establishes an independent one at all.
    pub fn inner(self) -> InnerDisplay {
        match self {
            Display::Flex | Display::InlineFlex => InnerDisplay::Flex,
            Display::Grid | Display::InlineGrid => InnerDisplay::Grid,
            Display::Table | Display::InlineTable => InnerDisplay::Table,
            _ => InnerDisplay::Flow,
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum InnerDisplay {
    /// Block-and-inline layout: the box holds either a block formatting
    /// context or an inline one, depending on its children.
    Flow,
    Flex,
    Grid,
    Table,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Position {
    Static,
    Relative,
    Absolute,
    Fixed,
    Sticky,
}

impl Position {
    /// A "positioned" box, in the §10.1 sense that makes it a containing block
    /// for absolutely positioned descendants.
    pub fn is_positioned(self) -> bool {
        !matches!(self, Position::Static)
    }
    /// Out of flow: skipped entirely by the block and inline formatting
    /// contexts, laid out against its own containing block afterwards.
    pub fn is_out_of_flow(self) -> bool {
        matches!(self, Position::Absolute | Position::Fixed)
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Float {
    None,
    Left,
    Right,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Clear {
    None,
    Left,
    Right,
    Both,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum BoxSizing {
    ContentBox,
    BorderBox,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Overflow {
    Visible,
    Hidden,
    Scroll,
    Auto,
    Clip,
}

impl Overflow {
    /// Anything other than `visible` establishes a block formatting context
    /// (CSS 2.1 §9.4.1). This is the rule that makes `overflow: hidden` the
    /// canonical way to make a parent contain its floats.
    pub fn establishes_bfc(self) -> bool {
        !matches!(self, Overflow::Visible)
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum TextAlign {
    Left,
    Right,
    Center,
    Justify,
}

#[derive(Debug, Clone, Copy, PartialEq)]
pub enum LineHeight {
    /// The UA's own leading. CSS says "normal" is a "reasonable" value the UA
    /// picks; browsers use the font's own line spacing, which for the common
    /// web faces lands near 1.15–1.2em, not the 1.33em the Python engine's
    /// hardcoded 1.25 multiplier produces.
    Normal,
    /// A bare number, which inherits as a *number* and so re-multiplies against
    /// each descendant's own font size.
    Number(f32),
    Px(f32),
}

impl LineHeight {
    /// The used line-height for a box with this font size.
    pub fn resolve(self, font_size: f32, normal_ratio: f32) -> f32 {
        match self {
            LineHeight::Normal => font_size * normal_ratio,
            LineHeight::Number(n) => font_size * n,
            LineHeight::Px(v) => v,
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq)]
pub enum VerticalAlign {
    Baseline,
    Sub,
    Super,
    TextTop,
    TextBottom,
    Middle,
    Top,
    Bottom,
    /// A length or percentage raises the box above its baseline; the
    /// percentage base is the box's own `line-height` (§10.8.1).
    Length(LengthPercentage),
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum WhiteSpace {
    Normal,
    Pre,
    NoWrap,
    PreWrap,
    PreLine,
}

impl WhiteSpace {
    pub fn preserves_newlines(self) -> bool {
        matches!(self, WhiteSpace::Pre | WhiteSpace::PreWrap | WhiteSpace::PreLine)
    }
    pub fn preserves_spaces(self) -> bool {
        matches!(self, WhiteSpace::Pre | WhiteSpace::PreWrap)
    }
    pub fn allows_wrap(self) -> bool {
        matches!(self, WhiteSpace::Normal | WhiteSpace::PreWrap | WhiteSpace::PreLine)
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum FontStyle {
    Normal,
    Italic,
}

/// Per-side quantities, in CSS's own clockwise order.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct Sides<T> {
    pub top: T,
    pub right: T,
    pub bottom: T,
    pub left: T,
}

impl<T: Copy> Sides<T> {
    pub fn all(v: T) -> Self {
        Sides { top: v, right: v, bottom: v, left: v }
    }
}

impl Sides<f32> {
    pub const ZERO: Sides<f32> = Sides { top: 0.0, right: 0.0, bottom: 0.0, left: 0.0 };
    pub fn inline_sum(&self) -> f32 {
        self.left + self.right
    }
    pub fn block_sum(&self) -> f32 {
        self.top + self.bottom
    }
}

impl std::ops::Add for Sides<f32> {
    type Output = Sides<f32>;
    fn add(self, o: Sides<f32>) -> Sides<f32> {
        Sides {
            top: self.top + o.top,
            right: self.right + o.right,
            bottom: self.bottom + o.bottom,
            left: self.left + o.left,
        }
    }
}

/// The computed style of one element.
#[derive(Debug, Clone, PartialEq)]
pub struct ComputedStyle {
    pub display: Display,
    pub position: Position,
    pub float: Float,
    pub clear: Clear,
    pub box_sizing: BoxSizing,
    pub overflow_x: Overflow,
    pub overflow_y: Overflow,

    pub width: Size,
    pub height: Size,
    pub min_width: Size,
    pub max_width: Size,
    pub min_height: Size,
    pub max_height: Size,

    pub margin: Sides<Margin>,
    pub padding: Sides<LengthPercentage>,
    pub border: Sides<f32>,
    pub inset: Sides<Inset>,

    pub font_size: f32,
    pub font_style: FontStyle,
    pub font_weight: u16,
    pub font_family: String,
    pub line_height: LineHeight,
    pub vertical_align: VerticalAlign,
    pub text_align: TextAlign,
    pub text_indent: LengthPercentage,
    pub white_space: WhiteSpace,

    /// Flex container properties, kept here so a flex formatting context does
    /// not need a second style representation.
    pub flex_direction: FlexDirection,
    pub flex_wrap: FlexWrap,
    pub justify_content: JustifyContent,
    pub align_items: AlignItems,
    pub align_self: AlignItems,
    pub column_gap: LengthPercentage,
    pub row_gap: LengthPercentage,
    pub flex_grow: f32,
    pub flex_shrink: f32,
    pub flex_basis: Size,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum FlexDirection {
    Row,
    RowReverse,
    Column,
    ColumnReverse,
}

impl FlexDirection {
    pub fn is_row(self) -> bool {
        matches!(self, FlexDirection::Row | FlexDirection::RowReverse)
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum FlexWrap {
    NoWrap,
    Wrap,
    WrapReverse,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum JustifyContent {
    FlexStart,
    FlexEnd,
    Center,
    SpaceBetween,
    SpaceAround,
    SpaceEvenly,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum AlignItems {
    Auto,
    FlexStart,
    FlexEnd,
    Center,
    Baseline,
    Stretch,
}

impl Default for ComputedStyle {
    fn default() -> Self {
        ComputedStyle {
            display: Display::Inline,
            position: Position::Static,
            float: Float::None,
            clear: Clear::None,
            box_sizing: BoxSizing::ContentBox,
            overflow_x: Overflow::Visible,
            overflow_y: Overflow::Visible,
            width: Size::Auto,
            height: Size::Auto,
            min_width: Size::Auto,
            max_width: Size::None,
            min_height: Size::Auto,
            max_height: Size::None,
            margin: Sides::all(Margin::ZERO),
            padding: Sides::all(LengthPercentage::ZERO),
            border: Sides::all(0.0),
            inset: Sides::all(Inset::Auto),
            font_size: 16.0,
            font_style: FontStyle::Normal,
            font_weight: 400,
            font_family: String::new(),
            line_height: LineHeight::Normal,
            vertical_align: VerticalAlign::Baseline,
            text_align: TextAlign::Left,
            text_indent: LengthPercentage::ZERO,
            white_space: WhiteSpace::Normal,
            flex_direction: FlexDirection::Row,
            flex_wrap: FlexWrap::NoWrap,
            justify_content: JustifyContent::FlexStart,
            align_items: AlignItems::Stretch,
            align_self: AlignItems::Auto,
            column_gap: LengthPercentage::ZERO,
            row_gap: LengthPercentage::ZERO,
            flex_grow: 0.0,
            flex_shrink: 1.0,
            flex_basis: Size::Auto,
        }
    }
}

impl ComputedStyle {
    /// The style an anonymous box inherits: everything inheritable from
    /// `self`, everything else initial. Anonymous boxes exist because a block
    /// container with mixed children has to wrap the inline runs, and they must
    /// not carry the parent's margins or background.
    pub fn inherited_only(&self) -> ComputedStyle {
        ComputedStyle {
            display: Display::Block,
            font_size: self.font_size,
            font_style: self.font_style,
            font_weight: self.font_weight,
            font_family: self.font_family.clone(),
            line_height: self.line_height,
            text_align: self.text_align,
            text_indent: self.text_indent,
            white_space: self.white_space,
            ..ComputedStyle::default()
        }
    }

    /// Does this box establish a block formatting context? CSS 2.1 §9.4.1:
    /// floats, absolutely positioned boxes, inline-blocks, table cells, and
    /// block boxes with `overflow` other than `visible`. Flex and grid
    /// containers establish their own (independent) formatting contexts too.
    ///
    /// This predicate is load-bearing three times over: a BFC root contains its
    /// own floats (§10.6.3), margins never collapse across its boundary
    /// (§8.3.1), and floats outside it never intrude into it (§9.5).
    pub fn establishes_bfc(&self) -> bool {
        if self.float != Float::None
            || self.position.is_out_of_flow()
            || self.display.is_atomic_inline()
            || self.display == Display::TableCell
        {
            return true;
        }
        if self.display.inner() != InnerDisplay::Flow {
            return true;
        }
        self.overflow_x.establishes_bfc() || self.overflow_y.establishes_bfc()
    }

    /// The used border widths. `border-style: none` forces the width to zero
    /// regardless of `border-width`, which is why `border: 20px solid` and
    /// `border-width: 20px` alone behave differently.
    pub fn border_widths(&self) -> Sides<f32> {
        self.border
    }
}

// ---------------------------------------------------------------------------
// Parsing
// ---------------------------------------------------------------------------

/// Everything a length needs in order to become a number of pixels, except a
/// containing block.
#[derive(Debug, Clone, Copy)]
pub struct StyleContext {
    /// The font size of the element the declaration belongs to, for `em`.
    /// Note this is the *parent's* font size while `font-size` itself is being
    /// resolved, and the element's own for every other property.
    pub font_size: f32,
    /// The root element's font size, for `rem`.
    pub root_font_size: f32,
    pub viewport_width: f32,
    pub viewport_height: f32,
}

impl Default for StyleContext {
    fn default() -> Self {
        StyleContext {
            font_size: 16.0,
            root_font_size: 16.0,
            viewport_width: 1200.0,
            viewport_height: 900.0,
        }
    }
}

/// Parse a length, returning `None` for anything unrecognised so the caller can
/// keep the inherited or initial value rather than silently substituting zero.
pub fn parse_length_percentage(input: &str, cx: &StyleContext) -> Option<LengthPercentage> {
    let s = input.trim();
    if s.is_empty() {
        return None;
    }
    let lower = s.to_ascii_lowercase();
    if let Some(num) = lower.strip_suffix('%') {
        return num.trim().parse::<f32>().ok().map(LengthPercentage::Percent);
    }
    // Longest suffixes first so `rem` is not eaten by `em`.
    const UNITS: &[(&str, f32)] = &[
        ("vmin", 0.0),
        ("vmax", 0.0),
        ("rem", 0.0),
        ("px", 1.0),
        ("em", 0.0),
        ("ex", 0.0),
        ("ch", 0.0),
        ("vw", 0.0),
        ("vh", 0.0),
        ("pt", 96.0 / 72.0),
        ("pc", 16.0),
        ("in", 96.0),
        ("cm", 96.0 / 2.54),
        ("mm", 96.0 / 25.4),
        ("q", 96.0 / 101.6),
    ];
    for (unit, fixed) in UNITS {
        if let Some(num) = lower.strip_suffix(unit) {
            let v: f32 = num.trim().parse().ok()?;
            let px = match *unit {
                "em" => v * cx.font_size,
                // `ex` and `ch` want real font metrics; the conventional
                // fallbacks are half an em and half an em respectively, and
                // being explicit about the approximation beats pretending.
                "ex" => v * cx.font_size * 0.5,
                "ch" => v * cx.font_size * 0.5,
                "rem" => v * cx.root_font_size,
                "vw" => v * cx.viewport_width / 100.0,
                "vh" => v * cx.viewport_height / 100.0,
                "vmin" => v * cx.viewport_width.min(cx.viewport_height) / 100.0,
                "vmax" => v * cx.viewport_width.max(cx.viewport_height) / 100.0,
                _ => v * fixed,
            };
            return Some(LengthPercentage::Px(px));
        }
    }
    // A bare `0` is a valid length; any other bare number is not, but authors
    // write them often enough that treating them as pixels loses nothing.
    lower.parse::<f32>().ok().map(LengthPercentage::Px)
}

fn parse_size(input: &str, cx: &StyleContext) -> Option<Size> {
    let s = input.trim().to_ascii_lowercase();
    match s.as_str() {
        "auto" => Some(Size::Auto),
        "none" => Some(Size::None),
        "min-content" => Some(Size::MinContent),
        "max-content" => Some(Size::MaxContent),
        "fit-content" => Some(Size::FitContent),
        _ => parse_length_percentage(&s, cx).map(Size::LengthPercentage),
    }
}

fn parse_margin(input: &str, cx: &StyleContext) -> Option<Margin> {
    let s = input.trim();
    if s.eq_ignore_ascii_case("auto") {
        return Some(Margin::Auto);
    }
    parse_length_percentage(s, cx).map(Margin::LengthPercentage)
}

fn parse_inset(input: &str, cx: &StyleContext) -> Option<Inset> {
    let s = input.trim();
    if s.eq_ignore_ascii_case("auto") {
        return Some(Inset::Auto);
    }
    parse_length_percentage(s, cx).map(Inset::LengthPercentage)
}

/// Split a shorthand into its space-separated components, respecting nesting so
/// `1px solid rgb(0, 0, 0)` yields three tokens rather than five.
pub fn split_components(value: &str) -> Vec<String> {
    let mut out = Vec::new();
    let mut cur = String::new();
    let mut depth = 0i32;
    for c in value.chars() {
        match c {
            '(' => {
                depth += 1;
                cur.push(c);
            }
            ')' => {
                depth -= 1;
                cur.push(c);
            }
            c if c.is_whitespace() && depth == 0 => {
                if !cur.is_empty() {
                    out.push(std::mem::take(&mut cur));
                }
            }
            _ => cur.push(c),
        }
    }
    if !cur.is_empty() {
        out.push(cur);
    }
    out
}

/// Expand the 1-to-4 value box shorthand into top/right/bottom/left.
fn box_shorthand(parts: &[String]) -> Option<[String; 4]> {
    match parts.len() {
        1 => Some([parts[0].clone(), parts[0].clone(), parts[0].clone(), parts[0].clone()]),
        2 => Some([parts[0].clone(), parts[1].clone(), parts[0].clone(), parts[1].clone()]),
        3 => Some([parts[0].clone(), parts[1].clone(), parts[2].clone(), parts[1].clone()]),
        4 => Some([parts[0].clone(), parts[1].clone(), parts[2].clone(), parts[3].clone()]),
        _ => None,
    }
}

fn parse_border_width(token: &str, cx: &StyleContext) -> Option<f32> {
    match token.trim().to_ascii_lowercase().as_str() {
        "thin" => Some(1.0),
        "medium" => Some(3.0),
        "thick" => Some(5.0),
        other => match parse_length_percentage(other, cx)? {
            LengthPercentage::Px(v) => Some(v),
            // A percentage border-width is invalid; drop the declaration.
            LengthPercentage::Percent(_) => None,
        },
    }
}

fn is_border_style(token: &str) -> bool {
    matches!(
        token.trim().to_ascii_lowercase().as_str(),
        "none"
            | "hidden"
            | "dotted"
            | "dashed"
            | "solid"
            | "double"
            | "groove"
            | "ridge"
            | "inset"
            | "outset"
    )
}

fn font_size_keyword(token: &str) -> Option<f32> {
    // The CSS 2.1 absolute-size scale, with `medium` = 16px.
    Some(match token {
        "xx-small" => 9.0,
        "x-small" => 10.0,
        "small" => 13.0,
        "medium" => 16.0,
        "large" => 18.0,
        "x-large" => 24.0,
        "xx-large" => 32.0,
        _ => return None,
    })
}

/// Which properties inherit. Getting this list right matters more than it looks
/// — `line-height` inheriting as a *number* rather than a resolved length is
/// what makes `body { line-height: 1.5 }` scale per-element instead of freezing
/// at 24px for the whole document.
fn inherit_from(parent: &ComputedStyle) -> ComputedStyle {
    ComputedStyle {
        font_size: parent.font_size,
        font_style: parent.font_style,
        font_weight: parent.font_weight,
        font_family: parent.font_family.clone(),
        line_height: parent.line_height,
        text_align: parent.text_align,
        text_indent: parent.text_indent,
        white_space: parent.white_space,
        ..ComputedStyle::default()
    }
}

/// Turn a bag of declarations into a computed style, given the parent's.
///
/// `font-size` is resolved first and out of order, because every `em` in the
/// same rule resolves against the element's *own* font size, not its parent's.
/// Missing that ordering is the difference between `padding: 1em` on a 32px
/// heading meaning 32px and meaning 16px — which is precisely the Python
/// engine's cause #11, worth 788 elements on Wikipedia alone.
pub fn compute(
    decls: &HashMap<String, String>,
    parent: &ComputedStyle,
    cx: &StyleContext,
) -> ComputedStyle {
    let mut style = inherit_from(parent);

    // Pass 1: font-size, against the parent's font size.
    let parent_cx = StyleContext { font_size: parent.font_size, ..*cx };
    if let Some(raw) = decls.get("font-size") {
        let v = raw.trim().to_ascii_lowercase();
        if let Some(px) = font_size_keyword(&v) {
            style.font_size = px;
        } else if v == "larger" {
            style.font_size = parent.font_size * 1.2;
        } else if v == "smaller" {
            style.font_size = parent.font_size / 1.2;
        } else if let Some(lp) = parse_length_percentage(&v, &parent_cx) {
            style.font_size = match lp {
                LengthPercentage::Px(px) => px,
                // A percentage font-size is relative to the parent's.
                LengthPercentage::Percent(p) => parent.font_size * p / 100.0,
            };
        }
    }

    // Pass 2: everything else, against this element's own font size.
    let cx = StyleContext { font_size: style.font_size, ..*cx };
    apply(&mut style, decls, &cx);
    style
}

fn apply(style: &mut ComputedStyle, decls: &HashMap<String, String>, cx: &StyleContext) {
    // Shorthands run before longhands so an explicit `margin-top` after a
    // `margin` shorthand wins, which is the cascade order authors expect.
    for (name, longhands) in [
        ("margin", ["margin-top", "margin-right", "margin-bottom", "margin-left"]),
        ("padding", ["padding-top", "padding-right", "padding-bottom", "padding-left"]),
    ] {
        if let Some(raw) = decls.get(name) {
            if let Some(vals) = box_shorthand(&split_components(raw)) {
                for (lh, v) in longhands.iter().zip(vals.iter()) {
                    apply_one(style, lh, v, cx);
                }
            }
        }
    }
    if let Some(raw) = decls.get("border-width") {
        if let Some(vals) = box_shorthand(&split_components(raw)) {
            let sides = ["border-top-width", "border-right-width", "border-bottom-width", "border-left-width"];
            for (lh, v) in sides.iter().zip(vals.iter()) {
                apply_one(style, lh, v, cx);
            }
        }
    }
    if let Some(raw) = decls.get("border-style") {
        if let Some(vals) = box_shorthand(&split_components(raw)) {
            for (i, v) in vals.iter().enumerate() {
                if v.trim().eq_ignore_ascii_case("none") || v.trim().eq_ignore_ascii_case("hidden") {
                    set_border_side(style, i, 0.0);
                }
            }
        }
    }
    // `border: 1px solid red` and its per-side forms.
    for (name, side) in [
        ("border", 4usize),
        ("border-top", 0),
        ("border-right", 1),
        ("border-bottom", 2),
        ("border-left", 3),
    ] {
        if let Some(raw) = decls.get(name) {
            let parts = split_components(raw);
            let mut width = None;
            let mut none_style = false;
            for p in &parts {
                if is_border_style(p) {
                    if p.eq_ignore_ascii_case("none") || p.eq_ignore_ascii_case("hidden") {
                        none_style = true;
                    } else if width.is_none() {
                        // A style keyword with no width means `medium`.
                        width = Some(3.0);
                    }
                } else if let Some(w) = parse_border_width(p, cx) {
                    width = Some(w);
                }
            }
            let used = if none_style { 0.0 } else { width.unwrap_or(0.0) };
            if side == 4 {
                style.border = Sides::all(used);
            } else {
                set_border_side(style, side, used);
            }
        }
    }
    if let Some(raw) = decls.get("inset") {
        if let Some(vals) = box_shorthand(&split_components(raw)) {
            for (lh, v) in ["top", "right", "bottom", "left"].iter().zip(vals.iter()) {
                apply_one(style, lh, v, cx);
            }
        }
    }
    if let Some(raw) = decls.get("gap") {
        let parts = split_components(raw);
        if let Some(row) = parts.first().and_then(|v| parse_length_percentage(v, cx)) {
            style.row_gap = row;
            style.column_gap = parts
                .get(1)
                .and_then(|v| parse_length_percentage(v, cx))
                .unwrap_or(row);
        }
    }
    if let Some(raw) = decls.get("flex") {
        apply_flex_shorthand(style, raw, cx);
    }
    if let Some(raw) = decls.get("overflow") {
        let parts = split_components(raw);
        if let Some(x) = parts.first().and_then(|v| parse_overflow(v)) {
            style.overflow_x = x;
            style.overflow_y = parts.get(1).and_then(|v| parse_overflow(v)).unwrap_or(x);
        }
    }

    for (name, value) in decls {
        apply_one(style, name, value, cx);
    }
}

fn set_border_side(style: &mut ComputedStyle, side: usize, v: f32) {
    match side {
        0 => style.border.top = v,
        1 => style.border.right = v,
        2 => style.border.bottom = v,
        _ => style.border.left = v,
    }
}

fn parse_overflow(v: &str) -> Option<Overflow> {
    Some(match v.trim().to_ascii_lowercase().as_str() {
        "visible" => Overflow::Visible,
        "hidden" => Overflow::Hidden,
        "scroll" => Overflow::Scroll,
        "auto" => Overflow::Auto,
        "clip" => Overflow::Clip,
        _ => return None,
    })
}

fn apply_flex_shorthand(style: &mut ComputedStyle, raw: &str, cx: &StyleContext) {
    let v = raw.trim().to_ascii_lowercase();
    match v.as_str() {
        "none" => {
            style.flex_grow = 0.0;
            style.flex_shrink = 0.0;
            style.flex_basis = Size::Auto;
            return;
        }
        "auto" => {
            style.flex_grow = 1.0;
            style.flex_shrink = 1.0;
            style.flex_basis = Size::Auto;
            return;
        }
        "initial" => {
            style.flex_grow = 0.0;
            style.flex_shrink = 1.0;
            style.flex_basis = Size::Auto;
            return;
        }
        _ => {}
    }
    let parts = split_components(&v);
    let mut numbers = Vec::new();
    let mut basis = None;
    for p in &parts {
        if let Ok(n) = p.parse::<f32>() {
            numbers.push(n);
        } else if let Some(s) = parse_size(p, cx) {
            basis = Some(s);
        }
    }
    // `flex: <grow>` alone sets basis to 0, not auto — the single most
    // commonly mis-remembered default in the spec.
    match numbers.len() {
        1 => {
            style.flex_grow = numbers[0];
            style.flex_shrink = 1.0;
            style.flex_basis = basis.unwrap_or(Size::LengthPercentage(LengthPercentage::Px(0.0)));
        }
        2 => {
            style.flex_grow = numbers[0];
            style.flex_shrink = numbers[1];
            style.flex_basis = basis.unwrap_or(Size::LengthPercentage(LengthPercentage::Px(0.0)));
        }
        _ => {
            if let Some(b) = basis {
                style.flex_basis = b;
                style.flex_grow = 1.0;
                style.flex_shrink = 1.0;
            }
        }
    }
}

fn apply_one(style: &mut ComputedStyle, name: &str, raw: &str, cx: &StyleContext) {
    let value = raw.trim();
    let lower = value.to_ascii_lowercase();
    match name {
        "display" => {
            style.display = match lower.as_str() {
                "none" => Display::None,
                "block" => Display::Block,
                "inline" => Display::Inline,
                "inline-block" => Display::InlineBlock,
                "flex" => Display::Flex,
                "inline-flex" => Display::InlineFlex,
                "grid" => Display::Grid,
                "inline-grid" => Display::InlineGrid,
                "list-item" => Display::ListItem,
                "table" => Display::Table,
                "inline-table" => Display::InlineTable,
                "table-row" => Display::TableRow,
                "table-cell" => Display::TableCell,
                "table-row-group" | "table-header-group" | "table-footer-group" => {
                    Display::TableRowGroup
                }
                _ => return,
            }
        }
        "position" => {
            style.position = match lower.as_str() {
                "static" => Position::Static,
                "relative" => Position::Relative,
                "absolute" => Position::Absolute,
                "fixed" => Position::Fixed,
                "sticky" => Position::Sticky,
                _ => return,
            }
        }
        "float" => {
            style.float = match lower.as_str() {
                "none" => Float::None,
                "left" => Float::Left,
                "right" => Float::Right,
                _ => return,
            }
        }
        "clear" => {
            style.clear = match lower.as_str() {
                "none" => Clear::None,
                "left" => Clear::Left,
                "right" => Clear::Right,
                "both" => Clear::Both,
                _ => return,
            }
        }
        "box-sizing" => {
            style.box_sizing = match lower.as_str() {
                "border-box" => BoxSizing::BorderBox,
                "content-box" => BoxSizing::ContentBox,
                _ => return,
            }
        }
        "overflow-x" => {
            if let Some(o) = parse_overflow(&lower) {
                style.overflow_x = o;
            }
        }
        "overflow-y" => {
            if let Some(o) = parse_overflow(&lower) {
                style.overflow_y = o;
            }
        }
        "width" => {
            if let Some(s) = parse_size(&lower, cx) {
                style.width = s;
            }
        }
        "height" => {
            if let Some(s) = parse_size(&lower, cx) {
                style.height = s;
            }
        }
        "min-width" => {
            if let Some(s) = parse_size(&lower, cx) {
                style.min_width = if s == Size::None { Size::Auto } else { s };
            }
        }
        "max-width" => {
            if let Some(s) = parse_size(&lower, cx) {
                style.max_width = s;
            }
        }
        "min-height" => {
            if let Some(s) = parse_size(&lower, cx) {
                style.min_height = if s == Size::None { Size::Auto } else { s };
            }
        }
        "max-height" => {
            if let Some(s) = parse_size(&lower, cx) {
                style.max_height = s;
            }
        }
        "margin-top" => {
            if let Some(m) = parse_margin(&lower, cx) {
                style.margin.top = m;
            }
        }
        "margin-right" => {
            if let Some(m) = parse_margin(&lower, cx) {
                style.margin.right = m;
            }
        }
        "margin-bottom" => {
            if let Some(m) = parse_margin(&lower, cx) {
                style.margin.bottom = m;
            }
        }
        "margin-left" => {
            if let Some(m) = parse_margin(&lower, cx) {
                style.margin.left = m;
            }
        }
        "padding-top" => {
            if let Some(p) = parse_length_percentage(&lower, cx) {
                style.padding.top = p;
            }
        }
        "padding-right" => {
            if let Some(p) = parse_length_percentage(&lower, cx) {
                style.padding.right = p;
            }
        }
        "padding-bottom" => {
            if let Some(p) = parse_length_percentage(&lower, cx) {
                style.padding.bottom = p;
            }
        }
        "padding-left" => {
            if let Some(p) = parse_length_percentage(&lower, cx) {
                style.padding.left = p;
            }
        }
        "border-top-width" => {
            if let Some(w) = parse_border_width(&lower, cx) {
                style.border.top = w;
            }
        }
        "border-right-width" => {
            if let Some(w) = parse_border_width(&lower, cx) {
                style.border.right = w;
            }
        }
        "border-bottom-width" => {
            if let Some(w) = parse_border_width(&lower, cx) {
                style.border.bottom = w;
            }
        }
        "border-left-width" => {
            if let Some(w) = parse_border_width(&lower, cx) {
                style.border.left = w;
            }
        }
        "top" => {
            if let Some(i) = parse_inset(&lower, cx) {
                style.inset.top = i;
            }
        }
        "right" => {
            if let Some(i) = parse_inset(&lower, cx) {
                style.inset.right = i;
            }
        }
        "bottom" => {
            if let Some(i) = parse_inset(&lower, cx) {
                style.inset.bottom = i;
            }
        }
        "left" => {
            if let Some(i) = parse_inset(&lower, cx) {
                style.inset.left = i;
            }
        }
        "font-style" => {
            style.font_style = match lower.as_str() {
                "italic" | "oblique" => FontStyle::Italic,
                _ => FontStyle::Normal,
            }
        }
        "font-weight" => {
            style.font_weight = match lower.as_str() {
                "normal" => 400,
                "bold" => 700,
                "bolder" => 700,
                "lighter" => 300,
                v => v.parse().unwrap_or(400),
            }
        }
        "font-family" => style.font_family = value.to_string(),
        "line-height" => {
            style.line_height = if lower == "normal" {
                LineHeight::Normal
            } else if let Ok(n) = lower.parse::<f32>() {
                LineHeight::Number(n)
            } else if let Some(lp) = parse_length_percentage(&lower, cx) {
                match lp {
                    LengthPercentage::Px(v) => LineHeight::Px(v),
                    // A percentage line-height resolves against the element's
                    // own font size at computed-value time, and then inherits
                    // as that fixed length.
                    LengthPercentage::Percent(p) => LineHeight::Px(cx.font_size * p / 100.0),
                }
            } else {
                return;
            }
        }
        "vertical-align" => {
            style.vertical_align = match lower.as_str() {
                "baseline" => VerticalAlign::Baseline,
                "sub" => VerticalAlign::Sub,
                "super" => VerticalAlign::Super,
                "text-top" => VerticalAlign::TextTop,
                "text-bottom" => VerticalAlign::TextBottom,
                "middle" => VerticalAlign::Middle,
                "top" => VerticalAlign::Top,
                "bottom" => VerticalAlign::Bottom,
                _ => match parse_length_percentage(&lower, cx) {
                    Some(lp) => VerticalAlign::Length(lp),
                    None => return,
                },
            }
        }
        "text-align" => {
            style.text_align = match lower.as_str() {
                "left" | "start" => TextAlign::Left,
                "right" | "end" => TextAlign::Right,
                "center" => TextAlign::Center,
                "justify" => TextAlign::Justify,
                _ => return,
            }
        }
        "text-indent" => {
            if let Some(lp) = parse_length_percentage(&lower, cx) {
                style.text_indent = lp;
            }
        }
        "white-space" => {
            style.white_space = match lower.as_str() {
                "normal" => WhiteSpace::Normal,
                "pre" => WhiteSpace::Pre,
                "nowrap" => WhiteSpace::NoWrap,
                "pre-wrap" => WhiteSpace::PreWrap,
                "pre-line" => WhiteSpace::PreLine,
                _ => return,
            }
        }
        "flex-direction" => {
            style.flex_direction = match lower.as_str() {
                "row" => FlexDirection::Row,
                "row-reverse" => FlexDirection::RowReverse,
                "column" => FlexDirection::Column,
                "column-reverse" => FlexDirection::ColumnReverse,
                _ => return,
            }
        }
        "flex-wrap" => {
            style.flex_wrap = match lower.as_str() {
                "nowrap" => FlexWrap::NoWrap,
                "wrap" => FlexWrap::Wrap,
                "wrap-reverse" => FlexWrap::WrapReverse,
                _ => return,
            }
        }
        "justify-content" => {
            style.justify_content = match lower.as_str() {
                "flex-start" | "start" | "left" | "normal" => JustifyContent::FlexStart,
                "flex-end" | "end" | "right" => JustifyContent::FlexEnd,
                "center" => JustifyContent::Center,
                "space-between" => JustifyContent::SpaceBetween,
                "space-around" => JustifyContent::SpaceAround,
                "space-evenly" => JustifyContent::SpaceEvenly,
                _ => return,
            }
        }
        "align-items" => {
            if let Some(a) = parse_align(&lower) {
                style.align_items = a;
            }
        }
        "align-self" => {
            if let Some(a) = parse_align(&lower) {
                style.align_self = a;
            }
        }
        "column-gap" => {
            if let Some(lp) = parse_length_percentage(&lower, cx) {
                style.column_gap = lp;
            }
        }
        "row-gap" => {
            if let Some(lp) = parse_length_percentage(&lower, cx) {
                style.row_gap = lp;
            }
        }
        "flex-grow" => {
            if let Ok(n) = lower.parse::<f32>() {
                style.flex_grow = n;
            }
        }
        "flex-shrink" => {
            if let Ok(n) = lower.parse::<f32>() {
                style.flex_shrink = n;
            }
        }
        "flex-basis" => {
            if let Some(s) = parse_size(&lower, cx) {
                style.flex_basis = s;
            }
        }
        _ => {}
    }
}

fn parse_align(v: &str) -> Option<AlignItems> {
    Some(match v {
        "auto" => AlignItems::Auto,
        "flex-start" | "start" | "self-start" => AlignItems::FlexStart,
        "flex-end" | "end" | "self-end" => AlignItems::FlexEnd,
        "center" => AlignItems::Center,
        "baseline" => AlignItems::Baseline,
        "stretch" | "normal" => AlignItems::Stretch,
        _ => return None,
    })
}
