"""Video containers and video codecs, decoded to raw RGBA.

This is the bytes half of video: it turns a file into a sequence of frames
with times on them. It knows nothing about clocks, threads, layout or the
screen -- `media.py` does that -- so everything here is a pure function of the
input file and can be tested by reading a byte array.

What is actually decoded to pixels:

  AVI (RIFF) with `BI_RGB`   uncompressed 8/24/32-bit DIB frames
  AVI (RIFF) with `BI_RLE8`  Microsoft's 8-bit run-length codec, including
                             the delta frames that make it inter-frame
  AVI (RIFF) with `MJPG`     Motion JPEG: every frame is a whole JPEG
  MOV/MP4 with `jpeg`/`mjpa` the same codec in QuickTime's container
  MOV/MP4 with `raw `/`png ` uncompressed and PNG-per-frame QuickTime video
  a bare `.mjpeg` stream      JPEGs end to end with no container at all

Motion JPEG is the one that makes this a video player rather than a
demonstration. It is here because the expensive half of it was already
written: `imagecodec.decode_jpeg` is our own baseline-and-progressive JPEG
decoder in Rust, and it decodes a 320x240 picture in about a millisecond, so
a frame costs a couple of percent of one frame's worth of time. The codec
below is the cheap half -- work out where each JPEG starts and ends, hand it
over, and put the pixels where the compositor expects them.

Everything else in this module *reads* the file and refuses honestly: an MP4
carrying H.264, or a WebM carrying VP9, is walked far enough to report its
dimensions, duration and codec name, and then declines, because saying
"1280x720, H.264, 4.0s, no decoder" is useful and pretending to play it is
not. See docs/media.md.

Three rules the parsers hold to, because the file came from a stranger:

  * every read is bounds-checked and raises `MediaError`, never `IndexError`
    and never a silent short read;
  * every walk over a chunk list is bounded, both by the remaining bytes and
    by a hard iteration cap, so a file whose sizes point at themselves ends
    the parse instead of the process;
  * a frame's pixel count is capped before any buffer is allocated.

The frame model is `VideoFrame`: an index, a presentation time in seconds, a
duration, and `rgba` -- 4 bytes per pixel, the same buffer shape
`canvas.PhotoImage` and `raster.Surface.blit_rgba` already consume, so a
decoded frame goes to the screen without a conversion step.
"""

import struct

from . import h264
from . import imagecodec
from .imagecodec import MAX_PIXELS

__all__ = ["MediaError", "VideoFrame", "VideoTrack", "MediaInfo",
           "open_video", "probe", "sniff", "MAX_FRAMES", "MAX_CHUNKS",
           "MJPEG_DEFAULT_FPS"]


class MediaError(Exception):
    """A media file we cannot use: malformed, truncated, or a codec we do
    not have. One exception type, because every caller does the same thing
    with it -- show the poster and say why."""


# Bounds. A file is allowed to be big; it is not allowed to be unbounded.
MAX_FRAMES = 100000          # frames in one track
MAX_CHUNKS = 2000000         # RIFF/box/EBML elements walked before we stop
MAX_FRAME_BYTES = 64 << 20   # one compressed packet
MAX_DEPTH = 12               # container nesting


# -- reading -----------------------------------------------------------------

class _Reader:
    """A cursor over a byte string that refuses to read past the end.

    Every field in every format below is read through this. The point is not
    convenience: it is that a truncated file produces one `MediaError` at the
    first short read rather than a struct.error, an IndexError, or -- worst --
    a plausible-looking value taken from whatever followed in memory.
    """

    __slots__ = ("data", "pos", "end")

    def __init__(self, data, pos=0, end=None):
        self.data = data
        self.pos = pos
        self.end = len(data) if end is None else min(end, len(data))

    def remaining(self):
        return max(0, self.end - self.pos)

    def take(self, count):
        if count < 0:
            raise MediaError("negative read")
        if self.remaining() < count:
            raise MediaError("truncated: wanted %d bytes, %d left"
                             % (count, self.remaining()))
        out = self.data[self.pos:self.pos + count]
        self.pos += count
        return out

    def skip(self, count):
        if count < 0 or self.remaining() < count:
            raise MediaError("truncated: cannot skip %d bytes" % count)
        self.pos += count

    def u8(self):
        return self.take(1)[0]

    def u16le(self):
        return struct.unpack("<H", self.take(2))[0]

    def u32le(self):
        return struct.unpack("<I", self.take(4))[0]

    def u16be(self):
        return struct.unpack(">H", self.take(2))[0]

    def u32be(self):
        return struct.unpack(">I", self.take(4))[0]

    def u64be(self):
        return struct.unpack(">Q", self.take(8))[0]

    def fourcc(self):
        raw = self.take(4)
        return raw.decode("latin-1")


def _check_size(width, height):
    if width <= 0 or height <= 0:
        raise MediaError("bad frame size %dx%d" % (width, height))
    if width * height > MAX_PIXELS:
        raise MediaError("frame of %dx%d exceeds the %d pixel cap"
                         % (width, height, MAX_PIXELS))


# -- frames ------------------------------------------------------------------

class VideoFrame:
    """One decoded picture and the instant it belongs at.

    `pts` is seconds from the start of the track, and it is the only thing
    the scheduler looks at -- deliberately, because counting frames is how a
    player drifts.
    """

    __slots__ = ("index", "pts", "duration", "width", "height", "rgba")

    def __init__(self, index, pts, duration, width, height, rgba):
        self.index = index
        self.pts = pts
        self.duration = duration
        self.width = width
        self.height = height
        self.rgba = rgba

    @property
    def end(self):
        return self.pts + self.duration

    def __repr__(self):
        return ("<VideoFrame %d %dx%d @%.3fs+%.3f>"
                % (self.index, self.width, self.height, self.pts,
                   self.duration))


class MediaInfo:
    """What a file says about itself, whether or not we can decode it.

    `supported` is the honest bit. A `<video>` pointing at an MP4 gets a
    MediaInfo with the right width, height and duration, `supported` False,
    and a `reason` fit to show a user.
    """

    __slots__ = ("container", "codec", "width", "height", "duration",
                 "frame_count", "supported", "reason")

    def __init__(self, container, codec="", width=0, height=0, duration=0.0,
                 frame_count=0, supported=False, reason=""):
        self.container = container
        self.codec = codec
        self.width = width
        self.height = height
        self.duration = duration
        self.frame_count = frame_count
        self.supported = supported
        self.reason = reason

    def __repr__(self):
        return ("<MediaInfo %s/%s %dx%d %.3fs %d frames %s>"
                % (self.container, self.codec or "?", self.width, self.height,
                   self.duration, self.frame_count,
                   "ok" if self.supported else "unsupported"))


# -- codecs ------------------------------------------------------------------

class _Codec:
    """A stateful decoder. Stateful because inter-frame codecs exist: RLE8
    delta frames are diffs against the picture already on screen, so the
    decoder owns the previous frame and a caller that wants frame N must have
    handed it every frame since the last keyframe. `VideoTrack.frame()`
    enforces that; the decoder just trusts its input order."""

    def reset(self):
        raise NotImplementedError

    def decode(self, packet, keyframe):
        """Return RGBA bytes for one packet, or None if the packet carries no
        new picture (an AVI drop frame, which means 'hold the last one')."""
        raise NotImplementedError


def _palette_rgba(raw, count):
    """A BITMAPINFOHEADER colour table: BGRX quads."""
    table = []
    for i in range(count):
        off = i * 4
        if off + 4 > len(raw):
            table.append((0, 0, 0, 255))
            continue
        b, g, r = raw[off], raw[off + 1], raw[off + 2]
        table.append((r, g, b, 255))
    return table


class _RawDib(_Codec):
    """`BI_RGB`: a bottom-up (or top-down) DIB, rows padded to 4 bytes.

    Uncompressed, so every frame is a keyframe and the decoder is stateless
    apart from the size it was configured with. This is the codec that proves
    the pipeline: if a raw AVI plays smoothly, everything that is left is
    decode cost.
    """

    def __init__(self, width, height, bit_count, palette, top_down):
        _check_size(width, height)
        if bit_count not in (8, 24, 32):
            raise MediaError("BI_RGB with %d bits per pixel is not supported"
                             % bit_count)
        self.width = width
        self.height = height
        self.bit_count = bit_count
        self.palette = palette
        self.top_down = top_down
        self.stride = ((width * bit_count + 31) // 32) * 4

    def reset(self):
        pass

    def decode(self, packet, keyframe):
        if not packet:
            return None
        need = self.stride * self.height
        if len(packet) < need:
            raise MediaError("BI_RGB frame short: %d bytes, need %d"
                             % (len(packet), need))
        out = bytearray(self.width * self.height * 4)
        for y in range(self.height):
            src_row = y if self.top_down else self.height - 1 - y
            base = src_row * self.stride
            dst = y * self.width * 4
            if self.bit_count == 24:
                for x in range(self.width):
                    s = base + x * 3
                    d = dst + x * 4
                    out[d] = packet[s + 2]
                    out[d + 1] = packet[s + 1]
                    out[d + 2] = packet[s]
                    out[d + 3] = 255
            elif self.bit_count == 32:
                for x in range(self.width):
                    s = base + x * 4
                    d = dst + x * 4
                    out[d] = packet[s + 2]
                    out[d + 1] = packet[s + 1]
                    out[d + 2] = packet[s]
                    out[d + 3] = 255
            else:
                table = self.palette
                for x in range(self.width):
                    r, g, b, a = table[packet[base + x]]
                    d = dst + x * 4
                    out[d] = r
                    out[d + 1] = g
                    out[d + 2] = b
                    out[d + 3] = a
        return bytes(out)


class _Rle8(_Codec):
    """`BI_RLE8`, Microsoft's 8-bit run-length codec.

    The opcode stream is pairs of bytes. A leading non-zero byte is a run of
    that many copies of the next index. A leading zero is an escape: 0 ends
    the row, 1 ends the bitmap, 2 introduces a two-byte (dx, dy) delta that
    leaves the skipped pixels *unchanged*, and 3 or more is an absolute run of
    that many literal indices, padded to an even length.

    The delta and end-of-bitmap escapes are what make this inter-frame: an
    encoder emits a frame that only touches what moved and leaves the rest of
    the previous picture in place. So the decoder keeps an index plane between
    calls and a non-keyframe starts from it, which is exactly the property a
    real codec has and the reason `VideoTrack.frame()` has to decode forward
    from a keyframe rather than jumping.
    """

    def __init__(self, width, height, palette, top_down):
        _check_size(width, height)
        self.width = width
        self.height = height
        self.palette = palette
        self.top_down = top_down
        self.plane = None

    def reset(self):
        self.plane = None

    def decode(self, packet, keyframe):
        if keyframe or self.plane is None:
            plane = bytearray(self.width * self.height)
        else:
            plane = bytearray(self.plane)
        if packet:
            self._run(packet, plane)
        self.plane = plane
        return self._to_rgba(plane)

    def _row_base(self, y):
        # RLE8 rows arrive bottom-up unless the header said otherwise.
        row = y if self.top_down else self.height - 1 - y
        return row * self.width

    def _run(self, packet, plane):
        reader = _Reader(packet)
        x = 0
        y = 0
        guard = 0
        while reader.remaining() >= 2:
            guard += 1
            if guard > MAX_CHUNKS:
                raise MediaError("RLE8 opcode stream does not terminate")
            count = reader.u8()
            value = reader.u8()
            if count:
                if y >= self.height:
                    raise MediaError("RLE8 run past the bottom of the frame")
                if x + count > self.width:
                    raise MediaError("RLE8 run past the end of row %d" % y)
                base = self._row_base(y) + x
                for i in range(count):
                    plane[base + i] = value
                x += count
                continue
            if value == 0:          # end of line
                x = 0
                y += 1
            elif value == 1:        # end of bitmap
                return
            elif value == 2:        # delta
                dx = reader.u8()
                dy = reader.u8()
                x += dx
                y += dy
                if x > self.width or y > self.height:
                    raise MediaError("RLE8 delta leaves the frame")
            else:                   # absolute run of literals
                if y >= self.height:
                    raise MediaError("RLE8 literals past the bottom")
                if x + value > self.width:
                    raise MediaError("RLE8 literals past the end of row %d" % y)
                literals = reader.take(value)
                base = self._row_base(y) + x
                plane[base:base + value] = literals
                x += value
                if value & 1:
                    reader.skip(1)  # runs are word-aligned

    def _to_rgba(self, plane):
        out = bytearray(len(plane) * 4)
        table = self.palette
        for i, index in enumerate(plane):
            r, g, b, a = table[index]
            d = i * 4
            out[d] = r
            out[d + 1] = g
            out[d + 2] = b
            out[d + 3] = a
        return bytes(out)


# -- JPEG, as a video codec --------------------------------------------------

# JPEG markers we have to recognise to walk a frame without decoding it.
_SOI = 0xD8
_EOI = 0xD9
_SOS = 0xDA
_DHT = 0xC4

# Standard Huffman tables, ISO/IEC 10918-1 Annex K.3. A JPEG file normally
# carries its own, but Motion JPEG has a long tradition of leaving them out:
# the tables are identical in every frame of a clip, so an encoder that is
# writing thousands of frames saves a few hundred bytes each by relying on
# the decoder to already know them. That is legal for the "abbreviated"
# format and it is what a great many camera AVIs and QuickTime .movs do, so a
# frame with no DHT in it gets these spliced in ahead of its scan rather than
# being called corrupt. Written out as (class, id, counts, symbols) because
# that is the order they go into the segment.
_STD_DC_LUMA_BITS = (0, 1, 5, 1, 1, 1, 1, 1, 1, 0, 0, 0, 0, 0, 0, 0)
_STD_DC_CHROMA_BITS = (0, 3, 1, 1, 1, 1, 1, 1, 1, 1, 1, 0, 0, 0, 0, 0)
_STD_DC_VALUES = tuple(range(12))

_STD_AC_LUMA_BITS = (0, 2, 1, 3, 3, 2, 4, 3, 5, 5, 4, 4, 0, 0, 1, 0x7D)
_STD_AC_LUMA_VALUES = (
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

_STD_AC_CHROMA_BITS = (0, 2, 1, 2, 4, 4, 3, 4, 7, 5, 4, 4, 0, 1, 2, 0x77)
_STD_AC_CHROMA_VALUES = (
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


def _dht_segment(table_class, table_id, bits, values):
    """One DHT marker segment: length, class/id nibble pair, 16 counts, the
    symbols. Assembled rather than pasted as a blob so the tables above stay
    readable as tables."""
    assert sum(bits) == len(values), "Huffman counts do not match the symbols"
    body = bytes([(table_class << 4) | table_id]) + bytes(bits) + bytes(values)
    return b"\xff\xc4" + struct.pack(">H", len(body) + 2) + body


DEFAULT_HUFFMAN_TABLES = (
    _dht_segment(0, 0, _STD_DC_LUMA_BITS, _STD_DC_VALUES)
    + _dht_segment(0, 1, _STD_DC_CHROMA_BITS, _STD_DC_VALUES)
    + _dht_segment(1, 0, _STD_AC_LUMA_BITS, _STD_AC_LUMA_VALUES)
    + _dht_segment(1, 1, _STD_AC_CHROMA_BITS, _STD_AC_CHROMA_VALUES))


def _jpeg_scan(data, start=0):
    """Walk one JPEG image from `start` and return (sos_at, end, size).

    `sos_at` is the offset of the 0xFF that begins the first scan header, or
    None when the image has no scan; `end` is one past the image's last byte;
    `size` is the (width, height) the frame header declared, or None.

    This has to be a real marker walk rather than a search for `FF D9`,
    because JPEG carries JPEGs inside itself: an EXIF thumbnail in an APP1
    segment is a complete image with its own end-of-image marker, and a
    scanner that stops at the first one it sees cuts every such frame in
    half. Inside a scan the rule is different again -- there is no length --
    so entropy-coded bytes are scanned for the next 0xFF that is neither a
    stuffed zero nor a restart marker.
    """
    n = len(data)
    if start + 2 > n or data[start] != 0xFF or data[start + 1] != _SOI:
        raise MediaError("MJPEG frame does not start with an SOI marker")
    pos = start + 2
    sos_at = None
    size = None
    guard = 0
    while pos < n:
        guard += 1
        if guard > MAX_CHUNKS:
            raise MediaError("JPEG marker list does not terminate")
        if data[pos] != 0xFF:
            raise MediaError("JPEG marker expected at offset %d" % pos)
        while pos < n and data[pos] == 0xFF:
            pos += 1            # fill bytes are legal between markers
        if pos >= n:
            raise MediaError("JPEG ended inside a marker")
        marker = data[pos]
        pos += 1
        if marker == _EOI:
            return sos_at, pos, size
        if marker == _SOI or 0xD0 <= marker <= 0xD7 or marker == 0x01:
            continue            # standalone markers, no payload
        if pos + 2 > n:
            raise MediaError("JPEG segment length runs off the end")
        length = struct.unpack(">H", data[pos:pos + 2])[0]
        if length < 2 or pos + length > n:
            raise MediaError("JPEG segment of %d bytes at %d" % (length, pos))
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC) \
                and size is None and length >= 7:
            size = (struct.unpack(">H", data[pos + 5:pos + 7])[0],
                    struct.unpack(">H", data[pos + 3:pos + 5])[0])
        if marker == _SOS:
            if sos_at is None:
                sos_at = pos - 2
            pos += length
            pos = _jpeg_entropy_end(data, pos)
            continue
        pos += length
    # Truncated: no EOI. Real capture files are cut mid-write often enough
    # that the last frame of an otherwise good clip is worth decoding anyway,
    # so hand back what there is and let the decoder judge it.
    return sos_at, n, size


def _jpeg_entropy_end(data, pos):
    """Skip entropy-coded data: everything up to the next real marker."""
    n = len(data)
    while pos < n:
        if data[pos] != 0xFF:
            pos += 1
            continue
        if pos + 1 >= n:
            return n
        nxt = data[pos + 1]
        if nxt == 0x00 or 0xD0 <= nxt <= 0xD7 or nxt == 0xFF:
            pos += 2 if nxt != 0xFF else 1
            continue
        return pos
    return n


def _jpeg_frame(packet):
    """One MJPEG packet, turned into a JPEG a decoder will accept.

    Two fixups, both of which real files need. The packet may carry padding
    on either side of the image -- AVI pads chunks to even lengths and some
    muxers pad rather more generously -- so the image is cut out at its own
    markers. And a frame in the abbreviated format has no Huffman tables in
    it, in which case the standard ones are spliced in immediately before the
    scan, which is exactly where a DHT is allowed to appear.
    """
    start = packet.find(b"\xff\xd8\xff")
    if start < 0 or start > 4096:
        raise MediaError("MJPEG packet contains no JPEG image")
    sos_at, end, _size = _jpeg_scan(packet, start)
    image = packet[start:end]
    if sos_at is None:
        raise MediaError("MJPEG frame has no scan in it")
    if b"\xff\xc4" in image[:sos_at - start]:
        return image
    cut = sos_at - start
    return image[:cut] + DEFAULT_HUFFMAN_TABLES + image[cut:]


class _Mjpeg(_Codec):
    """Motion JPEG: one whole JPEG per frame, decoded by our own decoder.

    Stateless, because every frame is a keyframe -- which is the property
    that makes MJPEG the pleasant codec to seek in and the wasteful one to
    store. There is no motion compensation and no reference frame, so
    `frame(n)` costs exactly one decode wherever the playhead came from, and
    `keyframe_before(n)` is `n`.

    Frames whose own size disagrees with the size the container declared are
    scaled to the container's, because that is the size layout already
    reserved and a buffer that changes shape mid-clip would mean rebuilding
    the retained canvas item. It happens in practice: a field-encoded camera
    AVI stores each field at half height under a full-height header.
    """

    def __init__(self, width, height):
        _check_size(width, height)
        self.width = width
        self.height = height

    def reset(self):
        pass

    def decode(self, packet, keyframe):
        if not packet:
            return None
        image = _jpeg_frame(packet)
        try:
            width, height, rgba = imagecodec.decode_jpeg(image)
        except imagecodec.ImageError as exc:
            raise MediaError("MJPEG frame: %s" % exc)
        if (width, height) != (self.width, self.height):
            _check_size(width, height)
            rgba = imagecodec.resize(rgba, width, height,
                                     self.width, self.height)
        return rgba


class _H264(_Codec):
    """H.264/AVC, decoded by the Fortran decoder in ``fortran/``.

    Phase one of that decoder is I frames only, so the useful question is
    not "is this H.264" but "does this particular stream decode", and the
    only honest way to answer it is to decode a frame. That is what the
    constructor does: it takes the first keyframe, runs it, and refuses
    the whole file with the decoder's own reason if it does not come out.
    Opening a file that plays for two frames and then stops would be worse
    than refusing it -- the poster and a sentence beats a frozen picture.

    Which is also why the sync flags are read before anything is decoded.
    Every ordinary web MP4 starts with an I frame and continues with P
    frames, so trial-decoding frame zero would say yes to a file that then
    freezes on frame one. A track whose every sample is a sync sample is
    the shape this decoder can finish, and it is the shape an all-intra
    encode has; anything else is refused up front and named.

    Stateless between frames, because there is nothing yet that is not a
    keyframe. When inter prediction arrives this class grows a reference
    picture and `reset()` starts meaning something.
    """

    def __init__(self, width, height, extradata, data, samples):
        _check_size(width, height)
        self.width = width
        self.height = height
        inter = sum(1 for _o, _l, keyframe in samples if not keyframe)
        if inter:
            raise MediaError(
                "H.264: %d of this track's %d frames are inter-coded, and "
                "the decoder does I frames only"
                % (inter, len(samples)))
        try:
            self._decoder = h264.Decoder(extradata)
            got_width, got_height, _rgba = self._decoder.decode(
                _first_keyframe(data, samples))
        except h264.H264Error as exc:
            raise MediaError("H.264: %s" % exc)
        # The container and the sequence parameter set can disagree about
        # the picture size, and when they do the SPS is the one that made
        # the pixels. Refusing is right: everything downstream sized itself
        # from the container, and silently handing it a different shape of
        # buffer is how a compositor learns to crash.
        if (got_width, got_height) != (width, height):
            raise MediaError("H.264: the stream is %dx%d but the container "
                             "says %dx%d" % (got_width, got_height,
                                             width, height))

    def reset(self):
        pass

    def decode(self, packet, keyframe):
        if not packet:
            return None
        try:
            _width, _height, rgba = self._decoder.decode(packet)
        except h264.H264Error as exc:
            raise MediaError("H.264 frame: %s" % exc)
        return rgba


# The fourccs that mean H.264. `avc1` and `avc3` are the MP4 spellings and
# differ only in where the parameter sets live -- in the `avcC` box for the
# first, inline in the stream for the second, and the decoder takes both.
# The rest are what AVI muxers wrote before anybody agreed.
H264_FOURCCS = ("avc1", "avc3", "H264", "h264", "X264", "x264", "AVC1")


# fourccs that mean "Motion JPEG". AVI and QuickTime each accumulated their
# own spellings over twenty years and the files are still out there, so they
# are all here; `dmb1` is Matrox's, `AVRn` Avid's, `MJPG`/`jpeg` the two
# common ones.
MJPEG_FOURCCS = ("MJPG", "mjpg", "MJPX", "jpeg", "JPEG", "mjpa", "AVDJ",
                 "dmb1", "AVRn", "ADJV")

# A bare `.mjpeg` stream has nowhere to record a frame rate -- it is JPEGs
# and nothing else -- so one has to be assumed. 25 matches what the webcams
# and capture cards that emit these files usually run at, and `<video>` gives
# a page no way to say otherwise.
MJPEG_DEFAULT_FPS = 25.0


# fourccs we can name but not decode. Naming them is the point: the element
# can say "XVID" instead of "unknown", and the next contributor can see
# exactly where a decoder plugs in.
KNOWN_UNDECODABLE = {
    "XVID": "MPEG-4 ASP: no decoder",
    "DIVX": "MPEG-4 ASP: no decoder",
    "VP80": "VP8: no decoder",
    "VP90": "VP9: no decoder",
    "cvid": "Cinepak: no decoder",
    "msvc": "Microsoft Video 1: no decoder",
    "CRAM": "Microsoft Video 1: no decoder",
}


# -- AVI ---------------------------------------------------------------------

# BITMAPINFOHEADER biCompression values we treat as codec identities.
BI_RGB = 0
BI_RLE8 = 1
BI_RLE4 = 2
BI_BITFIELDS = 3

AVIIF_KEYFRAME = 0x10


class _AviStream:
    def __init__(self):
        self.kind = ""
        self.handler = ""
        self.scale = 1
        self.rate = 0
        self.length = 0
        self.format = b""


def _riff_chunks(reader, depth=0):
    """Yield (fourcc, list_type, payload_reader) for one RIFF chunk level.

    Chunks are word-aligned and carry their own length, which is the whole
    attack surface: a length of 0xFFFFFFFF, or of 0, is what turns a walker
    into a hang. The remaining-bytes check handles the first and the explicit
    advance handles the second.
    """
    if depth > MAX_DEPTH:
        raise MediaError("RIFF nested too deeply")
    seen = 0
    while reader.remaining() >= 8:
        seen += 1
        if seen > MAX_CHUNKS:
            raise MediaError("RIFF chunk list does not terminate")
        tag = reader.fourcc()
        size = reader.u32le()
        if size > reader.remaining():
            # Truncated tail. Common in real files that were cut mid-write;
            # give back what is there rather than failing the whole parse.
            size = reader.remaining()
        start = reader.pos
        list_type = ""
        if tag in ("LIST", "RIFF"):
            if size < 4:
                raise MediaError("%s chunk of %d bytes" % (tag, size))
            list_type = reader.fourcc()
        yield tag, list_type, _Reader(reader.data, reader.pos, start + size)
        reader.pos = start + size
        if size & 1 and reader.remaining():
            reader.pos += 1


def _parse_avi(data):
    top = _Reader(data)
    tag = top.fourcc()
    size = top.u32le()
    form = top.fourcc()
    if tag != "RIFF" or form != "AVI ":
        raise MediaError("not an AVI file")
    body = _Reader(data, top.pos, min(len(data), 8 + max(size, 4)))

    micros_per_frame = 0
    streams = []
    packets = []          # (stream_index, offset, length)
    index_flags = {}      # ordinal within stream -> flags, from idx1
    movi_base = 0

    for tag, list_type, chunk in _riff_chunks(body):
        if tag == "LIST" and list_type == "hdrl":
            for sub, sub_list, sub_chunk in _riff_chunks(chunk, 1):
                if sub == "avih":
                    micros_per_frame = sub_chunk.u32le()
                elif sub == "LIST" and sub_list == "strl":
                    streams.append(_parse_strl(sub_chunk))
        elif tag == "LIST" and list_type == "movi":
            movi_base = chunk.pos
            packets = _scan_movi(chunk)
        elif tag == "idx1":
            index_flags = _parse_idx1(chunk)
    if not streams:
        raise MediaError("AVI has no stream headers")
    return micros_per_frame, streams, packets, index_flags, movi_base


def _parse_strl(chunk):
    stream = _AviStream()
    for tag, _list_type, sub in _riff_chunks(chunk, 2):
        if tag == "strh":
            stream.kind = sub.fourcc()
            stream.handler = sub.fourcc()
            sub.skip(12)            # flags, priority, language, initial frames
            stream.scale = sub.u32le()
            stream.rate = sub.u32le()
            sub.skip(4)             # start
            stream.length = sub.u32le()
        elif tag == "strf":
            stream.format = sub.take(sub.remaining())
    return stream


def _scan_movi(chunk):
    """Every `##dc`/`##db`/`##wb` chunk in the movi list, in file order."""
    out = []
    for tag, list_type, sub in _riff_chunks(chunk, 1):
        if tag == "LIST" and list_type == "rec ":
            out.extend(_scan_movi(sub))
            continue
        if len(tag) != 4 or not tag[:2].isdigit():
            continue
        out.append((int(tag[:2]), tag[2:], sub.pos, sub.remaining()))
        if len(out) > MAX_FRAMES * 4:
            raise MediaError("AVI movi list is implausibly long")
    return out


def _parse_idx1(chunk):
    """idx1 gives us keyframe flags, which is the only thing that makes a
    seek in an inter-frame codec correct."""
    flags = {}
    counts = {}
    guard = 0
    while chunk.remaining() >= 16:
        guard += 1
        if guard > MAX_FRAMES * 4:
            raise MediaError("idx1 is implausibly long")
        tag = chunk.fourcc()
        entry_flags = chunk.u32le()
        chunk.skip(8)
        if len(tag) != 4 or not tag[:2].isdigit():
            continue
        stream = int(tag[:2])
        ordinal = counts.get(stream, 0)
        counts[stream] = ordinal + 1
        flags[(stream, ordinal)] = entry_flags
    return flags


def _bitmapinfo(fmt):
    """Read a BITMAPINFOHEADER and its colour table."""
    reader = _Reader(fmt)
    header_size = reader.u32le()
    width = struct.unpack("<i", reader.take(4))[0]
    height = struct.unpack("<i", reader.take(4))[0]
    reader.skip(2)                      # biPlanes
    bit_count = reader.u16le()
    compression = reader.u32le()
    reader.skip(4)                      # biSizeImage
    reader.skip(8)                      # pixels-per-metre
    reader.skip(8)                      # biClrUsed, biClrImportant
    top_down = height < 0
    height = abs(height)
    if header_size > 40 and len(fmt) > header_size:
        table_at = header_size
    else:
        table_at = 40
    palette = ()
    if bit_count <= 8:
        # Always build all 256 entries, whatever biClrUsed said: an index
        # outside the declared table is a malformed file, and a black pixel
        # is a better answer than a bounds check in the inner loop.
        palette = _palette_rgba(fmt[table_at:], 256)
    return width, height, bit_count, compression, palette, top_down


def _fourcc_of(compression):
    if compression in (BI_RGB, BI_RLE8, BI_RLE4, BI_BITFIELDS):
        return {BI_RGB: "BI_RGB", BI_RLE8: "BI_RLE8", BI_RLE4: "BI_RLE4",
                BI_BITFIELDS: "BI_BITFIELDS"}[compression]
    raw = struct.pack("<I", compression)
    try:
        return raw.decode("ascii")
    except UnicodeDecodeError:
        return "0x%08X" % compression


class VideoTrack:
    """A decodable video stream: frame count, timing, and `frame(i)`.

    Decoding is on demand and single-threaded here. `media.Playback` is what
    runs it ahead of time on a worker; keeping the two apart is what lets the
    tests decode deterministically and the browser decode without stalling.
    """

    def __init__(self, data, info, packets, codec, frame_rate, keyframes,
                 times=None):
        self._data = data
        self.info = info
        self._packets = packets            # (offset, length, keyframe)
        self._codec = codec
        self.frame_rate = frame_rate
        self._keyframes = keyframes
        # Per-frame presentation times, when the container carries them.
        # AVI does not -- it has one rate for the whole stream -- but an MP4's
        # `stts` is a list of durations and is allowed to vary, so a track
        # from that container hands the real times over rather than letting
        # them be recomputed from an average that is right only on average.
        self._times = list(times) if times else None
        self._cursor = -1                  # last index handed to the codec
        self.width = info.width
        self.height = info.height
        self.frame_count = info.frame_count
        self.duration = info.duration
        self.codec_name = info.codec
        self.container = info.container

    def frame_time(self, index):
        if self._times is not None:
            if 0 <= index < len(self._times):
                return self._times[index][0]
            return 0.0
        return index / self.frame_rate if self.frame_rate else 0.0

    def frame_duration(self, index):
        if self._times is not None:
            if 0 <= index < len(self._times):
                return self._times[index][1]
            return 0.0
        return 1.0 / self.frame_rate if self.frame_rate else 0.0

    def index_at(self, seconds):
        """The frame on screen at `seconds`. Bisects the real times when we
        have them, so a seek into a variable-rate file lands on the frame the
        viewer is actually looking at rather than near it."""
        if self._times is None:
            if self.frame_rate <= 0:
                return 0
            index = int(seconds * self.frame_rate)
        else:
            lo, hi = 0, len(self._times)
            while lo < hi:
                mid = (lo + hi) // 2
                if self._times[mid][0] <= seconds:
                    lo = mid + 1
                else:
                    hi = mid
            index = lo - 1
        return max(0, min(index, max(0, self.frame_count - 1)))

    def packet(self, index):
        if not 0 <= index < len(self._packets):
            raise MediaError("frame %d out of range (0..%d)"
                             % (index, len(self._packets) - 1))
        offset, length, _key = self._packets[index]
        if length > MAX_FRAME_BYTES:
            raise MediaError("frame %d is %d bytes" % (index, length))
        end = offset + length
        if end > len(self._data):
            raise MediaError("frame %d runs past the end of the file" % index)
        return self._data[offset:end]

    def is_keyframe(self, index):
        if not 0 <= index < len(self._packets):
            return False
        return self._packets[index][2]

    def keyframe_before(self, index):
        for i in range(min(index, len(self._packets) - 1), -1, -1):
            if self._packets[i][2]:
                return i
        return 0

    def frame(self, index):
        """Decode frame `index`, replaying from the previous keyframe when
        the codec needs it. Sequential playback costs one packet per frame;
        a seek costs the distance back to the keyframe, which is the honest
        price of inter-frame coding and the reason a real player keeps an
        index."""
        if not 0 <= index < len(self._packets):
            raise MediaError("frame %d out of range" % index)
        start = index
        if index != self._cursor + 1:
            start = self.keyframe_before(index)
            self._codec.reset()
            self._cursor = start - 1
        rgba = None
        for i in range(start, index + 1):
            decoded = self._codec.decode(self.packet(i), self._packets[i][2])
            if decoded is not None:
                rgba = decoded
            self._cursor = i
        if rgba is None:
            # A run of drop frames with nothing before them. Show black
            # rather than nothing at all.
            rgba = bytes(self.width * self.height * 4)
        return VideoFrame(index, self.frame_time(index),
                          self.frame_duration(index),
                          self.width, self.height, rgba)

    def reset(self):
        self._codec.reset()
        self._cursor = -1


def _open_avi(data):
    micros, streams, packets, index_flags, _base = _parse_avi(data)
    video_index = None
    for i, stream in enumerate(streams):
        if stream.kind == "vids":
            video_index = i
            break
    if video_index is None:
        raise MediaError("AVI has no video stream")
    stream = streams[video_index]
    if len(stream.format) < 40:
        raise MediaError("AVI video stream has no BITMAPINFOHEADER")
    width, height, bit_count, compression, palette, top_down = \
        _bitmapinfo(stream.format)
    _check_size(width, height)

    if stream.rate and stream.scale:
        frame_rate = stream.rate / stream.scale
    elif micros:
        frame_rate = 1000000.0 / micros
    else:
        frame_rate = 0.0
    if frame_rate <= 0 or frame_rate > 1000:
        frame_rate = 25.0

    codec_name = _fourcc_of(compression)
    # Some encoders record the codec only in the stream handler and leave
    # biCompression at zero, which would otherwise read as "uncompressed" and
    # produce a screen of noise from JPEG bytes. The handler is the tie-break.
    if codec_name == "BI_RGB" and stream.handler in MJPEG_FOURCCS:
        codec_name = stream.handler
    is_mjpeg = codec_name in MJPEG_FOURCCS

    ours = []
    ordinal = 0
    for stream_id, kind, offset, length in packets:
        if stream_id != video_index or kind not in ("dc", "db"):
            continue
        flags = index_flags.get((stream_id, ordinal))
        if is_mjpeg:
            # Every MJPEG frame is a whole picture, whatever idx1 claims --
            # and idx1 claims nothing useful in a good many camera files,
            # where every entry has the keyframe bit clear. Trusting it there
            # would make a seek replay the clip from frame zero.
            keyframe = True
        elif flags is None:
            keyframe = compression in (BI_RGB, BI_BITFIELDS) or ordinal == 0
        else:
            keyframe = bool(flags & AVIIF_KEYFRAME)
        ours.append((offset, length, keyframe))
        ordinal += 1
        if len(ours) > MAX_FRAMES:
            raise MediaError("AVI has more than %d frames" % MAX_FRAMES)
    if not ours:
        raise MediaError("AVI video stream has no frames")
    if not ours[0][2]:
        ours[0] = (ours[0][0], ours[0][1], True)

    duration = len(ours) / frame_rate
    info = MediaInfo("AVI", codec_name, width, height, duration, len(ours))

    if is_mjpeg:
        codec = _Mjpeg(width, height)
    elif compression == BI_RGB:
        codec = _RawDib(width, height, bit_count, palette, top_down)
    elif compression == BI_RLE8:
        if bit_count != 8:
            raise MediaError("BI_RLE8 with %d bits per pixel" % bit_count)
        codec = _Rle8(width, height, palette, top_down)
    elif codec_name in H264_FOURCCS:
        # AVI carries H.264 as Annex B in the chunk, with no configuration
        # box anywhere, so there is no extradata to hand over.
        try:
            codec = _H264(width, height, b"", data, ours)
        except MediaError as exc:
            info.reason = _h264_reason(exc)
            raise _Unsupported(info)
    else:
        info.reason = KNOWN_UNDECODABLE.get(
            codec_name, "no decoder for %s" % codec_name)
        raise _Unsupported(info)
    info.supported = True
    return VideoTrack(data, info, ours, codec, frame_rate,
                      [i for i, p in enumerate(ours) if p[2]])


def _h264_reason(exc):
    """Whatever went wrong, said with the codec's name in front of it.

    Three things raise on the way to an H.264 track: the decoder, which
    names itself already; the sample-table reader; and the helper below
    that fetches the first keyframe. The last two have no business knowing
    which codec they are fetching for. The `<video>` element shows this one
    sentence and nothing else, so it has to name the codec exactly once --
    not never, and not twice.
    """
    text = str(exc)
    return text if text.startswith("H.264") else "H.264: %s" % text


def _first_keyframe(data, samples):
    """The bytes of the first sample a decoder could start from.

    Both containers describe a frame the same way once their tables are
    joined up -- offset, length, keyframe -- so one helper serves both.
    A file whose first keyframe points outside itself is truncated, and
    saying so here is better than handing a decoder a short buffer.
    """
    for offset, length, keyframe in samples:
        if not keyframe:
            continue
        if offset < 0 or length <= 0 or offset + length > len(data):
            raise MediaError("the first keyframe is outside the file")
        return data[offset:offset + length]
    raise MediaError("no keyframe in this video track")


class _Unsupported(MediaError):
    """Raised inside the openers when the file parsed fine and we simply do
    not have the codec. Carries the MediaInfo so `probe()` can report real
    numbers for a file `open_video()` refuses."""

    def __init__(self, info):
        MediaError.__init__(self, info.reason or "unsupported codec")
        self.info = info


# -- MP4 / MOV / ISO base media ----------------------------------------------
#
# One parser for both, because they are one format: QuickTime's file layout is
# what ISO standardised, and the only interesting differences are which
# codecs turn up inside. That is why the demuxer below is worth having even
# though the MP4s on the web are all H.264 -- the .mov half of the same code
# is where Motion JPEG lives, and a camera that writes .mov writes `jpeg`.
#
# The sample tables are the whole job. A frame's bytes are not marked in the
# file; they are computed from five tables that between them say how long
# each sample lasts (stts), which samples share a chunk (stsc), how big each
# sample is (stsz), where each chunk starts (stco/co64) and which samples can
# be decoded from cold (stss). Get any of them wrong and every frame after
# the first is garbage, which is why this reconstructs the list once, up
# front, and the tests check the offsets rather than the picture.

def _boxes(reader, depth=0):
    if depth > MAX_DEPTH:
        raise MediaError("MP4 boxes nested too deeply")
    seen = 0
    while reader.remaining() >= 8:
        seen += 1
        if seen > MAX_CHUNKS:
            raise MediaError("MP4 box list does not terminate")
        start = reader.pos
        size = reader.u32be()
        kind = reader.fourcc()
        if size == 1:
            size = reader.u64be()
        elif size == 0:
            size = (reader.end - start)
        if size < (reader.pos - start):
            raise MediaError("MP4 box %r declares %d bytes" % (kind, size))
        end = start + size
        if end > reader.end:
            end = reader.end
        yield kind, _Reader(reader.data, reader.pos, end)
        if end <= start:
            raise MediaError("MP4 box %r does not advance" % kind)
        reader.pos = end


class _Mp4Track:
    """One `trak`'s worth of sample tables, before they are joined up."""

    def __init__(self):
        self.handler = ""
        self.timescale = 0
        self.duration_ticks = 0
        self.display_width = 0      # tkhd, in points -- what to draw it at
        self.display_height = 0
        self.width = 0              # stsd, in pixels -- what is stored
        self.height = 0
        self.depth = 24
        self.codec = ""
        self.stts = []              # (sample count, duration in ticks)
        self.sync = None            # 1-based sample numbers, or None for all
        self.stsc = []              # (first chunk, samples per chunk)
        self.sample_size = 0        # non-zero when every sample is that size
        self.sizes = []
        self.chunks = []            # chunk offsets into the file
        self.extradata = b""        # avcC and the like, verbatim


def _parse_mp4(data):
    """Every track in the file, plus the movie's own duration in seconds."""
    duration = 0.0
    tracks = []
    for kind, box in _boxes(_Reader(data)):
        if kind != "moov":
            continue
        for sub_kind, sub in _boxes(box, 1):
            if sub_kind == "mvhd":
                version = sub.u8()
                sub.skip(3)
                if version == 1:
                    sub.skip(16)
                    timescale = sub.u32be()
                    ticks = sub.u64be()
                else:
                    sub.skip(8)
                    timescale = sub.u32be()
                    ticks = sub.u32be()
                if timescale:
                    duration = ticks / timescale
            elif sub_kind == "trak":
                tracks.append(_parse_trak(sub))
    return duration, tracks


def _parse_trak(trak):
    track = _Mp4Track()
    for kind, box in _boxes(trak, 2):
        if kind == "tkhd":
            version = box.u8()
            box.skip(3)
            box.skip(16 if version == 1 else 8)
            box.skip(4)                      # track id
            box.skip(4)                      # reserved
            box.skip(8 if version == 1 else 4)
            box.skip(52)                     # reserved, layer, volume, matrix
            width = box.u32be() / 65536.0
            height = box.u32be() / 65536.0
            if width >= 1 and height >= 1:
                track.display_width = int(round(width))
                track.display_height = int(round(height))
        elif kind == "mdia":
            _parse_mdia(box, track)
    return track


def _parse_mdia(mdia, track):
    for kind, box in _boxes(mdia, 3):
        if kind == "mdhd":
            version = box.u8()
            box.skip(3)
            box.skip(16 if version == 1 else 8)
            track.timescale = box.u32be()
            track.duration_ticks = box.u64be() if version == 1 else box.u32be()
        elif kind == "hdlr":
            box.skip(8)                      # version/flags, pre_defined
            track.handler = box.fourcc()
        elif kind == "minf":
            for m_kind, m in _boxes(box, 4):
                if m_kind == "stbl":
                    _parse_stbl(m, track)


def _parse_stbl(stbl, track):
    for kind, box in _boxes(stbl, 5):
        if kind == "stsd":
            _parse_stsd(box, track)
        elif kind == "stts":
            box.skip(4)
            for _ in range(_table_count(box, 8)):
                count = box.u32be()
                delta = box.u32be()
                track.stts.append((count, delta))
        elif kind == "stss":
            box.skip(4)
            sync = set()
            for _ in range(_table_count(box, 4)):
                sync.add(box.u32be())
            track.sync = sync
        elif kind == "stsc":
            box.skip(4)
            for _ in range(_table_count(box, 12)):
                first = box.u32be()
                per_chunk = box.u32be()
                box.skip(4)                  # sample description index
                track.stsc.append((first, per_chunk))
        elif kind == "stsz":
            box.skip(4)
            # stsz is the one table whose count is not the first field, so it
            # cannot go through _table_count -- a constant sample size means
            # there is no per-sample list to bound against at all.
            track.sample_size = box.u32be()
            declared = box.u32be()
            if track.sample_size:
                track.sizes = [track.sample_size] * min(declared, MAX_FRAMES)
            else:
                room = box.remaining() // 4
                for _ in range(min(declared, room, MAX_FRAMES)):
                    track.sizes.append(box.u32be())
        elif kind in ("stco", "co64"):
            box.skip(4)
            wide = kind == "co64"
            for _ in range(_table_count(box, 8 if wide else 4)):
                track.chunks.append(box.u64be() if wide else box.u32be())


def _table_count(box, entry_bytes):
    """How many entries a sample-table box really has.

    The declared count is read and then clamped to what is left in the box,
    because it is a 32-bit number a stranger wrote: a `stsz` claiming four
    billion samples is otherwise four billion `u32be()` calls before the
    first short read stops it.
    """
    declared = box.u32be()
    return min(declared, box.remaining() // entry_bytes, MAX_FRAMES)


def _parse_stsd(stsd, track):
    stsd.skip(4)                             # version + flags
    count = stsd.u32be()
    for _ in range(min(count, 16)):
        if stsd.remaining() < 8:
            break
        entry_size = stsd.u32be()
        fourcc = stsd.fourcc()
        if not track.codec:
            track.codec = fourcc
        body = min(max(entry_size - 8, 0), stsd.remaining())
        entry = _Reader(stsd.data, stsd.pos, stsd.pos + body)
        # 78 bytes is exactly a VisualSampleEntry with nothing appended.
        # Anything shorter is a sound or hint entry, or a truncated one, and
        # reading a picture size out of it would invent numbers.
        if track.handler in ("vide", "") and body >= 78:
            # VisualSampleEntry: 6 reserved, 2 data reference index, then
            # 16 bytes of pre_defined/reserved before the stored size.
            entry.skip(24)
            track.width = entry.u16be()
            track.height = entry.u16be()
            entry.skip(14)                   # resolutions, reserved, frames
            entry.skip(32)                   # compressor name (Pascal string)
            track.depth = entry.u16be()
            entry.skip(2)                    # pre_defined, always -1
            # Whatever boxes follow the sample entry are the codec's own
            # configuration. `avcC` is the one that matters here: an H.264
            # sample carries no SPS or PPS of its own in MP4, they live in
            # this box, and without it the samples are undecodable.
            _parse_sample_extensions(entry, track)
        stsd.skip(body)


def _parse_sample_extensions(entry, track):
    for kind, box in _boxes(entry, 1):
        if kind == "avcC" and not track.extradata:
            track.extradata = bytes(box.data[box.pos:box.end])


def _mp4_samples(track):
    """Join the sample tables into (offset, length, keyframe) per frame.

    The one subtlety is `stsc`: it is run-length coded over *chunks*, and the
    run ends where the next entry's first chunk begins, so the last entry
    silently means "and all the remaining chunks too".
    """
    sizes = track.sizes
    total = len(sizes)
    if not total or not track.chunks or not track.stsc:
        raise MediaError("MP4 track has no usable sample table")
    if total > MAX_FRAMES:
        raise MediaError("MP4 track has more than %d samples" % MAX_FRAMES)
    samples = []
    index = 0
    for i, (first, per_chunk) in enumerate(track.stsc):
        if per_chunk <= 0 or first <= 0:
            raise MediaError("MP4 stsc entry of %d samples in chunk %d"
                             % (per_chunk, first))
        last = track.stsc[i + 1][0] - 1 if i + 1 < len(track.stsc) \
            else len(track.chunks)
        for chunk_no in range(first, min(last, len(track.chunks)) + 1):
            offset = track.chunks[chunk_no - 1]
            for _ in range(per_chunk):
                if index >= total:
                    break
                size = sizes[index]
                if size > MAX_FRAME_BYTES:
                    raise MediaError("MP4 sample %d is %d bytes"
                                     % (index, size))
                keyframe = track.sync is None or (index + 1) in track.sync
                samples.append((offset, size, keyframe))
                offset += size
                index += 1
        if index >= total:
            break
    if not samples:
        raise MediaError("MP4 track has no samples")
    if not samples[0][2]:
        samples[0] = (samples[0][0], samples[0][1], True)
    return samples


def _mp4_times(track, count):
    """Per-sample (pts, duration) in seconds, from `stts`."""
    timescale = track.timescale or 600
    times = []
    ticks = 0
    for run, delta in track.stts:
        for _ in range(min(run, count - len(times))):
            times.append((ticks / timescale, delta / timescale))
            ticks += delta
        if len(times) >= count:
            break
    # A short or missing stts: hold the last duration for the rest. Better
    # than dropping the frames, which is what a strict reading would do.
    tail = times[-1][1] if times else (1.0 / MJPEG_DEFAULT_FPS)
    while len(times) < count:
        times.append((ticks / timescale, tail))
        ticks += tail * timescale
    return times


class _QuickTimeRaw(_Codec):
    """QuickTime `raw `: uncompressed, top-down, and not the byte order the
    Windows DIB above uses. 24-bit samples are R, G, B in that order and
    32-bit ones are A, R, G, B -- the alpha comes first, which is the detail
    that turns a picture into a colour-shifted one if you assume otherwise."""

    def __init__(self, width, height, depth):
        _check_size(width, height)
        if depth not in (24, 32):
            raise MediaError("QuickTime raw video at %d bits per pixel is "
                             "not supported" % depth)
        self.width = width
        self.height = height
        self.depth = depth

    def reset(self):
        pass

    def decode(self, packet, keyframe):
        if not packet:
            return None
        step = self.depth // 8
        need = self.width * self.height * step
        if len(packet) < need:
            raise MediaError("raw frame short: %d bytes, need %d"
                             % (len(packet), need))
        out = bytearray(self.width * self.height * 4)
        if step == 3:
            for i in range(self.width * self.height):
                s, d = i * 3, i * 4
                out[d] = packet[s]
                out[d + 1] = packet[s + 1]
                out[d + 2] = packet[s + 2]
                out[d + 3] = 255
        else:
            for i in range(self.width * self.height):
                s, d = i * 4, i * 4
                out[d] = packet[s + 1]
                out[d + 1] = packet[s + 2]
                out[d + 2] = packet[s + 3]
                out[d + 3] = 255
        return bytes(out)


class _PngFrames(_Codec):
    """QuickTime `png `: a whole PNG per frame. Lossless MJPEG, essentially,
    and free here because the PNG decoder was already written for `<img>`."""

    def __init__(self, width, height):
        _check_size(width, height)
        self.width = width
        self.height = height

    def reset(self):
        pass

    def decode(self, packet, keyframe):
        if not packet:
            return None
        try:
            width, height, rgba = imagecodec.decode_png(packet)
        except imagecodec.ImageError as exc:
            raise MediaError("PNG frame: %s" % exc)
        if (width, height) != (self.width, self.height):
            _check_size(width, height)
            rgba = imagecodec.resize(rgba, width, height,
                                     self.width, self.height)
        return rgba


def _open_mp4(data, container="MP4"):
    """A `VideoTrack` over an MP4/MOV, or `_Unsupported` carrying what we
    learned about it. Both paths run the same parse, so the numbers in the
    "no decoder" box are the numbers a player that had one would use."""
    movie_duration, tracks = _parse_mp4(data)
    video = None
    for track in tracks:
        if track.handler == "vide":
            video = track
            break
    if video is None:
        # No handler said "video". Some very old QuickTime files leave hdlr
        # out of mdia entirely, so fall back to the first track that named a
        # picture size -- stored or displayed -- rather than giving up on a
        # file we can still report the shape of.
        for track in tracks:
            if (track.width or track.display_width) \
                    and (track.height or track.display_height):
                video = track
                break
    if video is None:
        raise MediaError("%s has no video track" % container)

    width = video.width or video.display_width
    height = video.height or video.display_height
    codec = video.codec or ""
    duration = movie_duration
    if video.timescale and video.duration_ticks:
        duration = video.duration_ticks / video.timescale

    info = MediaInfo(container, codec, width, height, duration, 0)
    try:
        samples = _mp4_samples(video)
    except MediaError as exc:
        info.reason = KNOWN_UNDECODABLE.get(
            codec, "no decoder for %s" % (codec or "this file's video codec"))
        if codec in MJPEG_FOURCCS or codec in ("raw ", "png "):
            info.reason = str(exc)
        # "no decoder for avc1" would be a lie now that there is one, and the
        # lie sends the reader off looking for a codec instead of at the
        # sample table that actually broke. Name the codec, then say what
        # went wrong with the file -- the same shape of message the formats
        # above have always given.
        if codec in H264_FOURCCS:
            info.reason = _h264_reason(exc)
        raise _Unsupported(info)
    info.frame_count = len(samples)
    times = _mp4_times(video, len(samples))
    if duration <= 0 and times:
        duration = times[-1][0] + times[-1][1]
        info.duration = duration
    frame_rate = len(samples) / duration if duration > 0 else 0.0
    if frame_rate <= 0 or frame_rate > 1000:
        frame_rate = MJPEG_DEFAULT_FPS

    if codec in MJPEG_FOURCCS:
        decoder = _Mjpeg(width, height)
    elif codec == "raw ":
        decoder = _QuickTimeRaw(width, height, video.depth)
    elif codec == "png ":
        decoder = _PngFrames(width, height)
    elif codec in H264_FOURCCS:
        try:
            decoder = _H264(width, height, video.extradata, data, samples)
        except MediaError as exc:
            info.reason = _h264_reason(exc)
            raise _Unsupported(info)
    else:
        info.reason = KNOWN_UNDECODABLE.get(
            codec, "no decoder for %s" % (codec or "this file's video codec"))
        raise _Unsupported(info)
    _check_size(width, height)
    info.supported = True
    return VideoTrack(data, info, samples, decoder, frame_rate,
                      [i for i, s in enumerate(samples) if s[2]], times=times)


def _probe_mp4(data, container="MP4"):
    try:
        return _open_mp4(data, container).info
    except _Unsupported as exc:
        return exc.info


# -- WebM / Matroska (probe only) --------------------------------------------

_EBML_SEGMENT = 0x18538067
_EBML_INFO = 0x1549A966
_EBML_TIMECODE_SCALE = 0x2AD7B1
_EBML_DURATION = 0x4489
_EBML_TRACKS = 0x1654AE6B
_EBML_TRACK_ENTRY = 0xAE
_EBML_CODEC_ID = 0x86
_EBML_VIDEO = 0xE0
_EBML_PIXEL_WIDTH = 0xB0
_EBML_PIXEL_HEIGHT = 0xBA

_EBML_MASTERS = (_EBML_SEGMENT, _EBML_INFO, _EBML_TRACKS, _EBML_TRACK_ENTRY,
                 _EBML_VIDEO)


def _ebml_id(reader):
    first = reader.u8()
    if first == 0:
        raise MediaError("EBML id with no length marker")
    length = 1
    mask = 0x80
    while not first & mask:
        mask >>= 1
        length += 1
    value = first
    for _ in range(length - 1):
        value = (value << 8) | reader.u8()
    return value


def _ebml_size(reader):
    first = reader.u8()
    if first == 0:
        raise MediaError("EBML size with no length marker")
    length = 1
    mask = 0x80
    while not first & mask:
        mask >>= 1
        length += 1
    value = first & (mask - 1)
    unknown = value == mask - 1
    for _ in range(length - 1):
        byte = reader.u8()
        value = (value << 8) | byte
        unknown = unknown and byte == 0xFF
    return (None if unknown else value)


def _ebml_walk(reader, info, timing, depth=0):
    """Walk one level of EBML, filling `info` and the `timing` dict.

    Duration is in Segment/Info ticks and the tick length is in a sibling
    element, so the two are collected separately and multiplied once the walk
    is done -- they can arrive in either order.
    """
    if depth > MAX_DEPTH:
        return
    seen = 0
    while reader.remaining() >= 2:
        seen += 1
        if seen > MAX_CHUNKS:
            raise MediaError("EBML element list does not terminate")
        element = _ebml_id(reader)
        size = _ebml_size(reader)
        if size is None or size > reader.remaining():
            # "Unknown size" is legal for a Segment and means "to the end".
            size = reader.remaining()
        body = _Reader(reader.data, reader.pos, reader.pos + size)
        if element in _EBML_MASTERS:
            _ebml_walk(body, info, timing, depth + 1)
        elif element == _EBML_TIMECODE_SCALE:
            timing["scale"] = _ebml_uint(body)
        elif element == _EBML_DURATION:
            timing["ticks"] = _ebml_float(body)
        elif element == _EBML_CODEC_ID:
            codec = body.take(body.remaining()).decode("latin-1").strip("\0")
            if codec.startswith("V_"):
                info.codec = codec
        elif element == _EBML_PIXEL_WIDTH:
            info.width = _ebml_uint(body)
        elif element == _EBML_PIXEL_HEIGHT:
            info.height = _ebml_uint(body)
        reader.pos += size
        if size == 0 and reader.remaining() < 2:
            return


def _ebml_uint(reader):
    value = 0
    for byte in reader.take(reader.remaining()):
        value = (value << 8) | byte
    return value


def _ebml_float(reader):
    raw = reader.take(reader.remaining())
    if len(raw) == 4:
        return struct.unpack(">f", raw)[0]
    if len(raw) == 8:
        return struct.unpack(">d", raw)[0]
    return 0.0


def _probe_webm(data):
    info = MediaInfo("WebM")
    timing = {"scale": 1000000, "ticks": 0.0}
    _ebml_walk(_Reader(data), info, timing)
    info.duration = (timing["ticks"] or 0.0) * (timing["scale"] or 1000000) / 1e9
    codec = info.codec
    short = {"V_VP8": "VP80", "V_VP9": "VP90"}.get(codec, codec)
    info.reason = KNOWN_UNDECODABLE.get(
        short, "no decoder for %s" % (codec or "this WebM's video codec"))
    return info


# -- a bare MJPEG stream -----------------------------------------------------

def _open_mjpeg_stream(data):
    """A file that is nothing but JPEGs, one after another.

    This is what a network camera serves and what a capture card writes, and
    it is the least a video file can be: no header, no index, no timing, no
    audio -- just pictures. Everything the container would have told us has to
    be inferred, so the size comes from the first frame's own header and the
    rate from `MJPEG_DEFAULT_FPS`, which is a guess and is documented as one.
    """
    frames = []
    size = None
    pos = 0
    while pos < len(data):
        sos_at, end, dims = _jpeg_scan(data, pos)
        if sos_at is None:
            break
        if size is None and dims:
            size = dims
        frames.append((pos, end - pos, True))
        if len(frames) > MAX_FRAMES:
            raise MediaError("MJPEG stream has more than %d frames"
                             % MAX_FRAMES)
        nxt = data.find(b"\xff\xd8\xff", end)
        if nxt < 0:
            break
        pos = nxt
    if not frames or size is None:
        raise MediaError("MJPEG stream has no complete frame in it")
    width, height = size
    _check_size(width, height)
    frame_rate = MJPEG_DEFAULT_FPS
    info = MediaInfo("MJPEG", "MJPG", width, height,
                     len(frames) / frame_rate, len(frames))
    info.supported = True
    return VideoTrack(data, info, frames, _Mjpeg(width, height), frame_rate,
                      list(range(len(frames))))


# -- entry points ------------------------------------------------------------

# Box types that can legally open an ISO/QuickTime file. A .mov out of an old
# camera often has no `ftyp` at all -- that box was invented for MP4 and
# retrofitted -- so the first box's *type* is what identifies the family.
_ISO_LEADERS = ("ftyp", "moov", "mdat", "wide", "free", "skip", "pnot",
                "PICT")


def sniff(data):
    """The container this looks like, or "" -- by magic bytes only, because
    a URL's extension is a claim and the first twelve bytes are evidence."""
    if len(data) < 12:
        return ""
    if data[:4] == b"RIFF" and data[8:12] == b"AVI ":
        return "AVI"
    if data[:3] == b"\xff\xd8\xff":
        return "MJPEG"
    if data[:4] == b"\x1a\x45\xdf\xa3":
        return "WebM"
    leader = data[4:8].decode("latin-1")
    if leader in _ISO_LEADERS:
        # `ftyp` carries the brand, and QuickTime's is the four characters
        # "qt  ". Telling the two apart is cosmetic -- the parser is the same
        # -- but a box that says MOV when the file is a .mov is worth having.
        if leader == "ftyp" and data[8:12] != b"qt  ":
            return "MP4"
        return "MOV"
    return ""


def probe(data):
    """What the file is, whether or not we can play it.

    Never raises for a file we merely lack a codec for; that case comes back
    as a MediaInfo with `supported` False and a `reason`. It still raises
    `MediaError` for bytes that are not a container we know at all, and for a
    container that is malformed.
    """
    kind = sniff(data)
    if kind in ("AVI", "MJPEG"):
        try:
            return open_video(data).info
        except _Unsupported as exc:
            return exc.info
    if kind in ("MP4", "MOV"):
        return _probe_mp4(data, kind)
    if kind == "WebM":
        return _probe_webm(data)
    raise MediaError("not a media container we recognise")


def open_video(data):
    """A `VideoTrack` for a file we can actually decode.

    Raises `MediaError` otherwise -- including for a well-formed MP4, whose
    `probe()` result is still worth showing.
    """
    kind = sniff(data)
    if kind == "AVI":
        return _open_avi(data)
    if kind == "MJPEG":
        return _open_mjpeg_stream(data)
    if kind in ("MP4", "MOV"):
        return _open_mp4(data, kind)
    raise _Unsupported(probe(data))
