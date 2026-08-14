"""Build real AVI/MP4/WebM bytes for the media tests.

Test media is generated here rather than committed, for the reason every
media project eventually learns: a binary fixture is a file nobody can read
in a diff, and one whose provenance and licence you have to keep explaining.
Everything below writes the same headers a muxer writes -- correct chunk
sizes, correct alignment, a real `idx1` -- so the parser under test is
parsing a file and not a mock.

Not a test suite: nothing here starts with `test_`, so `tests/test_suites.py`
does not expect a runner to name it. The tests live in `tests/test_render.py`.
"""

import struct


def _chunk(fourcc, payload):
    """A RIFF chunk: id, little-endian size, payload, pad to even."""
    assert len(fourcc) == 4, fourcc
    out = fourcc.encode("latin-1") + struct.pack("<I", len(payload)) + payload
    if len(payload) & 1:
        out += b"\x00"
    return out


def _list(list_type, payload):
    assert len(list_type) == 4, list_type
    body = list_type.encode("latin-1") + payload
    return b"LIST" + struct.pack("<I", len(body)) + body


def bitmapinfoheader(width, height, bit_count, compression, palette=None,
                     top_down=False):
    """A 40-byte BITMAPINFOHEADER plus its colour table.

    `height` is stored negative for a top-down bitmap, which is the only way
    the format has of saying which way up the rows are.
    """
    stored_height = -height if top_down else height
    header = struct.pack("<IiiHHIIiiII", 40, width, stored_height, 1,
                         bit_count, compression, 0, 0, 0,
                         len(palette) if palette else 0, 0)
    table = b""
    if palette:
        for entry in palette:
            r, g, b = entry[0], entry[1], entry[2]
            table += bytes((b, g, r, 0))
    return header + table


def avi(frames, width, height, fps=25.0, bit_count=24, compression=0,
        palette=None, top_down=False, keyframes=None, handler="DIB ",
        with_index=True, total_frames=None):
    """A single-video-stream AVI over the given list of packet payloads."""
    micros = int(round(1000000.0 / fps))
    count = len(frames) if total_frames is None else total_frames
    avih = struct.pack("<IIIIIIIIIIIIII", micros, 0, 0, 0x10, count, 0, 1, 0,
                       width, height, 0, 0, 0, 0)
    # dwRate/dwScale is the exact rate; dwMicroSecPerFrame above is rounded,
    # and the parser is expected to prefer the exact pair.
    scale = 1000
    rate = int(round(fps * scale))
    strh = (b"vids" + handler.encode("latin-1")
            + struct.pack("<IHHIIIIIIIIhhhh", 0, 0, 0, 0, scale, rate, 0,
                          len(frames), 0, 0, 0, 0, 0, width, height))
    strf = bitmapinfoheader(width, height, bit_count, compression, palette,
                            top_down)
    hdrl = _list("hdrl", _chunk("avih", avih)
                 + _list("strl", _chunk("strh", strh) + _chunk("strf", strf)))

    movi_body = b""
    entries = []
    for i, payload in enumerate(frames):
        # idx1 offsets are measured from the 'movi' fourcc.
        offset = 4 + len(movi_body)
        key = True if keyframes is None else bool(keyframes[i])
        entries.append((b"00dc", 0x10 if key else 0, offset, len(payload)))
        movi_body += _chunk("00dc", payload)
    movi = _list("movi", movi_body)

    body = hdrl + movi
    if with_index:
        idx = b"".join(struct.pack("<4sIII", *e) for e in entries)
        body += _chunk("idx1", idx)
    return b"RIFF" + struct.pack("<I", 4 + len(body)) + b"AVI " + body


def rgb24_frame(width, height, pixel, top_down=False):
    """One BI_RGB 24-bit frame. `pixel(x, y)` returns an (r, g, b) triple in
    image coordinates -- top-left origin -- whichever way up the rows go."""
    stride = ((width * 24 + 31) // 32) * 4
    rows = []
    for row in range(height):
        y = row if top_down else height - 1 - row
        data = bytearray(stride)
        for x in range(width):
            r, g, b = pixel(x, y)
            data[x * 3] = b
            data[x * 3 + 1] = g
            data[x * 3 + 2] = r
        rows.append(bytes(data))
    return b"".join(rows)


def rgb32_frame(width, height, pixel, top_down=False):
    rows = []
    for row in range(height):
        y = row if top_down else height - 1 - row
        data = bytearray(width * 4)
        for x in range(width):
            r, g, b = pixel(x, y)
            data[x * 4] = b
            data[x * 4 + 1] = g
            data[x * 4 + 2] = r
        rows.append(bytes(data))
    return b"".join(rows)


def pal8_frame(width, height, index, top_down=False):
    """One uncompressed 8-bit palettised frame, rows padded to 4 bytes."""
    stride = ((width * 8 + 31) // 32) * 4
    rows = []
    for row in range(height):
        y = row if top_down else height - 1 - row
        data = bytearray(stride)
        for x in range(width):
            data[x] = index(x, y)
        rows.append(bytes(data))
    return b"".join(rows)


def rle8_keyframe(width, height, index, top_down=False):
    """Encode a whole picture as BI_RLE8 runs: the honest keyframe case."""
    out = bytearray()
    for row in range(height):
        y = row if top_down else height - 1 - row
        x = 0
        while x < width:
            value = index(x, y)
            run = 1
            while (x + run < width and run < 255
                   and index(x + run, y) == value):
                run += 1
            out += bytes((run, value))
            x += run
        out += b"\x00\x00"          # end of line
    out += b"\x00\x01"              # end of bitmap
    return bytes(out)


def rle8_delta(ops):
    """Hand-built RLE8 opcodes, so a test can assert on a frame it wrote
    byte by byte. `ops` is a list of tuples:

        ("run", count, index)      count copies of index
        ("literal", [indices])     an absolute run, word-padded here
        ("delta", dx, dy)          move without touching pixels
        ("eol",)                   end of line
        ("eob",)                   end of bitmap
    """
    out = bytearray()
    for op in ops:
        kind = op[0]
        if kind == "run":
            out += bytes((op[1], op[2]))
        elif kind == "literal":
            values = bytes(op[1])
            out += bytes((0, len(values))) + values
            if len(values) & 1:
                out += b"\x00"
        elif kind == "delta":
            out += bytes((0, 2, op[1], op[2]))
        elif kind == "eol":
            out += b"\x00\x00"
        elif kind == "eob":
            out += b"\x00\x01"
        else:
            raise ValueError("unknown RLE8 op %r" % (op,))
    return bytes(out)


def grey_palette():
    """256 entries where index i is the grey (i, i, i) -- so a test can say
    what colour a pixel is by naming its index."""
    return [(i, i, i) for i in range(256)]


# -- containers we only probe ------------------------------------------------

def _box(kind, payload):
    return struct.pack(">I", 8 + len(payload)) + kind.encode("latin-1") \
        + payload


def mp4(width, height, duration, timescale=600, codec="avc1"):
    """Enough ISO base media for the prober: ftyp, mvhd, tkhd, stsd."""
    ftyp = _box("ftyp", b"isom" + struct.pack(">I", 512) + b"isomavc1")
    mvhd = _box("mvhd", struct.pack(">BBBBIIII", 0, 0, 0, 0, 0, 0, timescale,
                                    int(duration * timescale))
                + b"\x00" * 80)
    tkhd = _box("tkhd", struct.pack(">BBBB", 0, 0, 0, 7)
                + struct.pack(">IIIII", 0, 0, 1, 0, 0)
                + b"\x00" * 52
                + struct.pack(">II", width << 16, height << 16))
    entry = struct.pack(">I", 8 + 78) + codec.encode("latin-1") + b"\x00" * 78
    stsd = _box("stsd", struct.pack(">II", 0, 1) + entry)
    stbl = _box("stbl", stsd)
    minf = _box("minf", stbl)
    mdia = _box("mdia", minf)
    trak = _box("trak", tkhd + mdia)
    moov = _box("moov", mvhd + trak)
    return ftyp + moov + _box("mdat", b"\x00" * 16)


def _ebml(element_id, payload):
    raw_id = b""
    value = element_id
    while value:
        raw_id = bytes((value & 0xFF,)) + raw_id
        value >>= 8
    size = len(payload)
    # Four-byte size marker: plenty for a fixture, and exercises the
    # multi-byte varint path in the reader.
    return raw_id + struct.pack(">I", size | 0x10000000) + payload


def _ebml_uint(element_id, value):
    raw = b""
    while True:
        raw = bytes((value & 0xFF,)) + raw
        value >>= 8
        if not value:
            break
    return _ebml(element_id, raw)


def webm(width, height, duration, codec="V_VP9", timecode_scale=1000000):
    """Enough Matroska for the prober: Info duration and Tracks dimensions."""
    header = b"\x1a\x45\xdf\xa3" + struct.pack(">I", 5 | 0x10000000) \
        + b"\x42\x86\x81\x01\x00"
    info = _ebml(0x1549A966,
                 _ebml_uint(0x2AD7B1, timecode_scale)
                 + _ebml(0x4489, struct.pack(">d",
                                             duration * 1e9 / timecode_scale)))
    video = _ebml(0xE0, _ebml_uint(0xB0, width) + _ebml_uint(0xBA, height))
    entry = _ebml(0xAE, _ebml(0x86, codec.encode("latin-1")) + video)
    tracks = _ebml(0x1654AE6B, entry)
    segment = _ebml(0x18538067, info + tracks)
    return header + segment
