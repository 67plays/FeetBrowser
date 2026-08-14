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
from feetbrowser_engine import Surface

from . import fontengine

# Vertical subsamples per pixel row when rasterising outlines. Horizontal
# coverage is computed analytically, so 4 rows is enough to look smooth.
SUBSAMPLES = 4

__all__ = ["Surface", "SUBSAMPLES", "rasterize", "glyph_bitmap", "draw_text",
           "measure_text"]


# -- outline rasterisation ------------------------------------------------

def rasterize(polys, width, height, offset_x=0.0, offset_y=0.0):
    """Scan-convert polygons into an 8-bit coverage bitmap.

    Nonzero winding, matching TrueType. Coverage is sampled at SUBSAMPLES
    rows per pixel and computed analytically across each span, so edges get
    real anti-aliasing rather than a hard threshold.
    """
    cov = bytearray(width * height)
    if width <= 0 or height <= 0:
        return cov

    edges = []
    for poly in polys:
        n = len(poly)
        for i in range(n):
            x0, y0 = poly[i]
            x1, y1 = poly[(i + 1) % n]
            x0 += offset_x
            y0 += offset_y
            x1 += offset_x
            y1 += offset_y
            if y0 == y1:
                continue  # horizontal edges never cross a scanline
            edges.append((y0, y1, x0, (x1 - x0) / (y1 - y0),
                          1 if y1 > y0 else -1))
    if not edges:
        return cov

    top = max(0, int(min(min(e[0], e[1]) for e in edges)))
    bottom = min(height, int(max(max(e[0], e[1]) for e in edges)) + 1)
    step = 1.0 / SUBSAMPLES
    unit = 255.0 / SUBSAMPLES

    for py in range(top, bottom):
        acc = [0.0] * width
        hit = False
        for k in range(SUBSAMPLES):
            sy = py + (k + 0.5) * step
            xs = []
            for y0, y1, x0, slope, wind in edges:
                if (y0 <= sy < y1) or (y1 <= sy < y0):
                    xs.append((x0 + (sy - y0) * slope, wind))
            if len(xs) < 2:
                continue
            xs.sort()
            winding = 0
            span_start = 0.0
            for x, wind in xs:
                if winding == 0:
                    span_start = x
                winding += wind
                if winding == 0 and x > span_start:
                    _add_span(acc, span_start, x, unit, width)
                    hit = True
        if not hit:
            continue
        base = py * width
        for i, v in enumerate(acc):
            if v > 0:
                cov[base + i] = 255 if v >= 255 else int(v)
    return cov


def _add_span(acc, x0, x1, unit, width):
    """Add one subsample row's coverage for the span [x0, x1)."""
    if x1 <= 0 or x0 >= width:
        return
    x0 = max(x0, 0.0)
    x1 = min(x1, float(width))
    i0 = int(x0)
    i1 = int(x1)
    if i0 == i1:
        acc[i0] += (x1 - x0) * unit
        return
    acc[i0] += (i0 + 1 - x0) * unit
    for i in range(i0 + 1, i1):
        acc[i] += unit
    if i1 < width:
        acc[i1] += (x1 - i1) * unit


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
