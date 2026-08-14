"""Check that a --screenshot run produced a real image.

The end-to-end check in CI: the browser has to start, lay a page out, draw it
and write a PNG. A file that exists but is a few hundred bytes is what a
window full of nothing looks like, so this reads the header for the size and
insists on some weight behind it.

Usage: check_screenshot.py <path>
"""
import os
import struct
import sys


def check(path):
    with open(path, "rb") as f:
        header = f.read(24)
    if header[:8] != b"\x89PNG\r\n\x1a\n":
        raise SystemExit("%s is not a PNG" % path)
    width, height = struct.unpack(">II", header[16:24])
    size = os.path.getsize(path)
    if width < 200 or height < 200:
        raise SystemExit("%s is only %dx%d" % (path, width, height))
    if size < 2000:
        raise SystemExit("%s is suspiciously small: %d bytes" % (path, size))
    print("rendered %s (%dx%d, %d bytes)" % (path, width, height, size))


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    check(sys.argv[1])
