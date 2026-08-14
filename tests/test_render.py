"""Offline tests for the rendering stack: fonts, raster, image codecs, canvas.

These cover the layers that replaced Tk. They need no display and reach
nothing outside this machine -- the few that need a page to arrive over HTTP
serve it from a loopback server they start themselves -- but they do need at
least one installed font, which every platform we support has.
"""
import os
import struct
import sys
import zlib

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from feetbrowser import canvas as canvasmod
from feetbrowser import fontengine, gui, imagecodec, raster
from feetbrowser.window import Event, Window


# -- helpers ---------------------------------------------------------------

def _png(width, height, depth, color, samples, palette=None, trns=None,
         interlace=0, idat=None):
    """Build a PNG so the decoder can be tested against known pixels.

    `idat` replaces the compressed pixel data outright, which is how the
    malformed-input tests hand the decoder something no encoder wrote."""
    def chunk(tag, payload):
        body = tag + payload
        return (struct.pack(">I", len(payload)) + body
                + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF))

    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[color]
    stride = (width * channels * depth + 7) // 8
    raw = bytearray()
    if idat is None:
        for y in range(height):
            raw.append(0)
            raw += samples[y * stride:(y + 1) * stride]
        idat = zlib.compress(bytes(raw))
    out = b"\x89PNG\r\n\x1a\n" + chunk(
        b"IHDR", struct.pack(">IIBBBBB", width, height, depth, color, 0, 0,
                             interlace))
    if palette:
        out += chunk(b"PLTE", palette)
    if trns:
        out += chunk(b"tRNS", trns)
    return out + chunk(b"IDAT", idat) + chunk(b"IEND", b"")


def _gif():
    """A 2x2 GIF whose LZW stream is four literal codes and an end marker."""
    palette = bytes([255, 0, 0, 0, 0, 255]) + bytes(6)
    stream, acc, bits = bytearray(), 0, 0
    for code in (0, 1, 1, 0, 5):     # 4-entry table: clear=4, end=5, 3 bits
        acc |= code << bits
        bits += 3
        while bits >= 8:
            stream.append(acc & 0xFF)
            acc >>= 8
            bits -= 8
    if bits:
        stream.append(acc & 0xFF)
    return (b"GIF89a" + struct.pack("<HHBBB", 2, 2, 0x80 | 0x01, 0, 0)
            + palette
            + b"\x2C" + struct.pack("<HHHHB", 0, 0, 2, 2, 0)
            + bytes([2, len(stream)]) + bytes(stream) + b"\x00" + b"\x3B")


def _pixel(surface, x, y):
    o = y * surface.stride + x * 3
    return tuple(surface.pixels[o:o + 3])


def _sans():
    return canvasmod.Font(family="Helvetica", size=16)


# -- font engine -----------------------------------------------------------

def test_font_index_finds_families():
    index = fontengine.index()
    assert index, "no fonts found on this system"
    for family, faces in index.items():
        assert isinstance(family, str) and family == family.lower()
        assert faces, f"{family} indexed with no faces"
        break


def test_font_metrics_are_sane():
    font = _sans()
    assert font.ascent > 0, "ascent must be positive"
    assert font.descent >= 0, "descent is reported as a positive depth"
    assert font.linespace >= font.ascent + font.descent
    assert font.linespace <= font.size * 3, "line height implausibly large"


def test_font_metrics_scale_with_size():
    small = canvasmod.Font(family="Helvetica", size=10)
    large = canvasmod.Font(family="Helvetica", size=30)
    assert large.linespace > small.linespace
    assert large.measure("Handgloves") > small.measure("Handgloves")


def test_measure_is_additive():
    """The layout engine caches per-character widths and sums them, so a
    string's width must equal the sum of its characters' widths exactly."""
    font = _sans()
    text = "Handgloves, 0123!"
    total = sum(font.measure(ch) for ch in text)
    assert abs(font.measure(text) - total) < 1e-9, "measure is not additive"


def test_measure_empty_and_space():
    font = _sans()
    assert font.measure("") == 0
    assert font.measure(" ") > 0, "a space must advance the pen"


def test_glyph_lookup_and_contours():
    font = _sans()
    face = font.face
    assert face.glyph_id("H") != 0, "no glyph for 'H'"
    assert face.glyph_contours(face.glyph_id(" ")) == [], \
        "a space should have no outline"
    contours = face.glyph_contours(face.glyph_id("H"))
    assert contours and len(contours[0]) >= 4, "'H' has no usable outline"


def test_flatten_produces_pixel_polygons():
    font = _sans()
    face = font.face
    polys = fontengine.flatten(face.glyph_contours(face.glyph_id("o")),
                               face.scale(64))
    assert len(polys) >= 2, "'o' should flatten to an outer and inner contour"
    ys = [p[1] for poly in polys for p in poly]
    assert min(ys) < 0, "flattened glyphs sit above the baseline (negative y)"


def test_bold_and_italic_select_different_faces():
    plain = canvasmod.Font(family="Helvetica", size=16)
    bold = canvasmod.Font(family="Helvetica", size=16, weight="bold")
    assert bold.bold and not plain.bold
    # Either a real bold face exists (different widths) or it fell back to the
    # regular one; both are acceptable, but the flags must be right.
    assert bold.measure("Handgloves") >= plain.measure("Handgloves") * 0.9


def test_missing_glyph_falls_back_to_another_face():
    """A face without a glyph must not paint .notdef boxes; measuring and
    painting both have to agree on whichever face supplies the character."""
    font = _sans()
    ch = "★"  # BLACK STAR - absent from many text faces
    face, gid, _scale = font.face_for(ch)
    if font.face.glyph_id(ch) == 0:
        assert gid != 0 or face is font.face, \
            "no fallback face was consulted"
    assert font.measure(ch) >= 0


# -- malformed fonts -------------------------------------------------------
#
# Fonts come off the local disk rather than off the network, so the threat is
# not a hostile author but a truncated download, a partially written file, or
# a face doing something the spec allows and nobody expects. The rule is the
# same either way: a file that is not a font raises FontError, and a font that
# is merely strange gives up on the glyph it cannot read and keeps going. It
# may never crash the parser, which since the parser is Rust would mean
# taking the process with it.

def _sfnt(tables):
    """Assemble an sfnt file from ``{tag: payload}``."""
    tags = sorted(tables)
    out = struct.pack(">IHHHH", 0x00010000, len(tags), 0, 0, 0)
    offset = 12 + 16 * len(tags)
    body = b""
    for tag in tags:
        payload = tables[tag]
        out += tag.encode("ascii").ljust(4)[:4]
        out += struct.pack(">III", 0, offset + len(body), len(payload))
        body += payload + b"\x00" * (-len(payload) % 4)
    return out + body


def _minimal(glyf=None, loca=None, cmap=None, n_glyphs=2, n_metrics=2,
             index_to_loc=0):
    head = bytearray(54)
    struct.pack_into(">H", head, 18, 1000)          # unitsPerEm
    struct.pack_into(">h", head, 50, index_to_loc)  # indexToLocFormat
    hhea = bytearray(36)
    struct.pack_into(">hhh", hhea, 4, 800, -200, 0)
    struct.pack_into(">H", hhea, 34, n_metrics)
    maxp = bytearray(6)
    struct.pack_into(">H", maxp, 4, n_glyphs)
    hmtx = struct.pack(">HhHh", 500, 0, 300, 0)
    if glyf is None:
        # One square contour, points given as 16-bit deltas.
        glyf = (struct.pack(">hhhhh", 1, 0, 0, 100, 100)
                + struct.pack(">HH", 3, 0) + bytes([1, 1, 1, 1])
                + struct.pack(">hhhh", 0, 100, 0, -100)
                + struct.pack(">hhhh", 0, 0, 100, 0))
    if loca is None:
        loca = struct.pack(">HHH", 0, 0, len(glyf) // 2)
    if cmap is None:
        # Format 6, mapping 'A' to glyph 1.
        sub = struct.pack(">HHHHHH", 6, 12, 0, ord("A"), 1, 1)
        cmap = struct.pack(">HHHHI", 0, 1, 3, 1, 12) + sub
    return _sfnt({"head": bytes(head), "hhea": bytes(hhea),
                  "maxp": bytes(maxp), "hmtx": hmtx, "glyf": glyf,
                  "loca": loca, "cmap": cmap})


def _raises_fonterror(data, why):
    try:
        fontengine.Font(data)
    except fontengine.FontError:
        return
    raise AssertionError(why)


def test_minimal_font_parses():
    """The scaffolding above has to make a font, or the tests below prove
    nothing about the cases they break."""
    font = fontengine.Font(_minimal())
    assert (font.units_per_em, font.ascent, font.descent) == (1000, 800, -200)
    assert font.glyph_id("A") == 1 and font.glyph_id("B") == 0
    assert len(font.glyph_contours(1)) == 1
    assert font.advance(0) == 500 and font.advance(1) == 300


def test_font_rejects_files_that_are_not_fonts():
    _raises_fonterror(b"", "an empty file is not a font")
    _raises_fonterror(b"<html>not a font at all</html>", "HTML is not a font")
    _raises_fonterror(b"\x00\x01\x00\x00", "a bare sfnt tag is not a font")
    _raises_fonterror(_sfnt({"cmap": b"\x00" * 8}),
                      "a font without head cannot be measured")


def test_font_rejects_a_collection_index_it_does_not_have():
    _raises_fonterror(b"ttcf" + struct.pack(">IIII", 0x00010000, 1, 200, 0),
                      "a one-font collection has no second face")


def test_font_treats_tables_pointing_past_the_end_as_absent():
    """A truncated font leaves directory entries pointing into nothing. Those
    read as missing tables, which is how a partly written file still gives up
    politely instead of indexing off the end of the buffer."""
    data = bytearray(_minimal())
    # The directory is sorted by tag, so cmap is the first entry: aim its
    # offset a megabyte past the end of the file.
    struct.pack_into(">I", data, 12 + 8, 1 << 20)
    font = fontengine.Font(bytes(data))
    assert font.glyph_id("A") == 0, "a missing cmap maps nothing"
    assert font.names() == {}


def test_font_ignores_a_truncated_glyph():
    good = _minimal()
    for cut in (10, 12, 14, 16, 20, 24):
        glyf = (struct.pack(">hhhhh", 1, 0, 0, 100, 100)
                + struct.pack(">HH", 3, 0) + bytes([1, 1, 1, 1])
                + struct.pack(">hhhh", 0, 100, 0, -100)
                + struct.pack(">hhhh", 0, 0, 100, 0))[:cut]
        font = fontengine.Font(_minimal(glyf=glyf,
                                        loca=struct.pack(">HHH", 0, 0,
                                                         len(glyf) // 2)))
        assert font.glyph_contours(1) == [], f"cut at {cut} produced an outline"
    assert fontengine.Font(good).glyph_contours(1), "the whole glyph is fine"


def test_font_ignores_loca_entries_past_glyf():
    font = fontengine.Font(_minimal(loca=struct.pack(">HHH", 0, 0, 30000)))
    assert font.glyph_contours(1) == []
    assert font.glyph_contours(9999) == [], "a glyph id past loca is blank"


def test_font_stops_on_a_composite_that_refers_to_itself():
    """A composite pointing at itself would recurse for ever. The parser gives
    up after a few levels and returns nothing, which is the only answer that
    terminates."""
    glyf = (struct.pack(">hhhhh", -1, 0, 0, 100, 100)
            + struct.pack(">HH", 0x0002, 1) + bytes([0, 0]))
    font = fontengine.Font(_minimal(glyf=glyf,
                                    loca=struct.pack(">HHH", 0, 0,
                                                     len(glyf) // 2)))
    assert font.glyph_contours(1) == []


def test_font_cmap_ignores_impossible_ranges():
    """A format 12 group spanning the whole 32-bit space is a corrupt record,
    not four billion characters to enumerate."""
    groups = (struct.pack(">III", ord("A"), ord("A"), 1)
              + struct.pack(">III", 0, 0xFFFFFFFF, 1))
    sub = struct.pack(">HHIII", 12, 0, 16 + len(groups), 0, 2) + groups
    cmap = struct.pack(">HHHHI", 0, 1, 3, 10, 12) + sub
    font = fontengine.Font(_minimal(cmap=cmap))
    assert font.glyph_id("A") == 1
    assert font.glyph_id("B") == 0, "the impossible group should be skipped"


def test_font_advance_falls_back_to_the_last_metric():
    """hmtx stops after numberOfHMetrics entries and every glyph after that
    shares the last advance -- that is how the format spells a monospaced
    tail, not a reason to index past the table."""
    font = fontengine.Font(_minimal())
    assert font.advance(1) == 300
    assert font.advance(50000) == 300
    assert font.advance(-1) == 300


def test_font_survives_arbitrary_corruption():
    """Flip bytes through a real font and a hand-built one and insist the
    parser always either works or raises FontError. A font is read once and
    then asked for outlines all day, so a stray offset must be survivable at
    every one of those calls, not just at parse time."""
    import random

    seeds = [_minimal()]
    for faces in fontengine.index().values():
        path, _face = next(iter(faces.values()))
        with open(path, "rb") as f:
            seeds.append(f.read(200000))
        break
    rng = random.Random(20260814)
    for _ in range(1500):
        data = bytearray(rng.choice(seeds))
        for _flip in range(rng.randint(1, 8)):
            data[rng.randrange(len(data))] = rng.randrange(256)
        if rng.random() < 0.3:
            del data[rng.randrange(len(data)):]
        try:
            font = fontengine.Font(bytes(data))
            font.names()
            for ch in ("A", "g", "☃"):
                font.glyph_id(ch)
            for gid in (0, 1, 7, 4000):
                fontengine.flatten(font.glyph_contours(gid), font.scale(16))
                font.advance(gid)
        except fontengine.FontError:
            pass
        except Exception as exc:                 # noqa: BLE001
            raise AssertionError(
                f"{type(exc).__name__} escaped the font parser: {exc}")


def test_text_survives_lone_surrogates():
    """``&#xD800;`` puts a lone surrogate in a page's text. It is not a
    character any font maps, but measuring and drawing it must come back
    quietly rather than raise on the way into the renderer."""
    font = _sans()
    face = font.face
    assert face.glyph_id("\ud800") == 0
    assert face.has_char("\ud800") is False
    text = "a\ud800b"
    assert raster.measure_text(face, 16, text) >= 0
    s = raster.Surface(40, 20, (255, 255, 255))
    raster.draw_text(s, face, 16, text, 2, 15, (0, 0, 0))


# -- rasteriser ------------------------------------------------------------

def test_surface_starts_filled_with_background():
    s = raster.Surface(8, 4, (10, 20, 30))
    assert _pixel(s, 0, 0) == (10, 20, 30)
    assert _pixel(s, 7, 3) == (10, 20, 30)


def test_fill_rect_bounds_are_half_open():
    s = raster.Surface(10, 10, (0, 0, 0))
    s.fill_rect(2, 2, 5, 5, (255, 255, 255))
    assert _pixel(s, 2, 2) == (255, 255, 255)
    assert _pixel(s, 4, 4) == (255, 255, 255)
    assert _pixel(s, 5, 5) == (0, 0, 0), "x1/y1 must be exclusive"
    assert _pixel(s, 1, 1) == (0, 0, 0)


def test_fill_rect_clips_to_surface():
    s = raster.Surface(6, 6, (0, 0, 0))
    s.fill_rect(-40, -40, 400, 400, (9, 9, 9))
    assert _pixel(s, 0, 0) == (9, 9, 9) and _pixel(s, 5, 5) == (9, 9, 9)
    s.fill_rect(100, 100, 200, 200, (255, 0, 0))  # entirely outside
    assert _pixel(s, 5, 5) == (9, 9, 9)


def test_fill_rect_alpha_blends_halfway():
    s = raster.Surface(4, 4, (0, 0, 0))
    s.fill_rect(0, 0, 4, 4, (200, 100, 50), 128)
    r, g, b = _pixel(s, 1, 1)
    assert abs(r - 100) <= 2 and abs(g - 50) <= 2 and abs(b - 25) <= 2, \
        f"half-alpha blend gave {(r, g, b)}"


def test_fill_rect_alpha_lands_in_the_right_rows_and_nowhere_else():
    """This is what is left of the span-kernel test after the fill moved to
    Rust. The row-at-a-time asmblend path is gone -- the Rust fill covers the
    whole rectangle in one crossing instead of one per row -- so what is worth
    checking is the same thing that test checked underneath the plumbing: the
    blend is exact, it covers every pixel of the rectangle, and it touches
    nothing outside it. The kernels themselves are still exercised, directly
    against their Python references, in tests/test_asmblend.py.

    Note the arithmetic: `// 255`, as the translate tables did. The assembly
    rounded by `>> 8`, which is one level darker at the top of the range.
    """
    s = raster.Surface(6, 4, (40, 40, 40))
    s.fill_rect(1, 1, 5, 3, (200, 100, 50), 128)
    inv = 255 - 128
    expect = tuple((c * 128 + 40 * inv) // 255 for c in (200, 100, 50))
    assert _pixel(s, 1, 1) == expect, (_pixel(s, 1, 1), expect)
    assert _pixel(s, 4, 2) == expect, "the last covered pixel blended too"
    assert _pixel(s, 0, 1) == (40, 40, 40), "the blend escaped to the left"
    assert _pixel(s, 5, 1) == (40, 40, 40), "the blend escaped to the right"
    assert _pixel(s, 1, 0) == (40, 40, 40), "the blend escaped upwards"
    assert _pixel(s, 1, 3) == (40, 40, 40), "the blend escaped downwards"

    s.fill_all((1, 2, 3))
    assert _pixel(s, 0, 0) == (1, 2, 3), "the surface still refills afterwards"


def test_clip_confines_drawing():
    s = raster.Surface(20, 20, (0, 0, 0))
    saved = s.set_clip(5, 5, 10, 10)
    s.fill_rect(0, 0, 20, 20, (255, 255, 255))
    s.reset_clip(saved)
    assert _pixel(s, 7, 7) == (255, 255, 255)
    assert _pixel(s, 4, 4) == (0, 0, 0), "drawing escaped the clip"
    assert _pixel(s, 10, 10) == (0, 0, 0)
    s.fill_rect(0, 0, 2, 2, (1, 2, 3))
    assert _pixel(s, 0, 0) == (1, 2, 3), "clip was not restored"


def test_outline_rect_leaves_the_middle_alone():
    s = raster.Surface(12, 12, (0, 0, 0))
    s.outline_rect(2, 2, 10, 10, (255, 255, 255), 1)
    assert _pixel(s, 2, 2) == (255, 255, 255)
    assert _pixel(s, 5, 5) == (0, 0, 0), "outline should not fill"
    assert _pixel(s, 9, 9) == (255, 255, 255)


def test_draw_line_axis_aligned_and_diagonal():
    s = raster.Surface(16, 16, (0, 0, 0))
    s.draw_line(1, 3, 12, 3, (255, 0, 0))
    assert _pixel(s, 6, 3) == (255, 0, 0)
    s.draw_line(3, 1, 3, 12, (0, 255, 0))
    assert _pixel(s, 3, 6) == (0, 255, 0)
    s.draw_line(0, 0, 15, 15, (0, 0, 255))
    assert _pixel(s, 8, 8) == (0, 0, 255), "diagonal missed its midpoint"


def test_rasterize_fills_a_square_with_clean_edges():
    square = [[(2.0, 2.0), (10.0, 2.0), (10.0, 10.0), (2.0, 10.0)]]
    cov = raster.rasterize(square, 12, 12)
    assert cov[6 * 12 + 6] == 255, "interior should be fully covered"
    assert cov[0] == 0, "exterior should be empty"


def test_rasterize_anti_aliases_partial_coverage():
    """A half-pixel-wide sliver must produce partial coverage, not a hard
    on/off edge -- that is the whole point of the scanline sampler."""
    sliver = [[(1.0, 0.0), (1.5, 0.0), (1.5, 4.0), (1.0, 4.0)]]
    cov = raster.rasterize(sliver, 4, 4)
    value = cov[2 * 4 + 1]
    assert 0 < value < 255, f"expected partial coverage, got {value}"


def test_rasterize_respects_nonzero_winding():
    """An inner contour wound the other way must punch a hole, which is how
    counters in 'o' and 'e' stay open."""
    outer = [(0.0, 0.0), (12.0, 0.0), (12.0, 12.0), (0.0, 12.0)]
    inner = [(4.0, 4.0), (4.0, 8.0), (8.0, 8.0), (8.0, 4.0)]  # reversed
    cov = raster.rasterize([outer, inner], 12, 12)
    assert cov[1 * 12 + 1] == 255, "ring should be filled"
    assert cov[6 * 12 + 6] == 0, "counter should be knocked out"


def test_glyph_bitmap_is_cached_and_positioned():
    font = _sans()
    face = font.face
    gid = face.glyph_id("H")
    first = raster.glyph_bitmap(face, 24, gid)
    again = raster.glyph_bitmap(face, 24, gid)
    assert first is again, "glyph bitmaps must be cached by face/size/glyph"
    cov, w, h, _left, top = first
    assert w > 0 and h > 0 and len(cov) == w * h
    assert top < 0, "a capital sits above the baseline"


def test_blit_coverage_honours_alpha():
    s = raster.Surface(4, 4, (0, 0, 0))
    s.blit_coverage(bytes([255, 128, 0, 0] * 4), 4, 4, 0, 0, (200, 200, 200))
    assert _pixel(s, 0, 0) == (200, 200, 200), "full coverage should be solid"
    mid = _pixel(s, 1, 0)[0]
    assert 80 < mid < 120, f"half coverage gave {mid}"
    assert _pixel(s, 2, 0) == (0, 0, 0), "zero coverage must not paint"


def test_draw_text_advance_matches_measure():
    font = _sans()
    s = raster.Surface(300, 40, (255, 255, 255))
    advance = font.draw(s, "Handgloves 123", 5, 25, (0, 0, 0))
    assert abs(advance - font.measure("Handgloves 123")) < 1e-9, \
        "painted advance disagrees with measured width"


def test_draw_text_actually_marks_pixels():
    font = canvasmod.Font(family="Helvetica", size=24)
    s = raster.Surface(200, 40, (255, 255, 255))
    font.draw(s, "Hello", 5, 30, (0, 0, 0))
    dark = sum(1 for i in range(0, len(s.pixels), 3) if s.pixels[i] < 128)
    assert dark > 40, f"only {dark} dark pixels; text did not render"


def test_png_round_trip():
    s = raster.Surface(17, 11, (30, 60, 90))
    s.fill_rect(3, 3, 9, 7, (200, 10, 10))
    width, height, rgba = imagecodec.decode_png(s.to_png())
    assert (width, height) == (17, 11)
    for i in range(width * height):
        assert tuple(rgba[i * 4:i * 4 + 3]) == tuple(s.pixels[i * 3:i * 3 + 3])


# -- image codecs ----------------------------------------------------------

def test_decode_png_truecolour():
    data = _png(2, 2, 8, 2, bytes([255, 0, 0, 0, 255, 0,
                                   0, 0, 255, 255, 255, 255]))
    w, h, rgba = imagecodec.decode(data)
    assert (w, h) == (2, 2)
    assert bytes(rgba[:8]) == bytes([255, 0, 0, 255, 0, 255, 0, 255])


def test_decode_png_palette_with_transparency():
    palette = bytes([255, 0, 0, 0, 255, 0, 0, 0, 255])
    data = _png(3, 1, 8, 3, bytes([0, 1, 2]), palette=palette,
                trns=bytes([0, 255, 255]))
    _w, _h, rgba = imagecodec.decode(data)
    assert rgba[3] == 0, "palette index 0 should be transparent"
    assert rgba[7] == 255 and tuple(rgba[4:7]) == (0, 255, 0)


def test_decode_png_greyscale_alpha_and_low_bit_depth():
    _w, _h, rgba = imagecodec.decode(_png(2, 1, 8, 4,
                                          bytes([200, 0, 100, 255])))
    assert tuple(rgba[:4]) == (200, 200, 200, 0)
    _w, _h, rgba = imagecodec.decode(_png(4, 1, 1, 0, bytes([0b10100000])))
    assert rgba[0] == 255 and rgba[4] == 0, "1-bit grey should span 0..255"


def test_decode_png_all_filter_types():
    """Every scanline filter must reverse exactly; a single wrong Paeth
    prediction corrupts the rest of the image."""
    width, height, channels = 4, 3, 3
    source = bytes(range(width * height * channels))

    def encode(ftype):
        stride = width * channels
        raw = bytearray()
        prev = bytearray(stride)
        for y in range(height):
            line = source[y * stride:(y + 1) * stride]
            enc = bytearray(stride)
            for i in range(stride):
                a = line[i - channels] if i >= channels else 0
                b = prev[i]
                c = prev[i - channels] if i >= channels else 0
                if ftype == 0:
                    pred = 0
                elif ftype == 1:
                    pred = a
                elif ftype == 2:
                    pred = b
                elif ftype == 3:
                    pred = (a + b) >> 1
                else:
                    p = a + b - c
                    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                    pred = a if (pa <= pb and pa <= pc) else (
                        b if pb <= pc else c)
                enc[i] = (line[i] - pred) & 0xFF
            raw.append(ftype)
            raw += enc
            prev = bytearray(line)

        def chunk(tag, payload):
            body = tag + payload
            return (struct.pack(">I", len(payload)) + body
                    + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF))

        return (b"\x89PNG\r\n\x1a\n"
                + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2,
                                             0, 0, 0))
                + chunk(b"IDAT", zlib.compress(bytes(raw)))
                + chunk(b"IEND", b""))

    for ftype in range(5):
        _w, _h, rgba = imagecodec.decode(encode(ftype))
        got = bytes(b for i in range(width * height)
                    for b in rgba[i * 4:i * 4 + 3])
        assert got == source, f"filter {ftype} did not round-trip"


def test_decode_png_interlaced():
    width = height = 8
    image = bytes([(x * 32 + y * 4) % 256
                   for y in range(height) for x in range(width)])
    passes = ((0, 0, 8, 8), (4, 0, 8, 8), (0, 4, 4, 8), (2, 0, 4, 4),
              (0, 2, 2, 4), (1, 0, 2, 2), (0, 1, 1, 2))
    raw = bytearray()
    for ox, oy, sx, sy in passes:
        pw = (width - ox + sx - 1) // sx
        ph = (height - oy + sy - 1) // sy
        if pw <= 0 or ph <= 0:
            continue
        for y in range(ph):
            raw.append(0)
            for x in range(pw):
                raw.append(image[(oy + y * sy) * width + ox + x * sx])

    def chunk(tag, payload):
        body = tag + payload
        return (struct.pack(">I", len(payload)) + body
                + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF))

    data = (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 0, 0,
                                         0, 1))
            + chunk(b"IDAT", zlib.compress(bytes(raw)))
            + chunk(b"IEND", b""))
    _w, _h, rgba = imagecodec.decode(data)
    assert bytes(rgba[i * 4] for i in range(width * height)) == image


def test_decode_gif_lzw_and_palette():
    """A hand-built GIF whose LZW stream is nothing but literal codes: it
    exercises the bit unpacker and the code-width growth without needing a
    real encoder."""
    palette = bytes([255, 0, 0, 0, 0, 255]) + bytes(6)
    indices = [0, 1, 1, 0]
    min_code = 2                     # 4-entry table -> clear=4, end=5
    bits, width = [], min_code + 1   # codes start 3 bits wide
    for value in indices:
        bits.append((value, width))
    bits.append((5, width))          # end of information
    stream = bytearray()
    acc = acc_bits = 0
    for code, size in bits:
        acc |= code << acc_bits
        acc_bits += size
        while acc_bits >= 8:
            stream.append(acc & 0xFF)
            acc >>= 8
            acc_bits -= 8
    if acc_bits:
        stream.append(acc & 0xFF)

    data = (b"GIF89a" + struct.pack("<HHBBB", 2, 2, 0x80 | 0x01, 0, 0)
            + palette
            + b"\x2C" + struct.pack("<HHHHB", 0, 0, 2, 2, 0)
            + bytes([min_code, len(stream)]) + bytes(stream) + b"\x00"
            + b"\x3B")
    w, h, rgba = imagecodec.decode(data)
    assert (w, h) == (2, 2)
    assert tuple(rgba[0:4]) == (255, 0, 0, 255)
    assert tuple(rgba[4:8]) == (0, 0, 255, 255)


def test_decode_pnm_binary_and_ascii():
    _w, _h, rgba = imagecodec.decode(b"P6\n2 1\n255\n"
                                     + bytes([1, 2, 3, 4, 5, 6]))
    assert tuple(rgba[:4]) == (1, 2, 3, 255)
    _w, _h, rgba = imagecodec.decode(b"P3\n2 1\n255\n9 8 7 6 5 4\n")
    assert tuple(rgba[:4]) == (9, 8, 7, 255)


def test_decode_rejects_unknown_format():
    try:
        imagecodec.decode(b"not an image at all")
    except imagecodec.ImageError:
        return
    raise AssertionError("unknown data should raise ImageError")


def test_resize_nearest_neighbour():
    rgba = bytearray()
    for value in (10, 20, 30, 40):
        rgba += bytes([value, value, value, 255])
    out = imagecodec.resize(rgba, 2, 2, 4, 4)
    assert len(out) == 4 * 4 * 4
    assert out[0] == 10 and out[-4] == 40


def test_resize_is_identity_at_same_size():
    rgba = bytearray(bytes([1, 2, 3, 4]) * 4)
    assert imagecodec.resize(rgba, 2, 2, 2, 2) is rgba

# -- malformed images ------------------------------------------------------
#
# Everything below feeds the decoders bytes no encoder would ever produce.
# Images are the one part of a page that arrives as raw binary from a
# stranger, so the decoders are where a hostile file gets its chance: the
# rule is that a broken image is an ImageError and never a crash, a hang or
# an allocation the file chose the size of. That mattered when this was
# Python and it matters more now that it is Rust, where an unchecked index
# is a panic no `except` can catch.

def _rejects(data, why):
    try:
        imagecodec.decode(data)
    except imagecodec.ImageError:
        return
    raise AssertionError(why)


def test_decode_png_rejects_truncation_in_the_header():
    """Cut a good PNG short at each structural boundary up to the end of
    IHDR: the signature, the length word, the chunk tag, mid-payload."""
    good = _png(4, 3, 8, 2, bytes(36))
    for cut in (0, 1, 7, 8, 12, 20, 25):
        _rejects(good[:cut], f"truncation at {cut} bytes should be rejected")


def test_decode_png_pads_a_truncated_image():
    """Past the header a short file is not an error: a header we believe
    plus fewer pixels than it promised comes back padded, which is what a
    half-arrived image on a slow connection looks like."""
    good = _png(4, 3, 8, 2, bytes(range(36)))
    width, height, rgba = imagecodec.decode(good[:len(good) - 20])
    assert (width, height) == (4, 3)
    assert len(rgba) == 4 * 3 * 4


def test_decode_png_ignores_chunk_crcs():
    """We have never checked CRCs and must not start: a stray bad checksum
    is common in the wild and the pixels are usually perfectly fine."""
    good = _png(2, 2, 8, 2, bytes(12))
    _w, _h, rgba = imagecodec.decode(good[:-4] + b"\x00\x00\x00\x00")
    assert len(rgba) == 2 * 2 * 4


def test_decode_png_rejects_headers_it_cannot_honour():
    _rejects(_png(2, 2, 8, 2, bytes(12), interlace=3),
             "unknown interlace method should be rejected")
    _rejects(_png(2, 2, 7, 2, bytes(12)), "bit depth 7 should be rejected")
    header = struct.pack(">IIBBBBB", 2, 2, 8, 9, 0, 0, 0)
    body = b"IHDR" + header
    _rejects(b"\x89PNG\r\n\x1a\n" + struct.pack(">I", len(header)) + body
             + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF),
             "colour type 9 should be rejected")


def test_decode_png_rejects_absurd_dimensions():
    """Twenty million pixels is the cap, and a header is a claim rather
    than a fact: a 65535x65535 IHDR must not become a 17-gigabyte buffer."""
    _rejects(_png(0xFFFF, 0xFFFF, 8, 2, b""), "4G pixels should be rejected")
    _rejects(_png(0, 0, 8, 2, b""), "a zero-area image should be rejected")
    header = struct.pack(">IIBBBBB", 0x80000000, 4, 8, 2, 0, 0, 0)
    body = b"IHDR" + header
    _rejects(b"\x89PNG\r\n\x1a\n" + struct.pack(">I", len(header)) + body
             + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF),
             "a width past 2^31 should be rejected")


def test_decode_png_rejects_undecodable_pixel_data():
    _rejects(_png(2, 2, 8, 2, bytes(12), idat=b"junkjunkjunk"),
             "an IDAT that is not deflate data should be rejected")
    _rejects(_png(2, 2, 8, 2, bytes(12),
                  idat=zlib.compress(bytes([9, 1, 2, 3, 4, 5, 6,
                                            9, 1, 2, 3, 4, 5, 6]))),
             "filter type 9 does not exist and should be rejected")


def test_decode_png_inflate_is_bounded():
    """A third of a megabyte on the wire that would expand past
    MAX_INFLATED has to stop at the ceiling rather than eat the machine."""
    packer = zlib.compressobj(9)
    megabyte = b"\x00" * (1 << 20)
    parts = [packer.compress(megabyte)
             for _ in range((imagecodec.MAX_INFLATED >> 20) + 4)]
    parts.append(packer.flush())
    bomb = b"".join(parts)
    assert len(bomb) < (1 << 20), "the bomb should be small on the wire"
    _rejects(_png(64, 64, 8, 2, b"", idat=bomb),
             "a zip bomb should be rejected, not decoded")


def test_decode_gif_rejects_truncation():
    good = _gif()
    for cut in (0, 3, 6, 10, 13, 20, 26, 30):
        _rejects(good[:cut], f"truncation at {cut} bytes should be rejected")


def test_decode_gif_rejects_impossible_code_sizes():
    """A GIF palette holds 256 colours at most, so an initial LZW code size
    above 8 is a file asking us to size a table from a number it invented."""
    for min_code in (9, 12, 200, 255):
        raw = bytearray(_gif())
        raw[raw.index(b"\x2C") + 10] = min_code
        _rejects(bytes(raw),
                 f"LZW code size {min_code} should be rejected")


def test_decode_gif_rejects_absurd_geometry():
    _rejects(b"GIF89a" + struct.pack("<HHBBB", 0xFFFF, 0xFFFF, 0, 0, 0)
             + b"\x3B", "a 4G-pixel canvas should be rejected")
    _rejects(b"GIF89a" + struct.pack("<HHBBB", 4, 4, 0, 0, 0) + b"\x3B",
             "a GIF with no image block should be rejected")
    _rejects(b"GIF89a" + struct.pack("<HHBBB", 2, 2, 0x80 | 0x01, 0, 0)
             + bytes(12) + b"\x2C"
             + struct.pack("<HHHHB", 60000, 60000, 2, 2, 0)
             + bytes([2, 1]) + b"\x00\x00\x3B",
             "a frame placed 60000 pixels off canvas should be rejected")


def test_decode_pnm_rejects_malformed_headers():
    _rejects(b"P6", "a header with nothing after it should be rejected")
    _rejects(b"P6\n2 1\n", "a missing maxval should be rejected")
    _rejects(b"P6\nx y\n255\n" + bytes(6), "junk dimensions are rejected")
    _rejects(b"P6\n0 0\n255\n", "a zero-area image should be rejected")
    _rejects(b"P6\n99999 99999\n255\n" + bytes(6),
             "ten billion pixels should be rejected")
    _rejects(b"P3\n2 1\n255\nred green blue\n", "junk samples are rejected")
    _rejects(b"P9\n2 1\n255\n" + bytes(6), "P9 is not a Netpbm type")


def test_decode_pnm_accepts_the_awkward_but_legal():
    """Comments mid-header, 16-bit samples and bitmaps are all in the spec
    and all three take a different path through the reader."""
    _w, _h, rgba = imagecodec.decode(b"P6\n# who\n2 1\n# what\n255\n"
                                     + bytes([1, 2, 3, 4, 5, 6]))
    assert tuple(rgba[:4]) == (1, 2, 3, 255)
    _w, _h, rgba = imagecodec.decode(b"P5\n2 1\n65535\n"
                                     + bytes([255, 255, 0, 0]))
    assert rgba[0] == 255 and rgba[4] == 0
    _w, _h, rgba = imagecodec.decode(b"P4\n8 1\n" + bytes([0b10000000]))
    assert rgba[0] == 0 and rgba[4] == 255, "in PBM a set bit is black"
    _w, _h, rgba = imagecodec.decode(b"P6\n4 4\n255\n" + bytes(6))
    assert len(rgba) == 4 * 4 * 4, "short pixel data is padded, not fatal"


def test_decoders_survive_arbitrary_corruption():
    """Flip bytes at random through each format and insist that the only
    thing which ever comes back is a picture or an ImageError."""
    import random

    seeds = [_png(4, 3, 8, 2, bytes(36)),
             _png(4, 2, 8, 3, bytes([0, 1, 2, 3, 0, 1, 2, 3]),
                  palette=bytes(12)),
             _png(2, 2, 16, 6, bytes(32)),
             _gif(),
             b"P6\n3 2\n255\n" + bytes(18),
             b"P3\n2 2\n255\n1 2 3 4 5 6 7 8\n"]
    rng = random.Random(20260813)
    for _ in range(3000):
        data = bytearray(rng.choice(seeds))
        for _flip in range(rng.randint(1, 5)):
            data[rng.randrange(len(data))] = rng.randrange(256)
        if rng.random() < 0.3:
            del data[rng.randrange(len(data)):]
        try:
            imagecodec.decode(bytes(data))
        except imagecodec.ImageError:
            pass
        except Exception as exc:                 # noqa: BLE001
            raise AssertionError(
                f"{type(exc).__name__} escaped the decoder for "
                f"{bytes(data)!r}: {exc}")


# -- colours ---------------------------------------------------------------

def test_color_parses_every_accepted_form():
    assert canvasmod.color("#f00") == (255, 0, 0)
    assert canvasmod.color("#00ff00") == (0, 255, 0)
    assert canvasmod.color("#0000ffff0000") == (0, 255, 0)
    assert canvasmod.color("rebeccapurple") == (0x66, 0x33, 0x99)
    assert canvasmod.color("  WHITE  ") == (255, 255, 255)


def test_color_empty_is_transparent_and_junk_raises():
    assert canvasmod.color("") is None
    assert canvasmod.color(None) is None
    try:
        canvasmod.color("not-a-colour")
    except canvasmod.TclError:
        return
    raise AssertionError("a bad colour name must raise TclError, as Tk does")


# -- canvas ----------------------------------------------------------------

def test_canvas_items_get_increasing_ids():
    c = canvasmod.Canvas(width=50, height=50)
    first = c.create_rectangle(0, 0, 10, 10, fill="red")
    second = c.create_rectangle(0, 0, 10, 10, fill="blue")
    assert second > first
    assert c.find_all() == [first, second]


def test_canvas_delete_by_tag_and_all():
    c = canvasmod.Canvas(width=50, height=50)
    c.create_rectangle(0, 0, 5, 5, fill="red", tags=("page",))
    keep = c.create_rectangle(0, 0, 5, 5, fill="blue", tags=("chrome",))
    c.delete("page")
    assert c.find_all() == [keep]
    c.delete("all")
    assert c.find_all() == []


def test_canvas_delete_unknown_tag_is_harmless():
    c = canvasmod.Canvas(width=20, height=20)
    item = c.create_rectangle(0, 0, 5, 5, fill="red")
    c.delete("nothing-has-this-tag")
    assert c.find_all() == [item]


def test_canvas_addtag_withtag_matches_tk_semantics():
    """The browser tags a plugin's items by diffing find_all() around the
    call, so adding a tag must reach items found by an existing tag."""
    c = canvasmod.Canvas(width=50, height=50)
    a = c.create_rectangle(0, 0, 5, 5, fill="red", tags=("first",))
    c.create_rectangle(0, 0, 5, 5, fill="blue", tags=("second",))
    c.addtag_withtag("marked", "first")
    assert c.find_withtag("marked") == [a]
    c.addtag_withtag("marked", "first")  # idempotent
    assert c.find_withtag("marked") == [a]


def test_canvas_addtag_by_item_id():
    c = canvasmod.Canvas(width=50, height=50)
    item = c.create_rectangle(0, 0, 5, 5, fill="red")
    c.addtag_withtag("toe-draw", item)
    assert c.find_withtag("toe-draw") == [item]


def test_canvas_rejects_bad_colour_at_creation():
    """The display list catches TclError to fall back to black, so the error
    has to arrive from create_*, not from render()."""
    c = canvasmod.Canvas(width=10, height=10)
    try:
        c.create_rectangle(0, 0, 5, 5, fill="rgb-ish?")
    except canvasmod.TclError:
        return
    raise AssertionError("create_rectangle accepted an invalid colour")


def test_canvas_render_paints_in_creation_order():
    c = canvasmod.Canvas(width=20, height=20, bg="white")
    c.create_rectangle(0, 0, 20, 20, fill="#ff0000", width=0)
    c.create_rectangle(0, 0, 10, 10, fill="#0000ff", width=0)
    s = c.render()
    assert _pixel(s, 5, 5) == (0, 0, 255), "later items must paint on top"
    assert _pixel(s, 15, 15) == (255, 0, 0)


def test_small_filled_oval_is_a_round_dot():
    """A `disc` list marker. The oval used to be stroked only, so a filled
    one came out hollow -- and the corners have to stay empty or the dot is
    a square."""
    c = canvasmod.Canvas(width=20, height=20, bg="white")
    c.create_oval(4, 4, 14, 14, fill="#000000", outline="", width=0)
    s = c.render()
    assert _pixel(s, 9, 9) == (0, 0, 0), "the middle is filled"
    assert _pixel(s, 4, 4) == (255, 255, 255), "the corner is outside the dot"


def test_small_hollow_oval_keeps_its_hole():
    """A `circle` marker is a ring. At six pixels across an aliased ring is
    indistinguishable from a square, so this one is anti-aliased: the edge
    pixels land between the two colours."""
    c = canvasmod.Canvas(width=20, height=20, bg="white")
    c.create_oval(4, 4, 14, 14, fill="", outline="#000000", width=1)
    s = c.render()
    assert _pixel(s, 9, 9) == (255, 255, 255), "the middle stays open"
    edge = _pixel(s, 9, 4)
    assert edge != (255, 255, 255), "the top of the ring is drawn"
    corner = _pixel(s, 4, 4)
    assert corner[0] > 200, f"the corner is outside the ring: {corner}"


def test_big_oval_still_fills_without_supersampling():
    """Past the anti-aliasing size limit the cheap scanline takes over; it
    still has to fill."""
    c = canvasmod.Canvas(width=120, height=120, bg="white")
    c.create_oval(5, 5, 115, 115, fill="#ff0000", outline="", width=0)
    s = c.render()
    assert _pixel(s, 60, 60) == (255, 0, 0), "interior filled"
    assert _pixel(s, 6, 6) == (255, 255, 255), "corner untouched"


def test_canvas_render_is_repeatable():
    c = canvasmod.Canvas(width=30, height=30, bg="white")
    c.create_rectangle(5, 5, 10, 10, fill="#123456", width=0)
    first = bytes(c.render().pixels)
    second = bytes(c.render().pixels)
    assert first == second, "compositing must be idempotent"


def test_canvas_render_reflects_deletion():
    c = canvasmod.Canvas(width=20, height=20, bg="white")
    c.create_rectangle(0, 0, 20, 20, fill="#000000", width=0, tags=("x",))
    c.render()
    c.delete("x")
    s = c.render()
    assert _pixel(s, 10, 10) == (255, 255, 255), \
        "deleted items must not survive the next frame"


def test_canvas_text_anchors():
    font = _sans()
    c = canvasmod.Canvas(width=200, height=60, bg="white")
    nw = c.create_text(10, 10, text="Ay", font=font, fill="black",
                       anchor="nw")
    west = c.create_text(10, 30, text="Ay", font=font, fill="black",
                         anchor="w")
    top_nw = c._bounds(next(i for i in c._items if i.id == nw))[1]
    top_w = c._bounds(next(i for i in c._items if i.id == west))[1]
    assert top_nw == 10, "anchor=nw puts the top edge at y"
    assert top_w < 30, "anchor=w centres the line on y"


def test_canvas_text_width_matches_font():
    font = _sans()
    c = canvasmod.Canvas(width=300, height=40, bg="white")
    item = c.create_text(0, 0, text="Handgloves", font=font, anchor="nw")
    box = c._bounds(next(i for i in c._items if i.id == item))
    assert abs((box[2] - box[0]) - font.measure("Handgloves")) < 1e-9


def test_canvas_stipple_renders_translucent():
    c = canvasmod.Canvas(width=10, height=10, bg="white")
    c.create_rectangle(0, 0, 10, 10, fill="#000000", width=0,
                       stipple="gray50")
    value = _pixel(c.render(), 5, 5)[0]
    assert 100 < value < 160, f"stippled black over white gave {value}"


def test_canvas_image_blit():
    photo = canvasmod.PhotoImage(
        data=_png(2, 2, 8, 2, bytes([255, 0, 0] * 4)))
    assert (photo.width(), photo.height()) == (2, 2)
    assert photo.opaque, "an alpha-free PNG should take the fast blit path"
    c = canvasmod.Canvas(width=10, height=10, bg="white")
    c.create_image(1, 1, image=photo, anchor="nw")
    s = c.render()
    assert _pixel(s, 1, 1) == (255, 0, 0)
    assert _pixel(s, 5, 5) == (255, 255, 255)


def test_canvas_blank_photoimage_has_a_size():
    photo = canvasmod.PhotoImage(width=200, height=100)
    assert (photo.width(), photo.height()) == (200, 100)


def test_canvas_resize_keeps_background():
    c = canvasmod.Canvas(width=10, height=10, bg="#102030")
    c.resize(40, 20)
    assert (c.winfo_width(), c.winfo_height()) == (40, 20)
    assert _pixel(c.render(), 39, 19) == (0x10, 0x20, 0x30)


def test_canvas_render_region_clips():
    c = canvasmod.Canvas(width=40, height=40, bg="white")
    c.create_rectangle(0, 0, 40, 40, fill="#00ff00", width=0)
    c.render()
    c.delete("all")
    c.render(region=(0, 0, 10, 10))
    s = c.surface
    assert _pixel(s, 5, 5) == (255, 255, 255), "region should have been reset"
    assert _pixel(s, 20, 20) == (0, 255, 0), "outside the region is untouched"


def test_canvas_arc_strokes_without_filling():
    c = canvasmod.Canvas(width=40, height=40, bg="white")
    c.create_arc(10, 10, 30, 30, start=0, extent=270, style="arc",
                 outline="#ff0000", width=2)
    s = c.render()
    assert _pixel(s, 20, 20) == (255, 255, 255), "an arc must not fill"
    edge = [_pixel(s, x, 20) for x in range(28, 32)]
    assert any(p != (255, 255, 255) for p in edge), "arc drew nothing"


# -- window / event loop ---------------------------------------------------

def test_window_bindings_fire():
    w = Window()
    seen = []
    w.bind("<Button-1>", lambda e: seen.append((e.x, e.y)))
    assert w.dispatch("<Button-1>", Event(x=3, y=4))
    assert seen == [(3, 4)]
    assert not w.dispatch("<Button-3>"), "unbound sequences report no handler"


def test_window_binding_errors_do_not_escape():
    """Tk reported handler exceptions and carried on; so must we, or one bad
    plugin takes down the browser."""
    w = Window()
    w.on_callback_error = lambda where, exc: None
    w.bind("<Key>", lambda e: 1 // 0)
    w.dispatch("<Key>", Event(keysym="a"))


def test_window_timers_run_in_order():
    w = Window()
    order = []
    w.after(0, lambda: order.append("first"))
    w.after(0, lambda: order.append("second"))
    w.flush_timers()
    assert order == ["first", "second"]


def test_window_after_cancel_prevents_the_call():
    w = Window()
    fired = []
    handle = w.after(0, lambda: fired.append(1))
    w.after_cancel(handle)
    w.flush_timers()
    assert fired == []


def test_window_timer_not_yet_due_is_kept():
    w = Window()
    fired = []
    w.after(60_000, lambda: fired.append(1))
    wait = w.flush_timers()
    assert fired == []
    assert wait is not None and wait > 1, "should report the time remaining"


def test_window_geometry_and_resize_event():
    w = Window()
    seen = []
    w.bind("<Configure>", lambda e: seen.append((e.width, e.height)))
    w.geometry("640x480")
    assert (w.winfo_width(), w.winfo_height()) == (640, 480)
    assert seen == [(640, 480)]
    assert w.geometry() == "640x480"


def test_window_minsize_is_enforced_on_resize():
    w = Window()
    w.minsize(300, 200)
    w.resize(100, 100)
    assert (w.width, w.height) == (300, 200)


def test_window_clipboard_round_trip():
    w = Window()
    w.clipboard_clear()
    w.clipboard_append("hello")
    assert w.clipboard_get() == "hello"


def test_window_destroy_takes_children_with_it():
    from feetbrowser.window import Tk, Toplevel
    root = Tk()
    child = Toplevel(root)
    assert child in root.children
    root.destroy()
    assert not child.winfo_exists() and not root.winfo_exists()


def test_gui_backend_exports_everything_used():
    for name in ("Tk", "Toplevel", "Canvas", "PhotoImage", "TclError",
                 "Font"):
        assert getattr(gui, name, None) is not None, f"gui.{name} missing"
    assert gui.backend() in ("raster", "tk")


# -- images end to end -----------------------------------------------------
#
# imagecodec is tested above against known pixels, and passed happily while
# every <img> on the screen was the "[img]" placeholder: decoding was never
# the broken part. What follows drives the whole path instead -- page load,
# the fetch that runs off the UI thread, the timer sweep that publishes the
# decoded image, layout, and the blit -- and looks at the pixels that come
# out the far end.

IMAGE_RGB = (255, 0, 255)
IMAGE_SIZE = 8


def _page_with_image(directory, rgb=IMAGE_RGB, size=IMAGE_SIZE):
    """Write an HTML file whose <img> is a data: PNG of a solid colour."""
    import base64
    samples = bytes(rgb) * size * size
    src = "data:image/png;base64," + base64.b64encode(
        _png(size, size, 8, 2, samples)).decode()
    path = os.path.join(directory, "page.html")
    with open(path, "w") as handle:
        handle.write(f"<!doctype html><title>img</title><p><img src='{src}'>")
    return path


def _serve_page_with_image(delay, rgb=IMAGE_RGB, size=IMAGE_SIZE):
    """A loopback server: an HTML page, and a PNG that takes `delay` to send.

    The delay is the whole point. Images arrive after the document that asked
    for them, and anything that captures a frame in between captures
    placeholders -- so a test that lets the image win the race tests nothing.
    """
    import http.server
    import threading
    import time

    pixels = _png(size, size, 8, 2, bytes(rgb) * size * size)

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path.endswith(".png"):
                time.sleep(delay)
                body, ctype = pixels, "image/png"
            else:
                body = b'<!doctype html><title>img</title><p><img src="/i.png">'
                ctype = "text/html"
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _count_pixels(surface, rgb):
    return sum(1 for y in range(surface.height) for x in range(surface.width)
               if _pixel(surface, x, y) == rgb)


def test_screenshot_paints_images_rather_than_placeholders():
    """A screenshot must contain the page's image, not its alt text.

    The regression this guards was not in any decoder: --screenshot stopped
    waiting the moment the *document* had arrived, which is the moment image
    loading begins, so every frame was captured with an empty image cache and
    every <img> laid out as "[img]".
    """
    import shutil
    import tempfile
    from feetbrowser.browser import screenshot

    work = tempfile.mkdtemp(prefix="fb-shot-")
    server = _serve_page_with_image(0.3)
    try:
        out = os.path.join(work, "shot.png")
        url = "http://127.0.0.1:%d/page" % server.server_address[1]
        browser = screenshot(url, out, settle=20.0)
        placeholders = [c for c in browser.tabs[0].display_list
                        if "[img" in getattr(c, "text", "")]
        assert not placeholders, f"placeholder still drawn: {placeholders}"
        width, height, rgba = imagecodec.decode(open(out, "rb").read())
        assert (width, height) == (browser.canvas.winfo_width(),
                                   browser.canvas.winfo_height())
        painted = sum(1 for i in range(0, len(rgba), 4)
                      if tuple(rgba[i:i + 3]) == IMAGE_RGB)
        assert painted == IMAGE_SIZE * IMAGE_SIZE, \
            f"expected an {IMAGE_SIZE}px square, found {painted} pixels"
    finally:
        server.shutdown()
        shutil.rmtree(work, ignore_errors=True)


def test_settle_waits_for_images_a_finished_document_asked_for():
    """`loading` going false does not mean the page is finished.

    A document is fetched first and its images afterwards, so there is a
    window in which nothing is "loading" and the page is still all
    placeholders. Browser.settle() has to span it.
    """
    import shutil
    import tempfile
    from feetbrowser.browser import Browser

    work = tempfile.mkdtemp(prefix="fb-settle-")
    try:
        page = _page_with_image(work)
        browser = Browser()
        browser.new_tab("file://" + page)
        tab = browser.tabs[0]
        assert not tab.loading, "a file: document loads synchronously"
        assert tab.pending_images(), "images are queued, so work remains"
        assert browser.busy(), "and the browser has to call that busy"
        assert browser.settle(20.0), "settle should not have timed out"
        assert not browser.busy() and not tab.pending_images()
        assert tab.image_cache, "settling means the image is decoded"
        browser.draw()
        assert _count_pixels(browser.canvas.render(),
                             IMAGE_RGB) == IMAGE_SIZE * IMAGE_SIZE
    finally:
        shutil.rmtree(work, ignore_errors=True)


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
    print(f"\nALL {len(tests)} RENDER TESTS PASSED")


if __name__ == "__main__":
    main()
