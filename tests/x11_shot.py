"""Photograph a real X11 window and write it to a PNG.

`browser.py --screenshot` renders to a file without opening anything, which
proves the renderer works and says nothing at all about the window backend.
This does the opposite: it opens a genuine window on a genuine server, loads
a page into it, and then asks the *server* what is on that window. Every step
between the framebuffer and the screen -- the pixel conversion, the stride,
the XImage, XPutImage -- has to be right for the picture to come out, so a
plausible-looking PNG here is the evidence that Linux works.

Meant for CI under `xvfb-run`, where the result is uploaded as an artifact
and can be looked at by a human afterwards.

    python tests/x11_shot.py [out.png] [url]
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from feetbrowser import browser as browsermod
from feetbrowser import gui, raster, x11

import test_x11 as helpers   # noqa: E402 - the same live-server plumbing

PAGE = """<!doctype html>
<html><head><title>FeetBrowser on X11</title><style>
body { font: 16px sans-serif; margin: 0; background: #f4f6f8; color: #1c2430; }
header { background: #2f6fb0; color: #fff; padding: 24px 32px; }
h1 { margin: 0 0 6px; font-size: 30px; }
main { padding: 24px 32px; }
.row { display: block; margin: 0 0 18px; }
.swatch { display: inline-block; width: 60px; height: 60px;
          border: 2px solid #1c2430; }
table { border-collapse: collapse; margin-top: 16px; }
td, th { border: 1px solid #9aa7b4; padding: 6px 14px; text-align: left; }
th { background: #dde5ec; }
</style></head><body>
<header><h1>FeetBrowser on X11</h1>
<p>Rendered by our own rasteriser, put on the window with XPutImage, and
read back off the X server with XGetImage.</p></header>
<main>
<p class="row">
<span class="swatch" style="background:#d92b2b"></span>
<span class="swatch" style="background:#2bd94f"></span>
<span class="swatch" style="background:#2b6bd9"></span>
<span class="swatch" style="background:#111111"></span>
<span class="swatch" style="background:#ffffff"></span>
</p>
<p>If the three colour patches read red, green and blue in that order, the
visual's channel masks and the server's byte order were both handled the way
this server wants them.</p>
<table>
<tr><th>Piece</th><th>What it proves</th></tr>
<tr><td>Window</td><td>XCreateSimpleWindow and XMapWindow</td></tr>
<tr><td>Text</td><td>the font engine, drawn into our own surface</td></tr>
<tr><td>Colour</td><td>the visual masks and XImageByteOrder</td></tr>
<tr><td>Edges</td><td>the scanline padding on an odd width</td></tr>
</table>
</main></body></html>
"""


def capture(window):
    """The window's pixels as a Surface, straight off the X server.

    Decoded through the visual's masks rather than the backend's own byte
    offsets, so this produces a correct PNG at any TrueColor depth -- and so
    a picture that comes out wrong is evidence about the backend rather than
    about the two of them agreeing on the same mistake.
    """
    raw, line = helpers.grab(window)
    fmt = x11._state["format"]
    size = fmt.bits_per_pixel // 8
    order = "little" if fmt.byte_order == x11.LSB_FIRST else "big"
    masks = (fmt.red_mask, fmt.green_mask, fmt.blue_mask)
    shot = raster.Surface(window.width, window.height)
    out = shot.pixels
    for y in range(window.height):
        at = y * line
        dst = y * shot.stride
        for _ in range(window.width):
            value = int.from_bytes(raw[at:at + size], order)
            for channel, mask in enumerate(masks):
                out[dst + channel] = helpers._channel(value, mask)
            at += size
            dst += 3
    return shot


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else "x11-window.png"
    url = sys.argv[2] if len(sys.argv) > 2 else None
    if gui.backend() != "raster":
        sys.exit("x11_shot needs the raster backend (FEETBROWSER_BACKEND)")
    if not x11.available():
        sys.exit("no X11 display: %s" % (x11.unavailable_reason() or "?"))

    folder = os.path.dirname(os.path.abspath(path))
    if folder:
        os.makedirs(folder, exist_ok=True)
    if url is None:
        page = os.path.join(folder or ".", "x11-shot-page.html")
        with open(page, "w") as f:
            f.write(PAGE)
        url = "file://" + page

    window = x11.X11Tk(width=1000, height=720, title="FeetBrowser on X11")
    if not helpers.wait_ready(window):
        sys.exit("the window never reached the screen")
    browser = browsermod.Browser(window)
    # An odd width on purpose: a scanline-padding mistake is invisible at 1000
    # pixels and shears the whole picture at 1003.
    window.geometry("1003x701")
    browser.canvas.resize(1003, 701)
    browser._apply_resize()
    helpers.wait_geometry(window, (1003, 701))
    browser.new_tab(url)
    # The page finishes arriving on the timer queue, so give it a few turns
    # of the real loop rather than screenshotting a half-drawn frame.
    for _ in range(200):
        window.flush_timers()
        helpers.pump(window, 1)
        browser.draw()
        window.present()
    shot = capture(window)
    shot.save_png(path)
    window.destroy()
    size = os.path.getsize(path)
    print("wrote %s (%dx%d, %d bytes)" % (path, shot.width, shot.height, size))
    # A window that never got painted still writes a perfectly valid PNG, and
    # a flat one compresses to almost nothing -- so the size is the check.
    if size < 5000:
        sys.exit("the screenshot is suspiciously small; nothing was drawn")


if __name__ == "__main__":
    main()
