"""Software rasteriser: a pixel buffer and the operations that mark it.

This replaces the drawing half of Tk. A Surface is a flat RGB buffer plus a
clip rectangle; everything else -- rectangles, lines, glyphs, images -- is
composited into it here.

The Surface itself lives in Rust, in the `feetbrowser_engine` extension, and
so does its framebuffer: `surface.pixels` is a read-only memoryview onto the
buffer Rust owns, which is what lets the window backend blit a frame without
copying it. The class is otherwise exactly what it was -- same methods, same
arguments, same clip semantics.

The performance shape worth knowing: filling a rectangle is a run of memory
writes, while anti-aliased glyph coverage is blended a pixel at a time and
dominates the cost of a text page. That is why glyph coverage bitmaps are
rasterised once and cached per (face, size, glyph) -- drawing a character the
second time is a blend of an existing bitmap, never a re-run of the scanline
fill.
"""
from feetbrowser_engine import Surface, rasterize

from . import fontengine

# Vertical subsamples per pixel row when rasterising outlines. Horizontal
# coverage is computed analytically, so 4 rows is enough to look smooth.
SUBSAMPLES = 4

__all__ = ["Surface", "SUBSAMPLES", "rasterize", "glyph_bitmap", "draw_text",
           "measure_text"]


# -- outline rasterisation ------------------------------------------------
#
# `rasterize` and its span accumulator are in Rust: they are the innermost
# loop of the renderer, run once per uncached glyph and once per polygon a
# page draws, and a 200x200 star went from 4.5ms to a tenth of that.

# -- glyph cache ----------------------------------------------------------

# Rasterised glyphs are cached on the font that produced them, so the cache
# lives and dies with the face. Keying on id(font) instead would let a
# collected font hand its address -- and its glyph shapes -- to the next one.
_GLYPH_CACHE_MAX = 20000


def glyph_bitmap(font, size, gid):
    """Coverage bitmap for one glyph: ``(cov, w, h, left, top)``.

    ``left``/``top`` are offsets from the pen position on the baseline to the
    bitmap's top-left corner, so callers place it without re-reading the
    outline.
    """
    cache = getattr(font, "_bitmaps", None)
    if cache is None:
        cache = font._bitmaps = {}
    key = (size, gid)
    hit = cache.get(key)
    if hit is not None:
        return hit

    scale = font.scale(size)
    polys = fontengine.flatten(font.glyph_contours(gid), scale)
    if not polys:
        result = (bytearray(), 0, 0, 0, 0)
    else:
        xs = [p[0] for poly in polys for p in poly]
        ys = [p[1] for poly in polys for p in poly]
        left = int(min(xs)) - 1
        top = int(min(ys)) - 1
        w = int(max(xs)) - left + 2
        h = int(max(ys)) - top + 2
        if w <= 0 or h <= 0 or w > 4096 or h > 4096:
            result = (bytearray(), 0, 0, 0, 0)
        else:
            cov = rasterize(polys, w, h, -left, -top)
            result = (cov, w, h, left, top)

    if len(cache) < _GLYPH_CACHE_MAX:
        cache[key] = result
    return result


def draw_text(surface, font, size, text, x, baseline, color):
    """Draw a string, returning the advance in pixels.

    Advances are summed per character with no kerning, which keeps the
    layout engine's per-character width cache exact.
    """
    scale = font.scale(size)
    pen = float(x)
    for ch in text:
        gid = font.glyph_id(ch)
        adv = font.advance(gid) * scale
        if ch not in " \t":
            cov, w, h, left, top = glyph_bitmap(font, size, gid)
            if w:
                surface.blit_coverage(cov, w, h,
                                      int(pen) + left, int(baseline) + top,
                                      color)
        pen += adv
    return pen - x


def measure_text(font, size, text):
    """Advance width of a string in pixels."""
    scale = font.scale(size)
    return sum(font.advance(font.glyph_id(ch)) for ch in text) * scale
