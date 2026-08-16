"""Photograph a real X11 window and write it to a PNG.

`browser.py --screenshot` renders to a file without opening anything, which
proves the renderer works and says nothing at all about the window backend.
This does the opposite: it opens a genuine window on a genuine server, loads
a page into it, and then asks the *server* what is on that window. Every step
between the framebuffer and the screen -- the pixel conversion, the stride,
the XImage, XPutImage -- has to be right for the picture to come out, so a
plausible-looking PNG here is the evidence that Linux works.

doormat has a script of the same name that photographs a test card it fills
itself. This one photographs a page our rasteriser drew, which is the half
that repository cannot see: doormat proves the window carries pixels, and
this proves the pixels are a browser.

Meant for CI under `xvfb-run`, where the result is uploaded as an artifact
and can be looked at by a human afterwards.

    python tests/x11_shot.py [out.png] [url]
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from doormat import x11
from feetbrowser import browser as browsermod
from feetbrowser import raster

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


def channel(value, mask):
    """One channel of a packed pixel, scaled back up to 0..255."""
    shift = (mask & -mask).bit_length() - 1
    span = mask >> shift
    return ((value & mask) >> shift) * 255 // span


def capture(window):
    """The window's pixels as a Surface, straight off the X server.

    Decoded through the visual's masks rather than the backend's own byte
    offsets, so this produces a correct PNG at any TrueColor depth -- and so
    a picture that comes out wrong is evidence about the backend rather than
    about the two of them agreeing on the same mistake.

    The decoded rows are assembled as RGBA here and handed to the surface in
    one `blit_rgba`, because a Surface's framebuffer belongs to Rust and
    `pixels` is a read-only view of it: writing a photograph in is a drawing
    operation like any other, and every alpha byte is 255 so the blit is a
    strided copy.
    """
    raw, line = helpers.grab(window)
    fmt = x11._state["format"]
    size = fmt.bits_per_pixel // 8
    order = "little" if fmt.byte_order == x11.LSB_FIRST else "big"
    masks = (fmt.red_mask, fmt.green_mask, fmt.blue_mask)
    rgba = bytearray(b"\xff" * (window.width * window.height * 4))
    dst = 0
    for y in range(window.height):
        at = y * line
        for _ in range(window.width):
            value = int.from_bytes(raw[at:at + size], order)
            for index, mask in enumerate(masks):
                rgba[dst + index] = channel(value, mask)
            at += size
            dst += 4
    shot = raster.Surface(window.width, window.height)
    shot.blit_rgba(rgba, window.width, window.height, 0, 0, opaque=True)
    return shot


def check(shot):
    """Say what is wrong with the photograph, or nothing if it is right.

    The picture used to be uploaded and never looked at, which made it an
    artifact rather than a test: the job stayed green whatever came back off
    the server. The page above is built so that a handful of colours settle
    the question. Red, green and blue have to be there, as blocks big enough
    to be the swatches, and in that order across the window -- a visual's
    channel masks and the server's byte order are precisely what a wrong
    answer permutes, and permuting them is invisible in the file size.
    """
    swatches = ((0xD9, 0x2B, 0x2B), (0x2B, 0xD9, 0x4F), (0x2B, 0x6B, 0xD9))
    header = (0x2F, 0x6F, 0xB0)
    total = {colour: 0 for colour in swatches + (header,)}
    sum_x = dict(total)
    pixels = shot.pixels
    for y in range(shot.height):
        row = y * shot.stride
        for x in range(shot.width):
            at = row + x * 3
            colour = (pixels[at], pixels[at + 1], pixels[at + 2])
            if colour in total:
                total[colour] += 1
                sum_x[colour] += x
    if total[header] < 5000:
        return "the header block is missing (%d px of #2f6fb0)" % total[header]
    for colour in swatches:
        if total[colour] < 2000:
            return ("the #%02x%02x%02x swatch is missing (%d px)"
                    % (colour + (total[colour],)))
    middles = [sum_x[colour] / total[colour] for colour in swatches]
    if not middles[0] < middles[1] < middles[2]:
        return ("red, green and blue came out at x=%s, which is not the "
                "order they were drawn in"
                % [round(m) for m in middles])
    return None


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else "x11-window.png"
    url = sys.argv[2] if len(sys.argv) > 2 else None
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
    # a flat one compresses to almost nothing -- so the size is the floor.
    if size < 5000:
        sys.exit("the screenshot is suspiciously small; nothing was drawn")
    problem = check(shot)
    if problem:
        sys.exit("the window came out wrong: %s" % problem)
    print("red, green and blue arrived in order; the header block is there")


if __name__ == "__main__":
    main()
