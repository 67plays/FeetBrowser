"""Timings for the pixel pipeline.

Not a test: nothing here asserts. It exists so a change to the rasteriser,
the font engine or the image codecs can be argued about with numbers from
one machine rather than with adjectives. Run it before and after.

    .venv/bin/python tests/bench_render.py

Every figure is milliseconds per call, the mean over enough repetitions to
outrun the clock's resolution. The interesting one is draw_text: a page is
mostly text, and text is the only thing here that touches pixels one at a
time.
"""
import os
import struct
import sys
import time
import zlib

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from feetbrowser import fontengine, imagecodec, raster  # noqa: E402

WIDTH, HEIGHT = 1000, 700
TEXT = "The quick brown fox jumps over the lazy dog. " * 2


def bench(label, reps, fn):
    fn(0)  # warm every cache the operation owns
    start = time.perf_counter()
    for i in range(reps):
        fn(i)
    per = (time.perf_counter() - start) * 1000.0 / reps
    print("%-34s %8.3f ms" % (label, per))
    return per


def _face():
    for family in ("Helvetica", "DejaVu Sans", "Liberation Sans", "Arial"):
        face = fontengine.find(family)
        if face is not None:
            return face
    index = fontengine.index()
    if not index:
        raise SystemExit("no fonts installed; nothing to measure")
    return fontengine.find(sorted(index)[0])


def _sample_png(width, height):
    def chunk(tag, payload):
        body = tag + payload
        return (struct.pack(">I", len(payload)) + body
                + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF))

    raw = bytearray()
    for y in range(height):
        raw.append(0)
        for x in range(width):
            raw += bytes(((x * 3) % 256, (y * 5) % 256, (x + y) % 256))
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2,
                                         0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes(raw), 6))
            + chunk(b"IEND", b""))


def main():
    surface = raster.Surface(WIDTH, HEIGHT)
    face = _face()

    bench("raster.fill_rect full surface", 20,
          lambda i: surface.fill_rect(0, 0, WIDTH, HEIGHT, (255, 255, 255)))
    bench("raster.fill_rect 80x40", 2000,
          lambda i: surface.fill_rect(i % 900, (i * 7) % 600,
                                      i % 900 + 80, (i * 7) % 600 + 40,
                                      (200, 40, 40)))
    bench("raster.fill_rect alpha 80x40", 2000,
          lambda i: surface.fill_rect(i % 900, (i * 7) % 600,
                                      i % 900 + 80, (i * 7) % 600 + 40,
                                      (200, 40, 40), 128))
    bench("raster.draw_text, 90 chars", 200,
          lambda i: raster.draw_text(surface, face, 16, TEXT, 10,
                                     (i * 3) % 650, (0, 0, 0)))
    bench("raster.measure_text 90 chars", 500,
          lambda i: raster.measure_text(face, 16, TEXT))
    # A different glyph per repetition, so the cache never answers and this is
    # outline extraction plus a scanline fill at a body-text size.
    bench("raster.glyph_bitmap uncached", 60,
          lambda i: raster.glyph_bitmap(face, 16, 40 + i))
    bench("raster.rasterize 200x200 star", 100,
          lambda i: raster.rasterize([_star(100.0)], 200, 200, 100.0, 100.0))
    bench("raster.to_png %dx%d" % (WIDTH, HEIGHT), 5,
          lambda i: surface.to_png())

    png = _sample_png(400, 300)
    bench("imagecodec.decode_png 400x300", 20, lambda i: imagecodec.decode(png))
    rgba = imagecodec.decode(png)[2]
    bench("imagecodec.resize 400x300->800x600", 20,
          lambda i: imagecodec.resize(rgba, 400, 300, 800, 600))

    with open(_font_path(face), "rb") as f:
        data = f.read()
    bench("fontengine.Font parse + cmap", 20, lambda i: _parse(data))
    # A fresh face every time, so this is outline extraction rather than a
    # cache hit -- which is what a first paint actually pays.
    bench("fontengine.glyph_contours cold", 200,
          lambda i: fontengine.Font(data).glyph_contours(_gid(face)))
    contours = face.glyph_contours(_gid(face))
    bench("fontengine.flatten 'g' at 64px", 2000,
          lambda i: fontengine.flatten(contours, face.scale(64)))


def _gid(face):
    return face.glyph_id("g")


def _parse(data):
    font = fontengine.Font(data)
    font.glyph_id("A")
    return font


def _font_path(face):
    for faces in fontengine.index().values():
        for path, _face_index in faces.values():
            if fontengine.load(path, _face_index) is face:
                return path
    raise SystemExit("cannot locate the file behind the chosen face")


def _star(radius):
    import math
    pts = []
    for k in range(10):
        r = radius if k % 2 == 0 else radius * 0.45
        a = math.pi * k / 5.0
        pts.append((r * math.cos(a), r * math.sin(a)))
    return pts


if __name__ == "__main__":
    main()
