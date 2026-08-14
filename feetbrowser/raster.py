"""Software rasteriser: a pixel buffer and the operations that mark it.

This replaces the drawing half of Tk. A Surface is a flat RGB bytearray plus
a clip rectangle; everything else -- rectangles, lines, glyphs, images -- is
composited into it here.

The performance shape worth knowing: filling is done with whole-row slice
assignment, which runs at memcpy speed, while anti-aliased glyph coverage is
blended a pixel at a time and dominates the cost of a text page. That is why
glyph coverage bitmaps are rasterised once and cached per (face, size, glyph)
-- drawing a character the second time is a blend of an existing bitmap, never
a re-run of the scanline fill.
"""
import struct
import zlib

from . import fontengine

# Vertical subsamples per pixel row when rasterising outlines. Horizontal
# coverage is computed analytically, so 4 rows is enough to look smooth.
SUBSAMPLES = 4


_BLEND_TABLES = {}


def _blend_tables(color, alpha):
    """Per-channel translate tables mapping a destination byte to the result
    of blending `color` over it at `alpha`."""
    key = (color, alpha)
    try:
        return _BLEND_TABLES[key]
    except KeyError:
        pass
    inv = 255 - alpha
    tables = tuple(bytes((v * inv + c * alpha) // 255 for v in range(256))
                   for c in color)
    if len(_BLEND_TABLES) < 4096:
        _BLEND_TABLES[key] = tables
    return tables


class Surface:
    """An RGB pixel buffer with a clip rectangle."""

    def __init__(self, width, height, background=(255, 255, 255)):
        self.width = max(1, int(width))
        self.height = max(1, int(height))
        self.stride = self.width * 3
        self.pixels = bytearray(self.stride * self.height)
        self.clip = (0, 0, self.width, self.height)
        self.fill_all(background)

    # -- clipping --------------------------------------------------------

    def set_clip(self, x0, y0, x1, y1):
        """Restrict drawing to a rectangle; returns the previous clip."""
        old = self.clip
        self.clip = (max(0, int(x0)), max(0, int(y0)),
                     min(self.width, int(x1)), min(self.height, int(y1)))
        return old

    def reset_clip(self, saved=None):
        self.clip = saved if saved else (0, 0, self.width, self.height)

    # -- fills -----------------------------------------------------------

    def fill_all(self, color):
        r, g, b = color
        self.pixels[:] = bytes((r, g, b)) * (self.width * self.height)

    def fill_rect(self, x0, y0, x1, y1, color, alpha=255):
        """Axis-aligned fill. Opaque fills go row-at-a-time via slice assign."""
        cx0, cy0, cx1, cy1 = self.clip
        x0 = max(int(x0), cx0)
        y0 = max(int(y0), cy0)
        x1 = min(int(x1), cx1)
        y1 = min(int(y1), cy1)
        if x0 >= x1 or y0 >= y1 or alpha <= 0:
            return
        w = x1 - x0
        span = w * 3
        if alpha >= 255:
            row = bytes(color) * w
            for y in range(y0, y1):
                o = y * self.stride + x0 * 3
                self.pixels[o:o + span] = row
            return
        # Translucent fill. Blending each pixel in Python costs milliseconds
        # on a full-page shadow, so instead each channel gets a 256-entry
        # translate table and is blended as a strided slice -- three C-level
        # passes per row rather than three Python operations per pixel.
        tr, tg, tb = _blend_tables(color, alpha)
        px = self.pixels
        for y in range(y0, y1):
            o = y * self.stride + x0 * 3
            end = o + span
            px[o:end:3] = px[o:end:3].translate(tr)
            px[o + 1:end:3] = px[o + 1:end:3].translate(tg)
            px[o + 2:end:3] = px[o + 2:end:3].translate(tb)

    def outline_rect(self, x0, y0, x1, y1, color, thickness=1, alpha=255):
        t = max(1, int(thickness))
        self.fill_rect(x0, y0, x1, y0 + t, color, alpha)
        self.fill_rect(x0, y1 - t, x1, y1, color, alpha)
        self.fill_rect(x0, y0 + t, x0 + t, y1 - t, color, alpha)
        self.fill_rect(x1 - t, y0 + t, x1, y1 - t, color, alpha)

    def draw_line(self, x0, y0, x1, y1, color, thickness=1, alpha=255):
        """Straight line. Axis-aligned cases become fills; the rest steps."""
        x0, y0, x1, y1 = int(x0), int(y0), int(x1), int(y1)
        t = max(1, int(thickness))
        if y0 == y1:
            self.fill_rect(min(x0, x1), y0, max(x0, x1) + 1, y0 + t,
                           color, alpha)
            return
        if x0 == x1:
            self.fill_rect(x0, min(y0, y1), x0 + t, max(y0, y1) + 1,
                           color, alpha)
            return
        dx, dy = abs(x1 - x0), abs(y1 - y0)
        sx = 1 if x0 < x1 else -1
        sy = 1 if y0 < y1 else -1
        err = dx - dy
        while True:
            self.fill_rect(x0, y0, x0 + t, y0 + t, color, alpha)
            if x0 == x1 and y0 == y1:
                break
            e2 = err * 2
            if e2 > -dy:
                err -= dy
                x0 += sx
            if e2 < dx:
                err += dx
                y0 += sy

    # -- coverage compositing --------------------------------------------

    def blit_coverage(self, cov, cw, ch, x, y, color):
        """Composite an 8-bit coverage bitmap in a solid colour."""
        if cw <= 0 or ch <= 0:
            return
        cx0, cy0, cx1, cy1 = self.clip
        x, y = int(x), int(y)
        sx0 = max(0, cx0 - x)
        sy0 = max(0, cy0 - y)
        sx1 = min(cw, cx1 - x)
        sy1 = min(ch, cy1 - y)
        if sx0 >= sx1 or sy0 >= sy1:
            return
        px = self.pixels
        r, g, b = color
        stride = self.stride
        for row in range(sy0, sy1):
            src = row * cw
            dst = (y + row) * stride + (x + sx0) * 3
            for col in range(sx0, sx1):
                a = cov[src + col]
                if a:
                    if a >= 255:
                        px[dst] = r
                        px[dst + 1] = g
                        px[dst + 2] = b
                    else:
                        inv = 255 - a
                        px[dst] = (px[dst] * inv + r * a) // 255
                        px[dst + 1] = (px[dst + 1] * inv + g * a) // 255
                        px[dst + 2] = (px[dst + 2] * inv + b * a) // 255
                dst += 3

    def blit_rgba(self, data, iw, ih, x, y, opaque=False):
        """Composite raw RGBA bytes.

        `opaque` promises every alpha byte is 255, which lets a row be
        converted to RGB by deleting its alpha bytes and copied in one
        assignment -- the difference between a photo costing microseconds
        and costing milliseconds.
        """
        cx0, cy0, cx1, cy1 = self.clip
        x, y = int(x), int(y)
        sx0 = max(0, cx0 - x)
        sy0 = max(0, cy0 - y)
        sx1 = min(iw, cx1 - x)
        sy1 = min(ih, cy1 - y)
        if sx0 >= sx1 or sy0 >= sy1:
            return
        px = self.pixels
        if opaque:
            count = sx1 - sx0
            for row in range(sy0, sy1):
                src = (row * iw + sx0) * 4
                line = bytearray(data[src:src + count * 4])
                del line[3::4]
                dst = (y + row) * self.stride + (x + sx0) * 3
                px[dst:dst + count * 3] = line
            return
        for row in range(sy0, sy1):
            src = (row * iw + sx0) * 4
            dst = (y + row) * self.stride + (x + sx0) * 3
            for _ in range(sx1 - sx0):
                a = data[src + 3]
                if a:
                    if a >= 255:
                        px[dst] = data[src]
                        px[dst + 1] = data[src + 1]
                        px[dst + 2] = data[src + 2]
                    else:
                        inv = 255 - a
                        px[dst] = (px[dst] * inv + data[src] * a) // 255
                        px[dst + 1] = (px[dst + 1] * inv + data[src + 1] * a) // 255
                        px[dst + 2] = (px[dst + 2] * inv + data[src + 2] * a) // 255
                src += 4
                dst += 3

    # -- output ----------------------------------------------------------

    def to_png(self):
        """Encode as PNG bytes. zlib is standard library, so this is cheap."""
        raw = bytearray()
        stride = self.stride
        for y in range(self.height):
            raw.append(0)  # filter type 0 (None)
            o = y * stride
            raw += self.pixels[o:o + stride]
        return _png_chunks(self.width, self.height, bytes(raw))

    def save_png(self, path):
        with open(path, "wb") as f:
            f.write(self.to_png())


def _png_chunks(width, height, raw):
    def chunk(tag, payload):
        body = tag + payload
        return (struct.pack(">I", len(payload)) + body
                + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF))

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", header)
            + chunk(b"IDAT", zlib.compress(raw, 6))
            + chunk(b"IEND", b""))


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
