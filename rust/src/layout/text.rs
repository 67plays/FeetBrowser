//! Font metrics, behind a trait.
//!
//! Layout needs three numbers from a font — a string's advance width, and the
//! ascent and descent of the face — and it needs them for every inline box on
//! the page. `crate::font::Font` can supply all three, but its only constructor
//! takes a `&Bound<'_, PyAny>` and there is no font embedded in the binary, so
//! depending on it directly would make this module untestable without a Python
//! interpreter and a font file on disk.
//!
//! So the engine asks a trait. The real adapter lives with the browser; the
//! tests supply a face whose metrics are stated outright, which means a test
//! that fails tells you the layout arithmetic is wrong rather than that a font
//! got substituted.

use super::style::{ComputedStyle, FontStyle};

/// The vertical metrics of a font at a particular size, in pixels.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct FontMetrics {
    /// Distance from the baseline up to the top of the em box. Positive.
    pub ascent: f32,
    /// Distance from the baseline down to the bottom. Positive, unlike the
    /// signed value `hhea` stores.
    pub descent: f32,
    /// What `line-height: normal` means for this face. CSS leaves this to the
    /// UA; browsers use the font's own line spacing, which is why `normal` is
    /// not a fixed multiple and why hardcoding 1.25 (as the Python engine does)
    /// is wrong for every face at once.
    pub normal_line_height: f32,
}

impl FontMetrics {
    /// The content area of an inline box: `ascent + descent`, per §10.6.1.
    pub fn content_height(&self) -> f32 {
        self.ascent + self.descent
    }
}

/// A font key: everything about a style that can change a glyph's advance.
#[derive(Debug, Clone, PartialEq)]
pub struct FontKey {
    pub family: String,
    pub size: f32,
    pub weight: u16,
    pub italic: bool,
}

impl FontKey {
    pub fn of(style: &ComputedStyle) -> FontKey {
        FontKey {
            family: style.font_family.clone(),
            size: style.font_size,
            weight: style.font_weight,
            italic: style.font_style == FontStyle::Italic,
        }
    }
}

/// What layout needs from the outside world in order to measure text.
pub trait FontSource {
    /// Advance width of `text`, in pixels.
    ///
    /// Implementations must be additive — `advance(a) + advance(b)` must equal
    /// `advance(ab)` — because line breaking measures words separately and then
    /// assumes their sum is the line's width. `raster::measure_text` already
    /// guarantees this by summing in font units and scaling once at the end.
    fn advance(&self, key: &FontKey, text: &str) -> f32;

    fn metrics(&self, key: &FontKey) -> FontMetrics;
}

/// A deterministic face for tests and for the no-font case.
///
/// Every metric is a stated fraction of the em, so any expectation written
/// against it can be checked by hand. `advance_table` lets a test pin the width
/// of a specific string to a measured value — used where a Chrome expectation
/// depends on a real face's advances and the point of the test is the box
/// arithmetic around the text, not the text itself.
#[derive(Debug, Clone)]
pub struct StubFont {
    pub ascent_ratio: f32,
    pub descent_ratio: f32,
    pub normal_ratio: f32,
    pub advance_ratio: f32,
    pub bold_ratio: f32,
    /// `(text, font_size) -> advance`, consulted before the per-character
    /// fallback.
    pub advance_table: Vec<(String, f32, f32)>,
}

impl Default for StubFont {
    fn default() -> Self {
        StubFont {
            // 0.8 / 0.2 sums to exactly one em, so a 16px font has a 16px
            // content area and the arithmetic in a failing test is legible.
            ascent_ratio: 0.8,
            descent_ratio: 0.2,
            // Close to what the common web faces report, and deliberately not
            // 1.0, so a test that accidentally depends on `normal` shows it.
            normal_ratio: 1.15,
            advance_ratio: 0.5,
            bold_ratio: 1.0,
            advance_table: Vec::new(),
        }
    }
}

impl StubFont {
    pub fn with_advance(mut self, text: &str, size: f32, advance: f32) -> Self {
        self.advance_table.push((text.to_string(), size, advance));
        self
    }
}

impl FontSource for StubFont {
    fn advance(&self, key: &FontKey, text: &str) -> f32 {
        for (t, size, adv) in &self.advance_table {
            if t == text && (*size - key.size).abs() < 0.01 {
                return *adv;
            }
        }
        let bold = if key.weight >= 600 { self.bold_ratio } else { 1.0 };
        text.chars().count() as f32 * key.size * self.advance_ratio * bold
    }

    fn metrics(&self, key: &FontKey) -> FontMetrics {
        FontMetrics {
            ascent: key.size * self.ascent_ratio,
            descent: key.size * self.descent_ratio,
            normal_line_height: key.size * self.normal_ratio,
        }
    }
}

/// The used `line-height` of a box, and the half-leading that follows from it.
///
/// CSS 2.1 §10.8.1: the leading is `line-height − (ascent + descent)`, half of
/// it goes above the content area and half below, and the resulting box is what
/// the line box is built from. Negative leading is legal and common —
/// `line-height: 1` on a face whose ascent and descent already exceed the em
/// gives overlapping lines, which is exactly what the author asked for.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct InlineMetrics {
    /// Distance from the top of the inline box down to the baseline.
    pub above_baseline: f32,
    /// Distance from the baseline down to the bottom of the inline box.
    pub below_baseline: f32,
}

impl InlineMetrics {
    pub fn height(&self) -> f32 {
        self.above_baseline + self.below_baseline
    }

    pub fn for_style(style: &ComputedStyle, fonts: &dyn FontSource) -> InlineMetrics {
        let key = FontKey::of(style);
        let m = fonts.metrics(&key);
        let line_height = style
            .line_height
            .resolve(style.font_size, m.normal_line_height / style.font_size.max(0.001));
        Self::from_parts(&m, line_height)
    }

    pub fn from_parts(m: &FontMetrics, line_height: f32) -> InlineMetrics {
        let half_leading = (line_height - m.content_height()) / 2.0;
        InlineMetrics {
            above_baseline: m.ascent + half_leading,
            below_baseline: m.descent + half_leading,
        }
    }
}
