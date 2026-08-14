"""Software rasteriser: a pixel buffer and the operations that mark it.

This replaces the drawing half of Tk. A Surface is a flat RGB buffer plus a
clip rectangle; everything else -- rectangles, lines, glyphs, images -- is
composited into it here.

All of it lives in Rust now, in the `feetbrowser_engine` extension, including
the framebuffer: `surface.pixels` is a read-only memoryview onto the buffer
Rust owns, which is what lets the window backend blit a frame without copying
it. The API is unchanged -- same functions, same arguments, same clip
semantics -- so this module is the name the rest of the browser knows it by.

The performance shape worth knowing: filling a rectangle is a run of memory
writes, while anti-aliased glyph coverage is blended a pixel at a time and
dominates the cost of a text page. That is why glyph coverage bitmaps are
rasterised once and cached per (face, size, glyph) -- drawing a character the
second time is a blend of an existing bitmap, never a re-run of the scanline
fill. The cache is kept on the face, so it lives and dies with it.
"""
from feetbrowser_engine import (Surface, draw_text, glyph_bitmap, measure_text,
                                rasterize)

# Vertical subsamples per pixel row when rasterising outlines. Horizontal
# coverage is computed analytically, so 4 rows is enough to look smooth.
SUBSAMPLES = 4

__all__ = ["Surface", "SUBSAMPLES", "rasterize", "glyph_bitmap", "draw_text",
           "measure_text"]
