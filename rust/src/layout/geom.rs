//! Rectangles and the box model's four nested boxes.

use super::style::Sides;

#[derive(Debug, Clone, Copy, PartialEq, Default)]
pub struct Rect {
    pub x: f32,
    pub y: f32,
    pub width: f32,
    pub height: f32,
}

impl Rect {
    pub fn new(x: f32, y: f32, width: f32, height: f32) -> Rect {
        Rect { x, y, width, height }
    }
    pub fn right(&self) -> f32 {
        self.x + self.width
    }
    pub fn bottom(&self) -> f32 {
        self.y + self.height
    }
    /// Shrink by a set of edge widths — border box to content box, say.
    pub fn deflate(&self, e: Sides<f32>) -> Rect {
        Rect {
            x: self.x + e.left,
            y: self.y + e.top,
            width: (self.width - e.left - e.right).max(0.0),
            height: (self.height - e.top - e.bottom).max(0.0),
        }
    }
    pub fn inflate(&self, e: Sides<f32>) -> Rect {
        Rect {
            x: self.x - e.left,
            y: self.y - e.top,
            width: self.width + e.left + e.right,
            height: self.height + e.top + e.bottom,
        }
    }
    pub fn translate(&self, dx: f32, dy: f32) -> Rect {
        Rect { x: self.x + dx, y: self.y + dy, ..*self }
    }
    pub fn intersects_vertically(&self, top: f32, bottom: f32) -> bool {
        self.y < bottom && top < self.bottom()
    }
}

/// The used values of the edges around a box's content. Keeping these together
/// is what lets a single piece of code speak about "the border box" without
/// re-deriving it from style at every use — the Python engine recomputes
/// `_padding_box` at each call site and forgets the border at most of them.
#[derive(Debug, Clone, Copy, PartialEq, Default)]
pub struct BoxEdges {
    pub margin: Sides<f32>,
    pub border: Sides<f32>,
    pub padding: Sides<f32>,
}

impl BoxEdges {
    /// Everything between the content box and the border box.
    pub fn padding_border(&self) -> Sides<f32> {
        self.padding + self.border
    }
    pub fn inline_padding_border(&self) -> f32 {
        self.padding.inline_sum() + self.border.inline_sum()
    }
    pub fn block_padding_border(&self) -> f32 {
        self.padding.block_sum() + self.border.block_sum()
    }
    /// Content box to margin box, on the inline axis.
    pub fn inline_outer(&self) -> f32 {
        self.margin.inline_sum() + self.inline_padding_border()
    }
}

impl Default for Sides<f32> {
    fn default() -> Self {
        Sides::ZERO
    }
}
