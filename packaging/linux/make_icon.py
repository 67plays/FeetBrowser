"""Draw the application icon with the browser's own rasteriser.

The repository ships no artwork, and importing an image library to make some
would be the one dependency this project does not take. It does not need one:
`raster.rasterize` turns polygons into an anti-aliased coverage bitmap and
`Surface.blit_coverage` composites that bitmap in a solid colour, which is
every tool a flat two-colour mark requires. The same code path draws the
letterforms on every page the browser renders.

The mark is a footprint -- sole, arch and five toes -- on a dark ground. It is
built from ellipses and one quadratic-bezier-sided arch, all in one polygon
list, because the rasteriser fills by the nonzero winding rule and clamps
coverage at 255: overlapping shapes wound the same way union cleanly instead
of cancelling, so a foot is a heel plus a ball plus an arch plus five circles
with no path arithmetic at all.

Coordinates are written for a 256x256 grid and scaled, so every size is drawn
rather than resampled and the small ones stay crisp.

    python3 packaging/linux/make_icon.py OUTDIR [size ...]
"""
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from feetbrowser import raster  # noqa: E402

GRID = 256.0

# The ground, top to bottom. Two stops rather than a flat fill: at 48 pixels
# in a menu the gradient is what stops the tile reading as a hole.
TOP = (0x14, 0x22, 0x38)
BOTTOM = (0x24, 0x47, 0x6B)
FOOT = (0xF5, 0xE6, 0xD3)
SHADOW = (0x0D, 0x18, 0x28)

# (cx, cy, rx, ry) on the 256 grid: the ball of the foot, the heel, and the
# five toes. The toes get smaller and step further back, which is the whole
# of what makes a row of circles read as a foot.
ELLIPSES = [
    (125, 120, 48, 34),     # ball
    (127, 190, 30, 27),     # heel
    (88, 66, 18, 18),       # big toe
    (120, 55, 13, 13),
    (146, 55, 12, 12),
    (166, 62, 10.5, 10.5),
    (181, 75, 9, 9),
]

# The arch: left edge from the ball down to the heel, curving inwards, and the
# right edge curving out. (start, control, end) for each side.
ARCH_LEFT = ((78, 122), (97, 154), (100, 179))
ARCH_RIGHT = ((172, 122), (178, 150), (155, 179))


def ellipse(cx, cy, rx, ry, steps=72):
    """A closed polygon approximating an ellipse, wound clockwise."""
    return [(cx + rx * math.cos(2 * math.pi * i / steps),
             cy + ry * math.sin(2 * math.pi * i / steps))
            for i in range(steps)]


def bezier(p0, p1, p2, steps=24):
    """Points along a quadratic bezier, endpoints included."""
    out = []
    for i in range(steps + 1):
        t = i / steps
        u = 1 - t
        out.append((u * u * p0[0] + 2 * u * t * p1[0] + t * t * p2[0],
                    u * u * p0[1] + 2 * u * t * p1[1] + t * t * p2[1]))
    return out


def foot_polygons(scale):
    """Every polygon in the mark, scaled to the requested size."""
    polys = [ellipse(*e) for e in ELLIPSES]
    # Wound the same way round as the ellipses. The rasteriser fills by the
    # nonzero rule, so an arch traced the other way subtracts itself from the
    # ball it overlaps instead of joining it.
    arch = bezier(*ARCH_RIGHT) + list(reversed(bezier(*ARCH_LEFT)))
    polys.append(arch)
    return [[(x * scale, y * scale) for x, y in poly] for poly in polys]


def draw(size):
    """The icon at `size` x `size`, as a Surface."""
    surface = raster.Surface(size, size)
    for y in range(size):
        t = y / max(size - 1, 1)
        row = tuple(int(round(TOP[c] + (BOTTOM[c] - TOP[c]) * t))
                    for c in range(3))
        surface.fill_rect(0, y, size, y + 1, row)
    scale = size / GRID
    polys = foot_polygons(scale)
    # A soft drop shadow: the same mark, offset, in the darkest ground colour.
    # Drawn first so the foot lands on top of it.
    drop = max(1, int(round(size / 64.0)))
    shifted = [[(x + drop, y + drop) for x, y in poly] for poly in polys]
    surface.blit_coverage(raster.rasterize(shifted, size, size),
                          size, size, 0, 0, SHADOW)
    surface.blit_coverage(raster.rasterize(polys, size, size),
                          size, size, 0, 0, FOOT)
    return surface


def main():
    if len(sys.argv) < 2:
        sys.exit("usage: make_icon.py OUTDIR [size ...]")
    outdir = sys.argv[1]
    sizes = [int(a) for a in sys.argv[2:]] or [256, 128, 64, 48]
    os.makedirs(outdir, exist_ok=True)
    for size in sizes:
        path = os.path.join(outdir, "feetbrowser-%d.png" % size)
        draw(size).save_png(path)
        print("wrote %s (%d bytes)" % (path, os.path.getsize(path)))


if __name__ == "__main__":
    main()
