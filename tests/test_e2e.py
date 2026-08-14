"""End-to-end test: load a page from a local server and read the pixels back.

Everything else in the suite checks one layer. This checks the join between
all of them, the way a person looking at the screen would: a page is fetched
over a real socket, parsed, styled, laid out, rasterised and written to a
PNG, and then that PNG is decoded again and the colours in it are counted.

It exists because the suite had no such check and a regression walked
straight through the gap. `<img>` stopped drawing anything at all -- every
image on every page silently replaced by its alt-text placeholder -- and
nothing anywhere went red, because the only end-to-end assertion was that a
screenshot of `about:blank` came to more than 2000 bytes, which a blank white
rectangle satisfies comfortably.

So the fixture page carries a colour of its own for each thing that has to
survive the trip: a background, a border, glyphs, a PNG and a GIF, in shades
picked so that finding one pixel of the right colour is proof that the layer
that draws it ran. An image that quietly disappears takes its colour with
it, and this test says which one and where it should have been.
"""
import collections
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from feetbrowser import browser as browsermod
from feetbrowser import imagecodec
from feetbrowser.layout import DrawText

from fixture_server import FixtureServer


# The fixture's palette. Every value is deliberately odd so it cannot be
# confused with the browser chrome, the default stylesheet or a rounding
# error, and none of them is a shade the antialiaser produces on its own.
PAGE_BG = (0x12, 0x34, 0x56)
TEXT = (0xFF, 0xEE, 0x00)
BORDER = (0xAB, 0xCD, 0xEF)
PNG_BODY, PNG_MARK = (255, 0, 128), (0, 255, 255)
GIF_BODY, GIF_MARK = (20, 200, 100), (255, 150, 0)
PNG_SIZE, GIF_SIZE = (120, 80), (90, 60)
PNG_MARK_SIZE, GIF_MARK_SIZE = 24, 18


def _decode(path):
    with open(path, "rb") as fh:
        return imagecodec.decode_png(fh.read())


def _boxes(width, height, rgba):
    """Every colour in the shot, mapped to its pixel count and its bounding
    box. One pass, because a 1000x720 shot is 720,000 pixels and doing it
    once per colour of interest is the difference between a test and a wait.
    """
    count = collections.Counter()
    box = {}
    for i in range(width * height):
        colour = (rgba[i * 4], rgba[i * 4 + 1], rgba[i * 4 + 2])
        count[colour] += 1
        x, y = i % width, i // width
        seen = box.get(colour)
        if seen is None:
            box[colour] = [x, y, x, y]
        else:
            if x < seen[0]:
                seen[0] = x
            if x > seen[2]:
                seen[2] = x
            if y > seen[3]:
                seen[3] = y
    return count, box


def _report(count, drawn):
    """What to print when an assertion fails: the twenty commonest colours
    and the text that was drawn, which together say what the renderer did
    instead of what it was asked to."""
    lines = ["  colours in the shot (top 20):"]
    for colour, n in count.most_common(20):
        lines.append("    #%02x%02x%02x  %d" % (colour + (n,)))
    lines.append("  text drawn: %r" % (drawn,))
    return "\n".join(lines)


def _within(inner, outer, slack=0):
    return (inner[0] >= outer[0] - slack and inner[1] >= outer[1] - slack
            and inner[2] <= outer[2] + slack and inner[3] <= outer[3] + slack)


def test_page_renders_every_layer():
    with FixtureServer() as fixtures, tempfile.TemporaryDirectory() as folder:
        shot = os.path.join(folder, "e2e-shot.png")
        browser = browsermod.screenshot(fixtures.url("pixels.html"), shot)
        width, height, rgba = _decode(shot)

    drawn = [c.text for c in browser.tabs[0].display_list
             if isinstance(c, DrawText) and c.text]
    count, box = _boxes(width, height, rgba)
    detail = _report(count, drawn)

    # The alt-text placeholder is what `<img>` falls back to, so seeing it is
    # the regression itself rather than a symptom of one. Checked first: it
    # names the image that failed, which no amount of pixel counting can.
    placeholders = [t for t in drawn if "[img" in t]
    assert not placeholders, (
        "images fell back to their alt-text placeholder: %s\n%s"
        % (placeholders, detail))

    # The page background. The chrome is above it, so it must not start at
    # the top of the shot -- a page painted over the toolbar is a bug too.
    assert count[PAGE_BG] > width * 50, (
        "the page background barely got painted\n%s" % detail)
    page = box[PAGE_BG]
    assert page[1] > 0, "the page was painted over the browser chrome"

    # Glyphs. Coverage is antialiased, so only the middles of the strokes
    # land on the exact colour; a few hundred of those is a word, and zero
    # is a font engine that produced nothing.
    assert count[TEXT] > 300, (
        "almost no text was drawn (%d pixels of #ffee00)\n%s"
        % (count[TEXT], detail))
    text = box[TEXT]
    assert text[2] - text[0] > 60, (
        "the heading is too narrow to be four glyphs\n%s" % detail)

    # The border, which must be a frame and not a filled box: the middle of
    # its bounding box belongs to whatever it surrounds.
    assert BORDER in box, "the border was not drawn\n%s" % detail
    frame = box[BORDER]
    assert frame[2] - frame[0] > 400, (
        "the border does not span the page\n%s" % detail)
    mid = ((frame[1] + frame[3]) // 2) * width + (frame[0] + frame[2]) // 2
    assert tuple(rgba[mid * 4:mid * 4 + 3]) != BORDER, (
        "the border filled its box instead of outlining it\n%s" % detail)

    # The two images. Each is a solid field with a differently coloured
    # square in its top-left corner, so the body proves the decode and the
    # blit, and the corner proves the rows went down the way they came in.
    for name, body, mark, (iw, ih), marked in (
            ("swatch.png", PNG_BODY, PNG_MARK, PNG_SIZE, PNG_MARK_SIZE),
            ("dot.gif", GIF_BODY, GIF_MARK, GIF_SIZE, GIF_MARK_SIZE)):
        assert body in box, (
            "%s never reached the screen: no #%02x%02x%02x anywhere\n%s"
            % ((name,) + body + (detail,)))
        assert mark in box, (
            "%s lost its corner marker\n%s" % (name, detail))
        whole = [min(box[body][0], box[mark][0]),
                 min(box[body][1], box[mark][1]),
                 max(box[body][2], box[mark][2]),
                 max(box[body][3], box[mark][3])]
        assert (whole[2] - whole[0] + 1, whole[3] - whole[1] + 1) == (iw, ih), (
            "%s was drawn %dx%d, not %dx%d\n%s"
            % (name, whole[2] - whole[0] + 1, whole[3] - whole[1] + 1,
               iw, ih, detail))
        assert count[body] + count[mark] >= iw * ih * 0.9, (
            "%s is full of holes: %d of %d pixels\n%s"
            % (name, count[body] + count[mark], iw * ih, detail))
        assert count[mark] >= marked * marked * 0.9, (
            "%s: the corner marker is the wrong size\n%s" % (name, detail))
        assert box[mark][0] == whole[0] and box[mark][1] == whole[1], (
            "%s came out mirrored or upside down\n%s" % (name, detail))
        assert _within(whole, frame), (
            "%s landed outside the bordered box\n%s" % (name, detail))

    png, gif = box[PNG_BODY], box[GIF_BODY]
    assert png[2] < gif[0], (
        "the two images are not side by side in source order\n%s" % detail)
    assert text[3] < frame[1], (
        "the heading did not end up above the bordered box\n%s" % detail)

    print("  page %dx%d, background %d px, glyphs %d px, border %d px, "
          "swatch.png %d px, dot.gif %d px"
          % (width, height, count[PAGE_BG], count[TEXT], count[BORDER],
             count[PNG_BODY] + count[PNG_MARK],
             count[GIF_BODY] + count[GIF_MARK]))


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  ok  {t.__name__}")
        except Exception as e:
            failed += 1
            import traceback
            traceback.print_exc()
            print(f" FAIL {t.__name__}: {e}")
    if failed:
        print(f"\n{failed} FAILED")
        sys.exit(1)
    print(f"\nALL {len(tests)} END-TO-END TESTS PASSED")


if __name__ == "__main__":
    main()
