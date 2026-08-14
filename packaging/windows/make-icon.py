#!/usr/bin/env python3
"""Draw launcher/resources/FeetBrowser.ico.

The icon is a binary file that has to be in the repository -- rc.exe wants a
real .ico at link time, and a build that fetched its own icon from somewhere
would be a build that could not run offline. This is the thing that made it,
so the blob is reviewable: change a number here, re-run, and the diff on the
.ico is explained.

Pure standard library on purpose. It is the same rule the browser lives by,
and the same reason the icon is not a PNG someone exported from a drawing
program: nothing outside CPython is needed to reproduce it.

    python3 packaging/windows/make-icon.py

Windows Vista and later read PNG-compressed icon entries at every size, and
the bundle needs Windows 10 anyway, so every entry here is a PNG.
"""

import os
import struct
import sys
import zlib

# One master render, downsampled to the sizes Explorer, the taskbar, the
# Alt-Tab switcher and the file properties dialog each ask for.
MASTER = 1024
SIZES = (16, 24, 32, 48, 64, 128, 256)

BACKDROP = (27, 42, 74)       # deep navy, so the mark reads on light and dark
FOOT = (245, 217, 184)        # a foot
SHADOW = (14, 22, 40)


def _ellipse(px, py, cx, cy, rx, ry):
    dx = (px - cx) / rx
    dy = (py - cy) / ry
    return dx * dx + dy * dy <= 1.0


def _rounded_square(px, py, radius):
    """Inside the unit square with `radius` corners, in unit coordinates."""
    if px < 0.0 or px > 1.0 or py < 0.0 or py > 1.0:
        return False
    qx = min(max(px, radius), 1.0 - radius)
    qy = min(max(py, radius), 1.0 - radius)
    dx, dy = px - qx, py - qy
    return dx * dx + dy * dy <= radius * radius


# The mark: a sole, the ball of the foot, and five toes on an arc above it.
# Unit coordinates, y down.
TOES = [
    (0.345, 0.300, 0.052, 0.058),
    (0.443, 0.257, 0.049, 0.055),
    (0.535, 0.253, 0.046, 0.052),
    (0.618, 0.272, 0.042, 0.047),
    (0.690, 0.305, 0.037, 0.042),
]


def _foot(px, py):
    if _ellipse(px, py, 0.500, 0.640, 0.150, 0.185):
        return True
    if _ellipse(px, py, 0.510, 0.440, 0.205, 0.145):
        return True
    for cx, cy, rx, ry in TOES:
        if _ellipse(px, py, cx, cy, rx, ry):
            return True
    return False


def render_master():
    """RGBA rows of the MASTER-sized image."""
    rows = []
    for y in range(MASTER):
        py = (y + 0.5) / MASTER
        row = bytearray()
        for x in range(MASTER):
            px = (x + 0.5) / MASTER
            if not _rounded_square(px, py, 0.22):
                row += b"\x00\x00\x00\x00"
                continue
            # A one-pixel-at-256 drop shadow under the mark, which is what
            # stops the foot dissolving into the backdrop at 16 pixels.
            if _foot(px, py):
                r, g, b = FOOT
            elif _foot(px - 0.012, py - 0.014):
                r, g, b = SHADOW
            else:
                r, g, b = BACKDROP
            row += bytes((r, g, b, 255))
        rows.append(bytes(row))
    return rows


def downsample(rows, size):
    """Box-filter the master down to `size`, premultiplying over alpha."""
    step = MASTER // size
    out = []
    for oy in range(size):
        row = bytearray()
        for ox in range(size):
            r = g = b = a = 0
            for sy in range(oy * step, (oy + 1) * step):
                src = rows[sy]
                base = ox * step * 4
                for sx in range(step):
                    off = base + sx * 4
                    alpha = src[off + 3]
                    r += src[off] * alpha
                    g += src[off + 1] * alpha
                    b += src[off + 2] * alpha
                    a += alpha
            if a:
                row += bytes((r // a, g // a, b // a, a // (step * step)))
            else:
                row += b"\x00\x00\x00\x00"
        out.append(bytes(row))
    return out


def _chunk(kind, payload):
    body = kind + payload
    return struct.pack(">I", len(payload)) + body + struct.pack(">I", zlib.crc32(body))


def png(rows, size):
    raw = b"".join(b"\x00" + row for row in rows)
    return (b"\x89PNG\r\n\x1a\n"
            + _chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
            + _chunk(b"IDAT", zlib.compress(raw, 9))
            + _chunk(b"IEND", b""))


def ico(images):
    """ICONDIR + one ICONDIRENTRY per image, then the PNG payloads."""
    header = struct.pack("<HHH", 0, 1, len(images))
    offset = len(header) + 16 * len(images)
    entries, payloads = b"", b""
    for size, data in images:
        entries += struct.pack(
            "<BBBBHHII",
            size if size < 256 else 0,   # 0 means 256 in an .ico
            size if size < 256 else 0,
            0, 0, 1, 32, len(data), offset)
        payloads += data
        offset += len(data)
    return header + entries + payloads


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    out = os.path.join(here, "launcher", "resources", "FeetBrowser.ico")
    sys.stderr.write("rendering %dx%d master...\n" % (MASTER, MASTER))
    master = render_master()
    images = []
    for size in SIZES:
        sys.stderr.write("  %dx%d\n" % (size, size))
        images.append((size, png(downsample(master, size), size)))
    blob = ico(images)
    with open(out, "wb") as f:
        f.write(blob)
    sys.stderr.write("wrote %s (%d bytes, %d sizes)\n"
                     % (out, len(blob), len(images)))


if __name__ == "__main__":
    main()
