"""Draw FeetBrowser.app's icon with FeetBrowser's own rasteriser.

There is no artwork in this repository, and importing an image library to
make some would contradict the whole point of the project -- so the icon is
drawn by the same code that draws every page: `raster.rasterize` turns
polygons into an 8-bit coverage bitmap with real anti-aliasing, and
`Surface.blit_coverage` composites that coverage in a solid colour. The
shapes below are ordinary polygons; circles and the rounded corners are
subdivided into enough segments that the rasteriser's analytic horizontal
coverage does the smoothing.

Two things a Surface cannot do, and how they are handled:

  * It is RGB. macOS icons need an alpha channel or they render as opaque
    squares among a Dock full of rounded ones. The rounded-square coverage
    bitmap *is* the alpha channel, so `_png` below writes the surface's RGB
    and that coverage out together. That is a zlib call and a CRC, not an
    image library.
  * It has no gradient primitive. A gradient is a row of `fill_rect` calls,
    which is what the loop in `_backdrop` is.

Every size in the iconset is drawn from scratch at its own resolution rather
than resampled down from one big one, because the 16pt icon is four pixels of
toe and survives being drawn far better than being averaged.

    python packaging/macos/icon.py out.iconset
"""
import math
import os
import struct
import sys
import zlib

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from feetbrowser import raster  # noqa: E402

# The blue the project's own test page paints its header with, over the dark
# ink the same page uses for text. Keeping the icon in the browser's palette
# is free and means the app looks like the thing it launches.
TOP = (0x3C, 0x86, 0xCE)
BOTTOM = (0x1D, 0x3F, 0x66)
FOOT = (0xFF, 0xFF, 0xFF)
SHADOW = (0x14, 0x2C, 0x49)

# The sizes `iconutil` wants, as (pixels, filename).
ICONSET = [
    (16, "icon_16x16.png"), (32, "icon_16x16@2x.png"),
    (32, "icon_32x32.png"), (64, "icon_32x32@2x.png"),
    (128, "icon_128x128.png"), (256, "icon_128x128@2x.png"),
    (256, "icon_256x256.png"), (512, "icon_256x256@2x.png"),
    (512, "icon_512x512.png"), (1024, "icon_512x512@2x.png"),
]


def _ellipse(cx, cy, rx, ry, tilt=0.0, steps=64):
    """An ellipse as a polygon, tilted by `tilt` radians."""
    cos, sin = math.cos(tilt), math.sin(tilt)
    points = []
    for i in range(steps):
        a = 2 * math.pi * i / steps
        x, y = rx * math.cos(a), ry * math.sin(a)
        points.append((cx + x * cos - y * sin, cy + x * sin + y * cos))
    return points


def _rounded_square(size, radius, steps=24):
    """A rounded square covering the whole icon, corners first."""
    s, r = float(size), float(radius)
    corners = [(r, r, math.pi, 1.5 * math.pi), (s - r, r, 1.5 * math.pi, 2 * math.pi),
               (s - r, s - r, 0.0, 0.5 * math.pi), (r, s - r, 0.5 * math.pi, math.pi)]
    points = []
    for cx, cy, start, end in corners:
        for i in range(steps + 1):
            a = start + (end - start) * i / steps
            points.append((cx + r * math.cos(a), cy + r * math.sin(a)))
    return points


def _backdrop(surface, size):
    """A vertical gradient, one `fill_rect` per row."""
    for y in range(size):
        t = y / max(size - 1, 1)
        colour = tuple(int(round(TOP[i] + (BOTTOM[i] - TOP[i]) * t))
                       for i in range(3))
        surface.fill_rect(0, y, size, y + 1, colour)


# A right foot seen from underneath, as fractions of the icon's side.
#
# The sole is a disc swept down a spine: a chain of circles whose centres
# walk from the ball to the heel and whose radii follow the shape of a foot,
# wide across the ball, pinched at the arch, round again at the heel. Three
# big overlapping ellipses would be the obvious construction and it reads as
# three blobs in a row -- the union is seamless, but every place two of them
# meet is a corner in the silhouette, and a corner is exactly what a foot
# does not have. A swept disc has no corners anywhere: the ends are the end
# circles, and the sides are smooth as long as the radius is, which is what
# `_smooth` is for.
_SPINE = [(0.470, 0.170), (0.535, 0.166), (0.610, 0.140),
          (0.675, 0.124), (0.733, 0.128)]   # (centre y, radius)
_SOLE_LEFT, _SOLE_RIGHT = 0.504, 0.566   # centre line, ball end to heel

# How much of the icon's side the whole footprint spans. `_fit` scales the
# shape to this and centres it, so the numbers above only have to describe a
# foot and never also have to add up to a composition.
_EXTENT = 0.70

# (centre x, centre y, radius). Big toe first, each smaller than the last and
# set a little further round -- the fall-away is what stops five circles
# reading as a paw print. They are laid on an arc clear of the ball rather
# than overlapping it: a footprint is toes *and* a sole, and once they merge
# into one shape the toes stop being toes and become a bumpy edge.
_TOES = [(0.308, 0.280, 0.075), (0.429, 0.225, 0.058),
         (0.539, 0.224, 0.050), (0.629, 0.263, 0.044),
         (0.690, 0.325, 0.038)]


def _smooth(control, samples, passes=90):
    """`control` resampled to `samples` points and rounded off.

    Interpolating linearly between the control points gives a polyline with a
    kink at every one of them, and a kink in the radius is a crease in the
    silhouette. Repeatedly averaging each sample with its neighbours turns
    those kinks into curvature -- all the smoothing this shape needs, and a
    good deal less code than a spline.
    """
    columns = list(zip(*control))
    out = []
    for values in columns:
        row = []
        for i in range(samples):
            at = i * (len(values) - 1) / (samples - 1)
            low = min(int(at), len(values) - 2)
            row.append(values[low] + (values[low + 1] - values[low]) * (at - low))
        for _ in range(passes):
            row = [row[0]] + [(row[i - 1] + 2 * row[i] + row[i + 1]) / 4
                              for i in range(1, len(row) - 1)] + [row[-1]]
        out.append(row)
    return list(zip(*out))


def _sole(size, steps=56):
    """The sole, as a chain of overlapping circles down the spine."""
    u = float(size)
    circles = []
    spine = _smooth(_SPINE, steps)
    for i, (y, r) in enumerate(spine):
        t = i / (steps - 1)
        cx = _SOLE_LEFT + (_SOLE_RIGHT - _SOLE_LEFT) * t
        circles.append(_ellipse(cx * u, y * u, r * u, r * u, steps=48))
    return circles


def _fit(polys, size, extent=_EXTENT):
    """Scale and centre `polys` so the footprint spans `extent` of the icon."""
    xs = [x for poly in polys for x, _ in poly]
    ys = [y for poly in polys for _, y in poly]
    span = max(max(xs) - min(xs), max(ys) - min(ys)) or 1.0
    scale = extent * size / span
    dx = (size - (max(xs) + min(xs)) * scale) / 2
    dy = (size - (max(ys) + min(ys)) * scale) / 2
    return [[(x * scale + dx, y * scale + dy) for x, y in poly]
            for poly in polys]


def _foot(size):
    """The sole and five toes as polygons, at `size` pixels.

    Nonzero winding makes the overlapping circles of the sole a union with no
    seam, so the whole footprint is one silhouette however many discs went
    into it.
    """
    u = float(size)
    polys = _sole(size) + [_ellipse(cx * u, cy * u, r * u, r * u * 0.94)
                           for cx, cy, r in _TOES]
    return _fit(polys, size)


def draw(size):
    """One icon at `size` pixels: (surface, alpha bytes)."""
    surface = raster.Surface(size, size)
    _backdrop(surface, size)
    # One `rasterize` call for the whole foot, not one per part: nonzero
    # winding unions the overlapping ellipses in the coverage bitmap, so the
    # seams never exist to be shaded. Compositing them separately would draw
    # each one's anti-aliased edge over the last and leave visible joins.
    surface.blit_coverage(raster.rasterize(_foot(size), size, size), size, size,
                          0, 0, FOOT)
    alpha = raster.rasterize([_rounded_square(size, size * 0.225)], size, size)
    return surface, alpha


def _chunk(kind, payload):
    body = kind + payload
    return struct.pack(">I", len(payload)) + body + \
        struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)


def _png(path, surface, alpha):
    """Write the surface as RGBA, taking the alpha channel from `alpha`.

    `Surface.save_png` exists and is what the browser uses, but it writes RGB
    -- and an icon without alpha is a square in the Dock. This is the same
    file format with a fourth channel: filter byte 0 per row, one deflate
    stream, three chunks.
    """
    width, height, stride = surface.width, surface.height, surface.stride
    pixels = surface.pixels
    rows = bytearray()
    for y in range(height):
        rows.append(0)
        row = y * stride
        at = y * width
        for x in range(width):
            src = row + x * 3
            rows += pixels[src:src + 3]
            rows.append(alpha[at + x])
    data = _chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
    data += _chunk(b"IDAT", zlib.compress(bytes(rows), 9))
    data += _chunk(b"IEND", b"")
    with open(path, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n" + data)


def main():
    out = sys.argv[1] if len(sys.argv) > 1 else "FeetBrowser.iconset"
    os.makedirs(out, exist_ok=True)
    drawn = {}
    for size, name in ICONSET:
        if size not in drawn:
            drawn[size] = draw(size)
        _png(os.path.join(out, name), *drawn[size])
    print("wrote %d PNGs to %s" % (len(ICONSET), out))


if __name__ == "__main__":
    main()
