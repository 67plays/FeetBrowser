"""Build real AVI/MOV/MP4/WebM bytes for the media tests.

Test media is generated here rather than committed, for the reason every
media project eventually learns: a binary fixture is a file nobody can read
in a diff, and one whose provenance and licence you have to keep explaining.
Everything below writes the same headers a muxer writes -- correct chunk
sizes, correct alignment, a real `idx1` -- so the parser under test is
parsing a file and not a mock.

Not a test suite: nothing here starts with `test_`, so `tests/test_suites.py`
does not expect a runner to name it. The tests live in `tests/test_render.py`.
"""

import math
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


def _auds_strl(format_tag=0x0055, channels=2, sample_rate=44100, length=0):
    """A second `strl` describing a sound stream.

    Enough of one to be read and named: a `strh` that says `auds` and a
    `strf` that is a WAVEFORMATEX, whose first field is the format tag --
    which is how AVI names a codec, a 16-bit number rather than a fourcc.
    """
    strh = (b"auds" + b"\x00\x00\x00\x00"
            + struct.pack("<IHHIIIIIIIIhhhh", 0, 0, 0, 0, 1, sample_rate, 0,
                          length, 0, 0, 0, 0, 0, 0, 0))
    strf = struct.pack("<HHIIHHH", format_tag, channels, sample_rate,
                       sample_rate * channels * 2, channels * 2, 16, 0)
    return _list("strl", _chunk("strh", strh) + _chunk("strf", strf))


def avi(frames, width, height, fps=25.0, bit_count=24, compression=0,
        palette=None, top_down=False, keyframes=None, handler="DIB ",
        with_index=True, total_frames=None, audio=None):
    """A single-video-stream AVI over the given list of packet payloads.

    `audio` is a dict of `_auds_strl` arguments, and adds a sound stream's
    headers -- headers only. No `##wb` chunks are written, because nothing
    demuxes AVI audio and a fixture that pretended otherwise would be
    describing a feature this repository does not have.
    """
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
    streams = _list("strl", _chunk("strh", strh) + _chunk("strf", strf))
    if audio is not None:
        streams += _auds_strl(**audio)
    hdrl = _list("hdrl", _chunk("avih", avih) + streams)

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


# -- JPEG, so that a Motion JPEG fixture is a real Motion JPEG ---------------
#
# A test that wants to know whether the MJPEG path works has to hand it JPEGs,
# and there is nowhere to get one from that is not either a committed binary
# or a library. So this encodes them. It is a genuine baseline encoder --
# level shift, 8x8 DCT, quantise, zigzag, Huffman -- and what comes out is a
# file any decoder reads; it is only the choices inside that are made for a
# fixture's convenience rather than a photograph's.
#
# Two of those choices are worth naming. The quantisation table is all ones,
# which is the largest file and the smallest error: a test that says "this
# frame is the colour #204070" wants that colour back, not that colour after
# a quality-75 table has been through it. And the Huffman codes are the
# standard ones from Annex K of the JPEG specification, transcribed below
# rather than built from the image's own symbol frequencies.
#
# That second choice is the one that matters, and it is not about size. Motion
# JPEG frames very often carry no DHT segment at all, because the tables would
# be identical in every frame of the clip; a decoder is expected to know the
# Annex K tables and supply them itself. `strip_huffman_tables` below produces
# exactly those frames, and it can only produce a *decodable* one if the codes
# the encoder used were the standard codes to begin with. So the fixture uses
# them, and the round trip through the abbreviated form is then a real test of
# the decoder's copy of the tables rather than a test of a private agreement
# between two halves of the test suite.

_ZIGZAG = (0, 1, 8, 16, 9, 2, 3, 10, 17, 24, 32, 25, 18, 11, 4, 5,
           12, 19, 26, 33, 40, 48, 41, 34, 27, 20, 13, 6, 7, 14, 21, 28,
           35, 42, 49, 56, 57, 50, 43, 36, 29, 22, 15, 23, 30, 37, 44, 51,
           58, 59, 52, 45, 38, 31, 39, 46, 53, 60, 61, 54, 47, 55, 62, 63)

# Annex K, tables K.3 and K.4: the DC codes, over magnitude categories 0..11.
# Eleven is enough for any 8-bit input through an unquantised DCT, whose DC
# coefficient lands in -1024..1016 and whose successive difference therefore
# never needs a twelfth bit.
_DC_LUMA_BITS = (0, 1, 5, 1, 1, 1, 1, 1, 1, 0, 0, 0, 0, 0, 0, 0)
_DC_CHROMA_BITS = (0, 3, 1, 1, 1, 1, 1, 1, 1, 1, 1, 0, 0, 0, 0, 0)
_DC_VALUES = tuple(range(12))

# Annex K, table K.5: the luminance AC codes. The symbols are (run, size)
# bytes, plus 0x00 for end-of-block and 0xF0 for a run of sixteen zeroes.
_AC_LUMA_BITS = (0, 2, 1, 3, 3, 2, 4, 3, 5, 5, 4, 4, 0, 0, 1, 0x7D)
_AC_LUMA_VALUES = (
    0x01, 0x02, 0x03, 0x00, 0x04, 0x11, 0x05, 0x12,
    0x21, 0x31, 0x41, 0x06, 0x13, 0x51, 0x61, 0x07,
    0x22, 0x71, 0x14, 0x32, 0x81, 0x91, 0xA1, 0x08,
    0x23, 0x42, 0xB1, 0xC1, 0x15, 0x52, 0xD1, 0xF0,
    0x24, 0x33, 0x62, 0x72, 0x82, 0x09, 0x0A, 0x16,
    0x17, 0x18, 0x19, 0x1A, 0x25, 0x26, 0x27, 0x28,
    0x29, 0x2A, 0x34, 0x35, 0x36, 0x37, 0x38, 0x39,
    0x3A, 0x43, 0x44, 0x45, 0x46, 0x47, 0x48, 0x49,
    0x4A, 0x53, 0x54, 0x55, 0x56, 0x57, 0x58, 0x59,
    0x5A, 0x63, 0x64, 0x65, 0x66, 0x67, 0x68, 0x69,
    0x6A, 0x73, 0x74, 0x75, 0x76, 0x77, 0x78, 0x79,
    0x7A, 0x83, 0x84, 0x85, 0x86, 0x87, 0x88, 0x89,
    0x8A, 0x92, 0x93, 0x94, 0x95, 0x96, 0x97, 0x98,
    0x99, 0x9A, 0xA2, 0xA3, 0xA4, 0xA5, 0xA6, 0xA7,
    0xA8, 0xA9, 0xAA, 0xB2, 0xB3, 0xB4, 0xB5, 0xB6,
    0xB7, 0xB8, 0xB9, 0xBA, 0xC2, 0xC3, 0xC4, 0xC5,
    0xC6, 0xC7, 0xC8, 0xC9, 0xCA, 0xD2, 0xD3, 0xD4,
    0xD5, 0xD6, 0xD7, 0xD8, 0xD9, 0xDA, 0xE1, 0xE2,
    0xE3, 0xE4, 0xE5, 0xE6, 0xE7, 0xE8, 0xE9, 0xEA,
    0xF1, 0xF2, 0xF3, 0xF4, 0xF5, 0xF6, 0xF7, 0xF8,
    0xF9, 0xFA)

# Annex K, table K.6: the chrominance AC codes.
_AC_CHROMA_BITS = (0, 2, 1, 2, 4, 4, 3, 4, 7, 5, 4, 4, 0, 1, 2, 0x77)
_AC_CHROMA_VALUES = (
    0x00, 0x01, 0x02, 0x03, 0x11, 0x04, 0x05, 0x21,
    0x31, 0x06, 0x12, 0x41, 0x51, 0x07, 0x61, 0x71,
    0x13, 0x22, 0x32, 0x81, 0x08, 0x14, 0x42, 0x91,
    0xA1, 0xB1, 0xC1, 0x09, 0x23, 0x33, 0x52, 0xF0,
    0x15, 0x62, 0x72, 0xD1, 0x0A, 0x16, 0x24, 0x34,
    0xE1, 0x25, 0xF1, 0x17, 0x18, 0x19, 0x1A, 0x26,
    0x27, 0x28, 0x29, 0x2A, 0x35, 0x36, 0x37, 0x38,
    0x39, 0x3A, 0x43, 0x44, 0x45, 0x46, 0x47, 0x48,
    0x49, 0x4A, 0x53, 0x54, 0x55, 0x56, 0x57, 0x58,
    0x59, 0x5A, 0x63, 0x64, 0x65, 0x66, 0x67, 0x68,
    0x69, 0x6A, 0x73, 0x74, 0x75, 0x76, 0x77, 0x78,
    0x79, 0x7A, 0x82, 0x83, 0x84, 0x85, 0x86, 0x87,
    0x88, 0x89, 0x8A, 0x92, 0x93, 0x94, 0x95, 0x96,
    0x97, 0x98, 0x99, 0x9A, 0xA2, 0xA3, 0xA4, 0xA5,
    0xA6, 0xA7, 0xA8, 0xA9, 0xAA, 0xB2, 0xB3, 0xB4,
    0xB5, 0xB6, 0xB7, 0xB8, 0xB9, 0xBA, 0xC2, 0xC3,
    0xC4, 0xC5, 0xC6, 0xC7, 0xC8, 0xC9, 0xCA, 0xD2,
    0xD3, 0xD4, 0xD5, 0xD6, 0xD7, 0xD8, 0xD9, 0xDA,
    0xE2, 0xE3, 0xE4, 0xE5, 0xE6, 0xE7, 0xE8, 0xE9,
    0xEA, 0xF2, 0xF3, 0xF4, 0xF5, 0xF6, 0xF7, 0xF8,
    0xF9, 0xFA)

_COS = [[math.cos((2 * x + 1) * u * math.pi / 16) for u in range(8)]
        for x in range(8)]


def _canonical(bits, values):
    """{symbol: (code, length)} for a JPEG Huffman table.

    The generation procedure the standard gives: codes are handed out in
    increasing length, in the order the values are listed, and moving to the
    next length shifts the running code left by one.
    """
    assert sum(bits) == len(values)
    codes = {}
    code = 0
    index = 0
    for length in range(1, 17):
        for _ in range(bits[length - 1]):
            codes[values[index]] = (code, length)
            index += 1
            code += 1
        code <<= 1
    return codes


_DC_LUMA_CODES = _canonical(_DC_LUMA_BITS, _DC_VALUES)
_DC_CHROMA_CODES = _canonical(_DC_CHROMA_BITS, _DC_VALUES)
_AC_LUMA_CODES = _canonical(_AC_LUMA_BITS, _AC_LUMA_VALUES)
_AC_CHROMA_CODES = _canonical(_AC_CHROMA_BITS, _AC_CHROMA_VALUES)


class _BitWriter:
    """MSB-first bits, with JPEG's byte stuffing: a 0xFF in the entropy
    stream is followed by a 0x00 so it cannot be read as a marker."""

    def __init__(self):
        self.out = bytearray()
        self._bits = 0
        self._count = 0

    def write(self, value, length):
        for shift in range(length - 1, -1, -1):
            self._bits = (self._bits << 1) | ((value >> shift) & 1)
            self._count += 1
            if self._count == 8:
                self.out.append(self._bits)
                if self._bits == 0xFF:
                    self.out.append(0x00)
                self._bits = 0
                self._count = 0

    def flush(self):
        while self._count:
            self.write(1, 1)        # pad with ones, as the standard says
        return bytes(self.out)


def _fdct(block):
    """Separable 8x8 forward DCT-II with JPEG's normalisation."""
    rows = []
    for y in range(8):
        row = block[y * 8:y * 8 + 8]
        rows.append([sum(row[x] * _COS[x][u] for x in range(8))
                     * (0.70710678118654752 if u == 0 else 1.0) / 2.0
                     for u in range(8)])
    out = [0.0] * 64
    for u in range(8):
        column = [rows[y][u] for y in range(8)]
        for v in range(8):
            total = sum(column[y] * _COS[y][v] for y in range(8))
            scale = 0.70710678118654752 if v == 0 else 1.0
            out[v * 8 + u] = total * scale / 2.0
    return out


def _category(value):
    """JPEG's magnitude category: how many bits the value needs."""
    magnitude = abs(value)
    size = 0
    while magnitude:
        size += 1
        magnitude >>= 1
    return size


def _encode_block(writer, coefficients, previous_dc, dc_codes, ac_codes):
    """One 8x8 block, already quantised, in zigzag order."""
    zigzag = [coefficients[i] for i in _ZIGZAG]
    diff = zigzag[0] - previous_dc
    size = _category(diff)
    writer.write(*dc_codes[size])
    if size:
        writer.write(diff if diff > 0 else diff + (1 << size) - 1, size)
    run = 0
    for index in range(1, 64):
        value = zigzag[index]
        if value == 0:
            run += 1
            continue
        while run > 15:
            writer.write(*ac_codes[0xF0])
            run -= 16
        size = _category(value)
        # The standard AC tables stop at category 10, and an 8-bit image
        # through an unquantised DCT cannot exceed it: the largest AC
        # coefficient any input can produce is about 909. If this ever fires,
        # the fixture has grown a quantisation table that scales coefficients
        # up, and the tables have to grow with it.
        assert size <= 10, "AC coefficient %d needs a longer code" % value
        writer.write(*ac_codes[(run << 4) | size])
        writer.write(value if value > 0 else value + (1 << size) - 1, size)
        run = 0
    if run:
        writer.write(*ac_codes[0x00])
    return zigzag[0]


def _dht(table_class, table_id, bits, values):
    body = bytes([(table_class << 4) | table_id]) + bytes(bits) + bytes(values)
    return b"\xff\xc4" + struct.pack(">H", len(body) + 2) + body


def jpeg(width, height, pixel):
    """A baseline 4:4:4 JPEG of `pixel(x, y) -> (r, g, b)`."""
    if width <= 0 or height <= 0:
        raise ValueError("a JPEG needs a positive size")
    planes = ([], [], [])
    for y in range(height):
        for x in range(width):
            r, g, b = pixel(x, y)
            planes[0].append(0.299 * r + 0.587 * g + 0.114 * b)
            planes[1].append(128 - 0.168736 * r - 0.331264 * g + 0.5 * b)
            planes[2].append(128 + 0.5 * r - 0.418688 * g - 0.081312 * b)

    writer = _BitWriter()
    last_dc = [0, 0, 0]
    for block_y in range(0, height, 8):
        for block_x in range(0, width, 8):
            for component in range(3):
                plane = planes[component]
                block = []
                for row in range(8):
                    # Edge blocks repeat the last real row and column, which
                    # is what an encoder does and what stops a border of
                    # ringing along the right and bottom edges.
                    sy = min(block_y + row, height - 1)
                    for col in range(8):
                        sx = min(block_x + col, width - 1)
                        block.append(plane[sy * width + sx] - 128.0)
                quantised = [int(round(value)) for value in _fdct(block)]
                dc_codes = _DC_LUMA_CODES if component == 0 \
                    else _DC_CHROMA_CODES
                ac_codes = _AC_LUMA_CODES if component == 0 \
                    else _AC_CHROMA_CODES
                last_dc[component] = _encode_block(writer, quantised,
                                                   last_dc[component],
                                                   dc_codes, ac_codes)
    scan = writer.flush()

    quant = b"\xff\xdb" + struct.pack(">H", 3 + 64) + b"\x00" + bytes([1] * 64)
    sof = b"\xff\xc0" + struct.pack(">HBHHB", 8 + 3 * 3, 8, height, width, 3)
    for component in (1, 2, 3):
        sof += bytes((component, 0x11, 0))
    sos = b"\xff\xda" + struct.pack(">HB", 6 + 2 * 3, 3)
    for component in (1, 2, 3):
        # Luma reads table 0, both chroma components table 1.
        sos += bytes((component, 0x00 if component == 1 else 0x11))
    sos += bytes((0, 63, 0))
    app0 = b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00" \
        + bytes((1, 1, 0, 0, 1, 0, 1, 0, 0))
    return (b"\xff\xd8" + app0 + quant
            + _dht(0, 0, _DC_LUMA_BITS, _DC_VALUES)
            + _dht(0, 1, _DC_CHROMA_BITS, _DC_VALUES)
            + _dht(1, 0, _AC_LUMA_BITS, _AC_LUMA_VALUES)
            + _dht(1, 1, _AC_CHROMA_BITS, _AC_CHROMA_VALUES)
            + sof + sos + scan + b"\xff\xd9")


def strip_huffman_tables(image):
    """The same JPEG in the abbreviated format: every DHT segment removed.

    Motion JPEG files in the wild do this, because the tables are the same in
    every frame of a clip. A decoder is expected to supply the standard ones.
    """
    out = bytearray(image[:2])
    pos = 2
    while pos + 4 <= len(image):
        marker = image[pos + 1]
        if marker == 0xDA:                  # start of scan: the rest is data
            out += image[pos:]
            return bytes(out)
        length = struct.unpack(">H", image[pos + 2:pos + 4])[0]
        if marker != 0xC4:
            out += image[pos:pos + 2 + length]
        pos += 2 + length
    return bytes(out)


MJPG = int.from_bytes(b"MJPG", "little")


def mjpeg_avi(frames, width, height, fps=25.0, handler="MJPG", **kwargs):
    """An AVI whose video stream is Motion JPEG."""
    return avi(frames, width, height, fps=fps, compression=MJPG,
               handler=handler, **kwargs)


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


def _visual_sample_entry(codec, width, height, depth=24):
    """A VisualSampleEntry: 78 bytes of it, laid out the way the spec does."""
    body = (b"\x00" * 6                       # reserved
            + struct.pack(">H", 1)            # data reference index
            + b"\x00" * 16                    # pre_defined / reserved
            + struct.pack(">HH", width, height)
            + struct.pack(">II", 72 << 16, 72 << 16)
            + struct.pack(">I", 0)            # reserved
            + struct.pack(">H", 1)            # frame count
            + b"\x00" * 32                    # compressor name
            + struct.pack(">H", depth)
            + struct.pack(">h", -1))          # pre_defined
    assert len(body) == 78, len(body)
    return struct.pack(">I", 8 + len(body)) + codec.encode("latin-1") + body


def _stts(durations):
    """A time-to-sample table, run-length encoded the way the spec asks.

    Passing the per-sample durations rather than a rate is what lets a fixture
    be variable frame rate, which is the case a player that divides by an
    average gets wrong and never notices.
    """
    runs = []
    for delta in durations:
        if runs and runs[-1][1] == delta:
            runs[-1][0] += 1
        else:
            runs.append([1, delta])
    return _box("stts", struct.pack(">II", 0, len(runs))
                + b"".join(struct.pack(">II", count, delta)
                           for count, delta in runs))


def _descriptor(tag, payload, long_length=False):
    """One MPEG-4 descriptor: a tag, a length, a payload.

    The length is seven bits a byte with the top bit meaning "another byte
    follows", so the same number has four legal spellings. `long_length`
    writes the four-byte one, which is what QuickTime writes and which a
    reader that assumes a single byte gets wrong by three.
    """
    assert len(payload) < 128, "the fixture's descriptors are all short"
    if long_length:
        length = bytes((0x80, 0x80, 0x80, len(payload)))
    else:
        length = bytes((len(payload),))
    return bytes((tag,)) + length + payload


def esds(asc, object_type=0x40, long_lengths=False):
    """An `esds` box carrying `asc` as its DecoderSpecificInfo.

    The whole descriptor chain a real file has -- ES_Descriptor, then a
    DecoderConfigDescriptor whose objectTypeIndication says which codec, then
    the config the decoder is actually built from -- because the demuxer has
    to walk all three and a fixture that skipped a layer would not test that.
    """
    dsi = _descriptor(0x05, asc, long_lengths)
    config = (bytes((object_type,)) + b"\x15\x00\x00\x00"
              + struct.pack(">II", 0, 0) + dsi)
    dcd = _descriptor(0x04, config, long_lengths)
    sl = _descriptor(0x06, b"\x02", long_lengths)
    es = _descriptor(0x03, struct.pack(">HB", 1, 0) + dcd + sl, long_lengths)
    return _box("esds", b"\x00\x00\x00\x00" + es)


def audio_sample_entry(codec="mp4a", channels=2, sample_rate=44100, extra=b"",
                       version=0):
    """An AudioSampleEntry in QuickTime sound description version 0, 1 or 2.

    Version 0 is what an MP4 muxer writes. The other two append fields before
    the child boxes -- sixteen bytes and thirty-six -- so a parser that does
    not know about them looks for `esds` in the middle of a number.
    """
    if version == 2:
        # Version 2 pins the old fixed-point field at 1.0 and puts the real
        # rate in a float64 further down, which is the only way the format
        # can say 44100.0 exactly rather than nearly.
        fixed = struct.pack(">HH", 1, 0)
    else:
        fixed = struct.pack(">HH", int(sample_rate) & 0xFFFF, 0)
    body = (b"\x00" * 6                       # reserved
            + struct.pack(">H", 1)            # data reference index
            + struct.pack(">HHI", version, 0, 0)   # version, revision, vendor
            + struct.pack(">HHHH", channels, 16, 0, 0)
            + fixed)
    assert len(body) == 28, len(body)
    if version == 1:
        body += struct.pack(">IIII", 1024, 0, 0, 2)
    elif version == 2:
        body += (struct.pack(">I", 72) + struct.pack(">d", float(sample_rate))
                 + struct.pack(">I", channels) + b"\x7f\x00\x00\x00"
                 + struct.pack(">IIII", 32, 1, 0, 1024))
    body += extra
    return struct.pack(">I", 8 + len(body)) + codec.encode("latin-1") + body


def _soun_trak(offsets, sizes, sample_rate=44100, channels=2,
               asc=b"\x12\x10", codec="mp4a", object_type=0x40,
               timescale=None, durations=None, samples_per_chunk=1,
               entry_version=0, in_wave=False, long_lengths=False):
    """A `soun` trak over packets already placed in the file.

    `offsets` are where those packets ended up, so the chunk table this
    writes points at real bytes and a test can check the demuxer found them.
    """
    count = len(sizes)
    if timescale is None:
        timescale = sample_rate
    if durations is None:
        # 1024 samples a frame is what AAC codes, and the last frame of a
        # real file is often shorter than the rest.
        durations = [1024] * count
    config = esds(asc, object_type, long_lengths) if asc is not None else b""
    if in_wave and config:
        # QuickTime hides the same box one level down, inside `wave`.
        config = _box("wave", config)
    stsd = _box("stsd", struct.pack(">II", 0, 1)
                + audio_sample_entry(codec, channels, sample_rate, config,
                                     entry_version))
    chunk_offsets = [offsets[i] for i in range(count)
                     if i % samples_per_chunk == 0]
    stsc = _box("stsc", struct.pack(">II", 0, 1)
                + struct.pack(">III", 1, samples_per_chunk, 1))
    stsz = _box("stsz", struct.pack(">III", 0, 0, count)
                + b"".join(struct.pack(">I", size) for size in sizes))
    stco = _box("stco", struct.pack(">II", 0, len(chunk_offsets))
                + b"".join(struct.pack(">I", o) for o in chunk_offsets))
    tables = stsd + _stts(durations) + stsc + stsz + stco
    mdhd = _box("mdhd", struct.pack(">BBBBIIIIHH", 0, 0, 0, 0, 0, 0,
                                    timescale, sum(durations), 0x55C4, 0))
    hdlr = _box("hdlr", struct.pack(">I", 0) + b"\x00\x00\x00\x00soun"
                + b"\x00" * 12 + b"\x00")
    smhd = _box("smhd", struct.pack(">IHH", 0, 0, 0))
    minf = _box("minf", smhd + _box("stbl", tables))
    tkhd = _box("tkhd", struct.pack(">BBBB", 0, 0, 0, 7)
                + struct.pack(">IIIII", 0, 0, 2, 0, 0)
                + b"\x00" * 52 + struct.pack(">II", 0, 0))
    return _box("trak", tkhd + _box("mdia", mdhd + hdlr + minf))


def mp4_audio(packets, brand=b"isom", movie_timescale=600, **kwargs):
    """An MP4 whose only track is sound: real tables over real packets.

    `mdat` comes before `moov`, as it does in `mov()` above and for the same
    reason -- the offsets in `stco` then point into a part of the file that
    was written before the table that describes it, which is where an offset
    bug shows up.
    """
    ftyp = _box("ftyp", brand + struct.pack(">I", 512) + brand)
    mdat = _box("mdat", b"".join(packets))
    base = len(ftyp) + 8
    offsets = []
    running = base
    for payload in packets:
        offsets.append(running)
        running += len(payload)
    trak = _soun_trak(offsets, [len(p) for p in packets], **kwargs)
    duration = sum(kwargs.get("durations") or [1024] * len(packets))
    rate = kwargs.get("sample_rate", 44100)
    ticks = int(duration * movie_timescale / (rate or 1))
    mvhd = _box("mvhd", struct.pack(">BBBBIIII", 0, 0, 0, 0, 0, 0,
                                    movie_timescale, ticks) + b"\x00" * 80)
    return ftyp + mdat + _box("moov", mvhd + trak)


def mov(frames, width, height, codec="jpeg", fps=25.0, depth=24,
        timescale=600, brand=b"qt  ", samples_per_chunk=1, sync=None,
        wide_offsets=False, durations=None, audio=None):
    """A QuickTime/ISO file over the given list of sample payloads.

    Real sample tables, and `mdat` is written before `moov` so the chunk
    offsets in `stco` are offsets into a file that is still being built --
    which is exactly the ordering that makes an offset bug visible.

    `audio` adds a second, sound, track: a dict of `packets` plus whatever
    `_soun_trak` takes. Its packets go into the same `mdat` after the video's,
    which is where a demuxer that reads the wrong track's chunk table lands.
    """
    delta = int(round(timescale / fps))
    count = len(frames)
    if durations is None:
        durations = [delta] * count
    audio = dict(audio) if audio else None
    audio_packets = list(audio.pop("packets")) if audio else []
    ftyp = _box("ftyp", brand + struct.pack(">I", 512) + brand)
    mdat_payload = b"".join(frames) + b"".join(audio_packets)
    mdat = _box("mdat", mdat_payload)
    # Samples live at their own offset inside mdat, whose payload starts
    # eight bytes into the box, which itself starts after ftyp.
    base = len(ftyp) + 8

    chunk_offsets = []
    offsets = []
    running = base
    for i, payload in enumerate(frames):
        if i % samples_per_chunk == 0:
            chunk_offsets.append(running)
        offsets.append(running)
        running += len(payload)

    stsd = _box("stsd", struct.pack(">II", 0, 1)
                + _visual_sample_entry(codec, width, height, depth))
    stts = _stts(durations)
    stsc = _box("stsc", struct.pack(">II", 0, 1)
                + struct.pack(">III", 1, samples_per_chunk, 1))
    stsz = _box("stsz", struct.pack(">III", 0, 0, count)
                + b"".join(struct.pack(">I", len(f)) for f in frames))
    if wide_offsets:
        stco = _box("co64", struct.pack(">II", 0, len(chunk_offsets))
                    + b"".join(struct.pack(">Q", o) for o in chunk_offsets))
    else:
        stco = _box("stco", struct.pack(">II", 0, len(chunk_offsets))
                    + b"".join(struct.pack(">I", o) for o in chunk_offsets))
    tables = stsd + stts + stsc + stsz + stco
    if sync is not None:
        tables += _box("stss", struct.pack(">II", 0, len(sync))
                       + b"".join(struct.pack(">I", n) for n in sync))

    duration_ticks = sum(durations)
    mdhd = _box("mdhd", struct.pack(">BBBBIIIIHH", 0, 0, 0, 0, 0, 0,
                                    timescale, duration_ticks, 0x55C4, 0))
    hdlr = _box("hdlr", struct.pack(">I", 0) + b"\x00\x00\x00\x00vide"
                + b"\x00" * 12 + b"\x00")
    minf = _box("minf", _box("stbl", tables))
    mdia = _box("mdia", mdhd + hdlr + minf)
    tkhd = _box("tkhd", struct.pack(">BBBB", 0, 0, 0, 7)
                + struct.pack(">IIIII", 0, 0, 1, 0, 0)
                + b"\x00" * 52
                + struct.pack(">II", width << 16, height << 16))
    trak = _box("trak", tkhd + mdia)
    if audio is not None:
        audio_offsets = []
        for payload in audio_packets:
            audio_offsets.append(running)
            running += len(payload)
        trak += _soun_trak(audio_offsets,
                           [len(p) for p in audio_packets], **audio)
    mvhd = _box("mvhd", struct.pack(">BBBBIIII", 0, 0, 0, 0, 0, 0, timescale,
                                    duration_ticks)
                + b"\x00" * 80)
    moov = _box("moov", mvhd + trak)
    assert len(ftyp) + 8 == base and offsets[0] == base
    return ftyp + mdat + moov


def quicktime_raw_frame(width, height, pixel, depth=24):
    """One `raw ` sample: top-down, and RGB rather than the DIB's BGR."""
    out = bytearray()
    for y in range(height):
        for x in range(width):
            r, g, b = pixel(x, y)
            if depth == 32:
                out += bytes((255, r, g, b))
            else:
                out += bytes((r, g, b))
    return bytes(out)


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


def webm(width, height, duration, codec="V_VP9", timecode_scale=1000000,
         audio_codec=None):
    """Enough Matroska for the prober: Info duration and Tracks dimensions.

    `audio_codec` adds a second TrackEntry naming a sound codec, which is all
    the audio prober reads out of a WebM -- there is no demuxer for it here.
    """
    header = b"\x1a\x45\xdf\xa3" + struct.pack(">I", 5 | 0x10000000) \
        + b"\x42\x86\x81\x01\x00"
    info = _ebml(0x1549A966,
                 _ebml_uint(0x2AD7B1, timecode_scale)
                 + _ebml(0x4489, struct.pack(">d",
                                             duration * 1e9 / timecode_scale)))
    video = _ebml(0xE0, _ebml_uint(0xB0, width) + _ebml_uint(0xBA, height))
    entry = _ebml(0xAE, _ebml(0x86, codec.encode("latin-1")) + video)
    if audio_codec is not None:
        entry += _ebml(0xAE, _ebml(0x86, audio_codec.encode("latin-1")))
    tracks = _ebml(0x1654AE6B, entry)
    segment = _ebml(0x18538067, info + tracks)
    return header + segment
