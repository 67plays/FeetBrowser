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

The translucent fill used to have two Python implementations under it: a
256-entry translate table per channel, and, where the raw span kernels are
real machine code, a call into `asmblend` per row. Both are gone from this
path, because they were solving the problem the Rust fill solves and the Rust
fill does the whole rectangle in one crossing rather than one per row. The
kernels themselves stay -- `asmblend.py`, `asm/spanblend.S` and their tests
are another contributor's work and still stand on their own -- but nothing in
the browser calls them now. Worth knowing that the two paths did not round
identically: the assembly took `(src*a + dst*(255-a)) >> 8`, the tables and
this code take `// 255`, so a channel could land one level darker at the top
of the range on Linux/x86-64 and only there.
"""
from feetbrowser_engine import (Surface, draw_text, glyph_bitmap, measure_text,
                                rasterize)

# Vertical subsamples per pixel row when rasterising outlines. Horizontal
# coverage is computed analytically, so 4 rows is enough to look smooth.
SUBSAMPLES = 4

__all__ = ["Surface", "SUBSAMPLES", "rasterize", "glyph_bitmap", "draw_text",
           "measure_text"]
