"""Media containers and codecs, decoded to raw RGBA and raw PCM.

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
  MOV/MP4/AVI with `avc1`    H.264, I, P and B slices, by the Fortran
                             decoder in `fortran/`; a stream with SP or SI
                             slices is refused before anything is drawn
  a bare `.mjpeg` stream      JPEGs end to end with no container at all

And what is decoded to sound:

  MOV/MP4 with `mp4a`        AAC-LC, by the Fortran decoder in `fortran/`,
                             handed back as interleaved 32-bit floats

That is one codec in two containers, and it is deliberately the whole list.
`probe_audio` walks everything else far enough to say what it is -- the
sample rate, the channel count, the duration and a name -- and then declines,
for the same reason the video side declines VP9: "48 kHz stereo Opus, no
decoder" is a useful sentence and silence is not.

Motion JPEG is the one that makes this a video player rather than a
demonstration. It is here because the expensive half of it was already
written: `imagecodec.decode_jpeg` is our own baseline-and-progressive JPEG
decoder in Rust, and it decodes a 320x240 picture in about a millisecond, so
a frame costs a couple of percent of one frame's worth of time. The codec
below is the cheap half -- work out where each JPEG starts and ends, hand it
over, and put the pixels where the compositor expects them.

Everything else in this module *reads* the file and refuses honestly: a WebM
carrying VP9, or an H.264 stream with SP slices, is walked far enough to
report its dimensions, duration and codec name, and then declines, because
saying "1280x720, VP9, 4.0s, no decoder" is useful and pretending to play it
is not. See docs/media.md.

B frames also make decode order and presentation order two different
orders, and the container is where they are put back together: `ctts` says
how far each sample's presentation time sits from its decode time, and
`VideoTrack` indexes everything by the order a viewer sees while decoding
in the order the file is written.

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
decoded frame goes to the screen without a conversion step. `AudioFrame` is
the same idea one channel layout over: an index, a time, a duration, and
`samples` -- interleaved 32-bit floats, which is what a mixer wants and what
the decoder already has.
"""

import struct

from . import h264
from . import imagecodec
from .imagecodec import MAX_PIXELS

__all__ = ["MediaError", "VideoFrame", "VideoTrack", "MediaInfo",
           "AudioFrame", "AudioTrack", "AudioInfo",
           "open_video", "probe", "open_audio", "probe_audio", "sniff",
           "MAX_FRAMES", "MAX_CHUNKS", "MJPEG_DEFAULT_FPS"]


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


class AudioFrame:
    """One decoded block of sound and the instant it belongs at.

    `samples` is interleaved 32-bit floats in the machine's own byte order --
    one float per sample per channel, channels adjacent -- because that is
    what the decoder produces and what a mixer consumes, and a conversion in
    between would cost a copy per frame for nothing.

    An AAC frame is 1024 samples per channel, so at 44.1 kHz this is about
    23 milliseconds of sound; `sample_count` is that number, not the length
    of the buffer, which is the distinction every audio bug is made of.
    """

    __slots__ = ("index", "pts", "duration", "sample_rate", "channels",
                 "samples")

    def __init__(self, index, pts, duration, sample_rate, channels, samples):
        self.index = index
        self.pts = pts
        self.duration = duration
        self.sample_rate = sample_rate
        self.channels = channels
        self.samples = samples

    @property
    def end(self):
        return self.pts + self.duration

    @property
    def sample_count(self):
        """Samples per channel: four bytes a float, `channels` of them."""
        if self.channels <= 0:
            return 0
        return len(self.samples) // 4 // self.channels

    def __repr__(self):
        return ("<AudioFrame %d %dch %dHz %d samples @%.3fs+%.3f>"
                % (self.index, self.channels, self.sample_rate,
                   self.sample_count, self.pts, self.duration))


class AudioInfo:
    """What a file's sound says about itself, whether or not we decode it.

    The audio half of `MediaInfo`, and separate from it on purpose: a file
    can have a video track we play and an audio track we only name, and one
    object carrying both would have to have a `supported` that means two
    things at once.
    """

    __slots__ = ("container", "codec", "sample_rate", "channels", "duration",
                 "frame_count", "supported", "reason")

    def __init__(self, container, codec="", sample_rate=0, channels=0,
                 duration=0.0, frame_count=0, supported=False, reason=""):
        self.container = container
        self.codec = codec
        self.sample_rate = sample_rate
        self.channels = channels
        self.duration = duration
        self.frame_count = frame_count
        self.supported = supported
        self.reason = reason

    def __repr__(self):
        return ("<AudioInfo %s/%s %dHz %dch %.3fs %d frames %s>"
                % (self.container, self.codec or "?", self.sample_rate,
                   self.channels, self.duration, self.frame_count,
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

    The decoder does I, P and B slices, so the useful question is not "is
    this H.264" but "does this particular stream decode", and part of the
    answer can only be had by decoding. The constructor takes the first
    keyframe, runs it, and refuses the whole file with the decoder's own
    reason if it does not come out. Opening a file that plays for two
    frames and then stops would be worse than refusing it -- the poster and
    a sentence beats a frozen picture.

    Trial-decoding frame zero cannot see frame four hundred, though, and
    the things this decoder still cannot do -- SP and SI slices -- are a
    per-slice property that an encoder is free to introduce anywhere. So
    every sample's slice_type is read first. That costs two exp-Golomb
    fields per NAL, needs no arithmetic decoding, and is the only way to
    know before the poster goes up that the file finishes.

    Stateful, and it means something: P and B frames are differences
    against other pictures, so `reset()` drops the decoder's reference
    pictures and `VideoTrack.frame()` replays from the keyframe. B frames
    also arrive out of presentation order, which is the container's problem
    rather than this class's: everything here is in decode order and
    `VideoTrack` puts the pictures back in the order they are shown.
    """

    def __init__(self, width, height, extradata, data, samples):
        _check_size(width, height)
        self.width = width
        self.height = height
        try:
            self._decoder = h264.Decoder(extradata)
        except h264.H264Error as exc:
            raise MediaError("H.264: %s" % exc)
        kinds = h264.slice_types(_annexb_track(data, samples, extradata))
        refused = [name for kind, name in ((3, "SP"), (4, "SI"))
                   if kind in kinds]
        if refused:
            raise MediaError(
                "H.264: this track has %s slices, and the decoder does I, P "
                "and B slices" % " and ".join(refused))
        try:
            got_width, got_height, _rgba = self._decoder.decode(
                _first_keyframe(data, samples))
            # The trial decode has left an IDR in the reference list; drop
            # it so that frame zero is decoded once, from a clean decoder,
            # by whoever asks for frame zero.
            self._decoder.reset()
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
        self._decoder.reset()

    def decode(self, packet, keyframe):
        if not packet:
            return None
        try:
            _width, _height, rgba = self._decoder.decode(packet)
        except h264.H264Error as exc:
            raise MediaError("H.264 frame: %s" % exc)
        return rgba


def _aac_module():
    """The AAC decoder, imported at the moment it is needed.

    Lazy, and for the same reason `probe()` has to keep working on a machine
    with no gfortran: a decoder that will not import is a codec we do not
    have, which is a sentence to show a user, not a reason for
    `import mediacodec` to fail and take the whole media stack with it.
    """
    try:
        from . import aac
    except Exception as exc:        # not just ImportError: a broken build
        raise MediaError("AAC: no decoder (%s)" % exc)
    return aac


class _Aac:
    """AAC, decoded by the Fortran decoder in ``fortran/``.

    Stateful, and unavoidably so: every AAC frame is coded independently in
    the sense that matters for a container -- there is no keyframe flag and
    no reference frame -- but the last step of decoding one is an inverse
    MDCT whose output is overlapped and added with the *previous* frame's.
    So a decoder handed frame N with nothing before it produces the right
    number of samples and the wrong sound for the first half of them, which
    is why `AudioTrack.frame()` replays from frame zero rather than seeking.
    """

    def __init__(self, asc):
        aac = _aac_module()
        self._aac = aac
        try:
            self._decoder = aac.Decoder(asc)
        except aac.AacError as exc:
            raise MediaError(_aac_reason(exc))
        self.sample_rate = self._decoder.sample_rate
        self.channels = self._decoder.channels
        self.frame_length = self._decoder.frame_length

    def reset(self):
        self._decoder.reset()

    def decode(self, packet):
        """(samples per channel, channels, interleaved float32 bytes)."""
        if not packet:
            return 0, self.channels, b""
        try:
            return self._decoder.decode(packet)
        except self._aac.AacError as exc:
            raise MediaError(_aac_reason(exc))


def _mp3_module():
    """The MPEG Layer III decoder, imported at the moment it is needed, for
    the same reason `_aac_module` is."""
    try:
        from . import ball
    except Exception as exc:        # not just ImportError: a broken build
        raise MediaError("MP3: no decoder (%s)" % exc)
    return ball


class _Mp3:
    """MPEG Layer III, decoded by the Fortran decoder in ``fortran/``.

    Stateful for one more reason than AAC is. The transform overlap and the
    filterbank history join each frame to the next as they do there, and on
    top of that Layer III has a bit reservoir: a frame's main data may
    physically live in frames already gone past. So a decoder handed frame
    N with nothing before it does not merely start attenuated, it may have
    no main data to read at all -- which it reports as silence rather than
    as an error, and which is why `AudioTrack.frame()` replays from zero.
    """

    def __init__(self):
        ball = _mp3_module()
        self._ball = ball
        try:
            self._decoder = ball.Decoder()
        except ball.Mp3Error as exc:
            raise MediaError(_mp3_reason(exc))
        self.sample_rate = 0
        self.channels = 0

    def reset(self):
        self._decoder.reset()

    def decode(self, packet):
        """(samples per channel, channels, interleaved float32 bytes)."""
        if not packet:
            return 0, self.channels, b""
        try:
            out = self._decoder.decode(packet)
        except self._ball.Mp3Error as exc:
            raise MediaError(_mp3_reason(exc))
        self.sample_rate = self._decoder.sample_rate
        self.channels = self._decoder.channels
        return out


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


# The MPEG-4 objectTypeIndication values that mean AAC. 0x40 is "MPEG-4
# Audio", which is what every modern muxer writes and is the one that then
# needs its AudioSpecificConfig read to find out *which* MPEG-4 audio; the
# other three are the MPEG-2 AAC profiles, which older files use and which
# decode the same way once the config is in hand.
AAC_OBJECT_TYPES = (0x40, 0x66, 0x67, 0x68)

# ...and the two that mean MP3 hiding inside an `mp4a` sample entry, which is
# legal, rare, and exactly the case where trusting the fourcc alone would
# hand an AAC decoder something that is not AAC.
MP3_OBJECT_TYPES = (0x69, 0x6B)

# Audio fourccs we can name but not decode, in the same spirit as the video
# table above: the point of naming them is that a page can say "AC-3" rather
# than "unknown", and that the next contributor can see where a decoder goes.
KNOWN_UNDECODABLE_AUDIO = {
    "ac-3": "Dolby Digital (AC-3): no decoder",
    "ec-3": "Dolby Digital Plus (E-AC-3): no decoder",
    ".mp3": "MP3: no decoder",
    "mp3 ": "MP3: no decoder",
    "sowt": "16-bit PCM: no decoder",
    "twos": "16-bit PCM: no decoder",
    "lpcm": "uncompressed PCM: no decoder",
    "raw ": "8-bit PCM: no decoder",
    "alac": "Apple Lossless: no decoder",
    "Opus": "Opus: no decoder",
    "opus": "Opus: no decoder",
    "fLaC": "FLAC: no decoder",
    "samr": "AMR narrowband: no decoder",
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
                 times=None, order=None):
        self._data = data
        self.info = info
        self._packets = packets            # (offset, length, keyframe)
        self._codec = codec
        self.frame_rate = frame_rate
        self._keyframes = keyframes
        # Every index this class takes or returns is a position in
        # presentation order -- the order a viewer sees. `_packets` is in
        # decode order, which for a stream with B frames is a different
        # order, and `_order[p]` is the packet shown p-th. None means the
        # two orders are the same, which is every stream without B frames
        # and so is worth not paying for.
        self._order = list(order) if order else None
        self._shown_at = None
        if self._order is not None:
            self._shown_at = [0] * len(self._order)
            for position, packet in enumerate(self._order):
                self._shown_at[packet] = position
        # Pictures decoded but not yet asked for, by presentation position.
        # A B stream produces them out of order and this is where they wait;
        # it holds the reorder delay and nothing more, because everything
        # already shown is dropped on the way out of `_picture`.
        self._pending = {}
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

    def _decode_index(self, index):
        """Where the frame shown at `index` sits in decode order."""
        if self._order is None:
            return index
        return self._order[index]

    def packet(self, index):
        if not 0 <= index < len(self._packets):
            raise MediaError("frame %d out of range (0..%d)"
                             % (index, len(self._packets) - 1))
        return self._sample(self._decode_index(index))

    def _sample(self, decode_index):
        offset, length, _key = self._packets[decode_index]
        if length > MAX_FRAME_BYTES:
            raise MediaError("frame %d is %d bytes" % (decode_index, length))
        end = offset + length
        if end > len(self._data):
            raise MediaError("frame %d runs past the end of the file"
                             % decode_index)
        return self._data[offset:end]

    def is_keyframe(self, index):
        if not 0 <= index < len(self._packets):
            return False
        return self._packets[self._decode_index(index)][2]

    def keyframe_before(self, index):
        """The latest frame at or before `index` that can be decoded cold.

        In presentation order, like every other index here. That is the
        right space for it even with B frames in the file: an IDR opens its
        group of pictures in both orders at once, so no frame shown after
        one is coded before it, and landing on it and playing forward loses
        nothing that was going to be shown.
        """
        for i in range(min(index, len(self._packets) - 1), -1, -1):
            if self.is_keyframe(i):
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
        rgba = self._picture(index) if self._order is not None \
            else self._in_order(index)
        if rgba is None:
            # A run of drop frames with nothing before them. Show black
            # rather than nothing at all.
            rgba = bytes(self.width * self.height * 4)
        return VideoFrame(index, self.frame_time(index),
                          self.frame_duration(index),
                          self.width, self.height, rgba)

    def _in_order(self, index):
        """Decode frame `index` when decode order is presentation order."""
        start = index
        if index != self._cursor + 1:
            start = self.keyframe_before(index)
            self._codec.reset()
            self._cursor = start - 1
        rgba = None
        for i in range(start, index + 1):
            decoded = self._codec.decode(self._sample(i), self._packets[i][2])
            if decoded is not None:
                rgba = decoded
            self._cursor = i
        return rgba

    def _picture(self, index):
        """The same, for a stream whose two orders differ.

        A B frame is coded after the frame it predicts forwards from, so
        asking for the frame shown fourth can mean decoding the sixth, and
        the fifth and sixth then come out of the buffer for free. Playing a
        B stream straight through therefore still costs one packet per
        frame; what it does not do is replay from the keyframe at every
        second frame, which is what happens if you decode in presentation
        order and pretend nothing is out of place.

        A seek backwards past what is still buffered resets and replays
        from the keyframe, exactly as `_in_order` does. That is the case
        worth being careful about: after a seek the decoder holds reference
        pictures from the old position, and a B frame decoded against those
        is a plausible picture rather than an error, so the reset has to
        happen on the way in and not be noticed later.
        """
        if index in self._pending:
            return self._pending[index]
        target = self._order[index]
        if target <= self._cursor:
            start = self._decode_index(self.keyframe_before(index))
            self._codec.reset()
            self._pending.clear()
            self._cursor = start - 1
        for i in range(self._cursor + 1, target + 1):
            decoded = self._codec.decode(self._sample(i), self._packets[i][2])
            self._cursor = i
            if decoded is not None:
                self._pending[self._shown_at[i]] = decoded
        rgba = self._pending.get(index)
        for shown in [p for p in self._pending if p < index]:
            del self._pending[shown]
        return rgba

    def reset(self):
        self._codec.reset()
        self._cursor = -1
        self._pending = {}


class AudioTrack:
    """A decodable audio stream: frame count, timing, and `frame(i)`.

    The same shape as `VideoTrack`, deliberately, because the thing above
    them -- a scheduler holding a clock -- wants to ask both tracks the same
    questions. A "frame" here is one coded AAC frame: 1024 samples per
    channel, about 23 milliseconds, not one sample.
    """

    def __init__(self, data, info, packets, codec, times=None, asc=b""):
        self._data = data
        self.info = info
        self._packets = packets            # (offset, length, keyframe)
        self._codec = codec
        # `stts` for a sound track is normally one run of equal deltas, but
        # it is allowed to vary and a final short frame is common, so the
        # real per-sample times are carried rather than divided out of a
        # rate that would then be wrong at the end of every file.
        self._times = list(times) if times else None
        self._cursor = -1                  # last index handed to the codec
        self.asc = asc                     # AudioSpecificConfig, verbatim
        self.sample_rate = info.sample_rate
        self.channels = info.channels
        self.sample_count = info.frame_count
        self.duration = info.duration
        self.codec_name = info.codec
        self.container = info.container

    def frame_time(self, index):
        if self._times is not None and 0 <= index < len(self._times):
            return self._times[index][0]
        return 0.0

    def frame_duration(self, index):
        if self._times is not None and 0 <= index < len(self._times):
            return self._times[index][1]
        return 0.0

    def index_at(self, seconds):
        """The coded frame that is playing at `seconds`."""
        if not self._times:
            return 0
        lo, hi = 0, len(self._times)
        while lo < hi:
            mid = (lo + hi) // 2
            if self._times[mid][0] <= seconds:
                lo = mid + 1
            else:
                hi = mid
        return max(0, min(lo - 1, max(0, self.sample_count - 1)))

    def packet(self, index):
        if not 0 <= index < len(self._packets):
            raise MediaError("audio frame %d out of range (0..%d)"
                             % (index, len(self._packets) - 1))
        offset, length, _key = self._packets[index]
        if length > MAX_FRAME_BYTES:
            raise MediaError("audio frame %d is %d bytes" % (index, length))
        end = offset + length
        if end > len(self._data):
            raise MediaError("audio frame %d runs past the end of the file"
                             % index)
        return self._data[offset:end]

    def frame(self, index):
        """Decode coded frame `index`, replaying from the start when it is
        not the one that comes next.

        There is no keyframe to replay from -- AAC has no such concept, every
        frame carries a whole spectrum -- but the decoder still carries the
        previous frame's second MDCT half, so an out-of-order request has to
        start from frame zero. The first frame after a `reset()` is the
        priming frame: it is decoded against silence and comes out
        attenuated, which is correct and is what every AAC decoder does.
        """
        if not 0 <= index < len(self._packets):
            raise MediaError("audio frame %d out of range" % index)
        start = index
        if index != self._cursor + 1:
            start = 0
            self._codec.reset()
            self._cursor = -1
        if index - start >= MAX_FRAMES:
            raise MediaError("replaying %d audio frames is too many"
                             % (index - start))
        channels = self.channels
        samples = b""
        for i in range(start, index + 1):
            _count, channels, samples = self._codec.decode(self.packet(i))
            self._cursor = i
        return AudioFrame(index, self.frame_time(index),
                          self.frame_duration(index), self.sample_rate,
                          channels or self.channels, samples)

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


# WAVEFORMATEX format tags, which is how an AVI names an audio codec: a
# 16-bit number rather than a fourcc. Only the ones worth naming are here --
# 0x00FF and the two 0x16xx are the three spellings of AAC that muxers
# actually wrote, and the last two cover the files most likely to turn up.
WAVE_FORMAT_NAMES = {
    0x0001: "PCM",
    0x0002: "ADPCM",
    0x0055: "MP3",
    0x00FF: "AAC",
    0x1600: "AAC",
    0x1601: "AAC",
    0x2000: "AC-3",
}


def _probe_avi_audio(data):
    """What an AVI's sound is, said honestly and then declined.

    AVI audio is not demuxed here. The chunks are interleaved with the video
    on a schedule the `strf` describes rather than a sample table, and
    getting that wrong produces sound that drifts out of sync over minutes
    rather than sound that is obviously broken -- so the choice is to read
    the format header, say what the stream is, and stop.
    """
    _micros, streams, _packets, _flags, _base = _parse_avi(data)
    stream = None
    for candidate in streams:
        if candidate.kind == "auds":
            stream = candidate
            break
    if stream is None:
        return AudioInfo("AVI", reason="this AVI has no audio stream")
    info = AudioInfo("AVI")
    if len(stream.format) >= 14:
        header = _Reader(stream.format)
        tag = header.u16le()
        info.codec = WAVE_FORMAT_NAMES.get(tag, "0x%04X" % tag)
        info.channels = header.u16le()
        info.sample_rate = header.u32le()
    if stream.rate and stream.scale and stream.length:
        info.duration = stream.length * stream.scale / stream.rate
    info.reason = ("%s in AVI: audio is demuxed out of MP4 and MOV only"
                   % (info.codec or "the audio stream"))
    return info


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


def _aac_reason(exc):
    """The same rule for sound: name the codec exactly once.

    Three things raise on the way to an AAC track and only one of them --
    the decoder -- knows it is AAC. The other two are the descriptor reader
    and the sample-table reader, which are told nothing about the codec they
    are reading for and should not be.
    """
    text = str(exc)
    return text if text.startswith("AAC") else "AAC: %s" % text


def _mp3_reason(exc):
    """And again for Layer III.

    The frame walk and the header reader live in the decoder library, so on a
    machine with no compiler they raise before a single frame is found. That
    is a sentence to put in front of somebody whose file will not play, not
    an exception type the media stack has any reason to know about.
    """
    text = str(exc)
    return text if text.startswith("MP3") else "MP3: %s" % text


def _annexb_track(data, samples, extradata):
    """Every sample of an H.264 track as one run of Annex B bytes.

    Only ever handed to `h264.slice_types`, which reads two fields out of
    each slice header, so this deliberately does not care about access unit
    boundaries. A sample that points outside the file is skipped rather
    than complained about: `_first_keyframe` is the check for that, and it
    gives a better sentence.
    """
    length_size = 0
    if extradata[:1] == b"\x01" and len(extradata) > 4:
        length_size = (extradata[4] & 3) + 1
    out = []
    for offset, length, _keyframe in samples:
        if offset < 0 or length <= 0 or offset + length > len(data):
            continue
        if length > MAX_FRAME_BYTES:
            continue
        sample = data[offset:offset + length]
        if length_size:
            try:
                sample = h264.annexb_from_avcc(sample, length_size)
            except h264.H264Error:
                continue
        out.append(sample)
    return b"".join(out)


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
        self.channels = 0           # stsd, sound tracks only
        self.sample_rate = 0.0      # stsd, in hertz
        self.sample_size = 0        # stsd, bits per PCM sample
        self.object_type = 0        # esds objectTypeIndication
        self.codec = ""
        self.stts = []              # (sample count, duration in ticks)
        self.ctts = []              # (sample count, composition offset)
        self.sync = None            # 1-based sample numbers, or None for all
        self.stsc = []              # (first chunk, samples per chunk)
        # `constant_size` is stsz's own field and is not `sample_size` above:
        # one is a number of bytes in the file and the other a number of bits
        # in a loudspeaker, and a sound track sets both.
        self.constant_size = 0      # non-zero when every sample is that size
        self.sizes = []
        self.chunks = []            # chunk offsets into the file
        self.extradata = b""        # avcC, an AudioSpecificConfig, verbatim


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
        elif kind == "ctts":
            # The composition offsets: how far each sample's presentation
            # time sits from its decode time. A file without B frames has no
            # `ctts` at all, which is why the rest of this reader could
            # ignore it for as long as it did. Version 0 declares the offsets
            # unsigned and version 1 signed; muxers wrote negative offsets
            # into version 0 boxes for years before version 1 existed, and a
            # 2^32-ish "duration" is unmistakably one of those, so read
            # anything past the top of the signed range as negative rather
            # than believing it.
            box.skip(4)
            for _ in range(_table_count(box, 8)):
                count = box.u32be()
                offset = box.u32be()
                if offset >= 0x80000000:
                    offset -= 0x100000000
                track.ctts.append((count, offset))
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
            track.constant_size = box.u32be()
            declared = box.u32be()
            if track.constant_size:
                track.sizes = [track.constant_size] * min(declared, MAX_FRAMES)
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
        elif track.handler == "soun" and body >= _AUDIO_ENTRY_BYTES:
            # A sound entry that will not read must not take the file's
            # picture with it: `probe()` walks every track in the file, and a
            # stranger's malformed `esds` is no reason to refuse to describe
            # a video track that parsed perfectly.
            try:
                _parse_audio_sample_entry(entry, track)
            except MediaError:
                pass
        stsd.skip(body)


# 6 reserved + 2 data reference index, then version, revision and vendor,
# then the four 16-bit fields and the 16.16 sample rate: the smallest
# AudioSampleEntry the spec allows.
_AUDIO_ENTRY_BYTES = 28


def _parse_audio_sample_entry(entry, track):
    """An AudioSampleEntry, in either of QuickTime's three versions.

    Version 0 is the ISO one and the only one an MP4 muxer writes. Versions 1
    and 2 are QuickTime's, and they matter here for one reason each: version
    1 appends sixteen bytes before the child boxes, so a parser that does not
    know about it looks for `esds` sixteen bytes early and finds nothing; and
    version 2 carries the sample rate as a float64, which is the only way the
    format can express the rates a 16.16 fixed-point field cannot.
    """
    entry.skip(8)                            # reserved, data reference index
    version = entry.u16be()
    entry.skip(6)                            # revision, vendor
    track.channels = entry.u16be()
    track.sample_size = entry.u16be()
    entry.skip(4)                            # compression id, packet size
    rate = entry.u16be()                     # 16.16 fixed point
    fraction = entry.u16be()
    track.sample_rate = rate + fraction / 65536.0
    if version == 1:
        # samples per packet, bytes per packet, bytes per frame, bytes per
        # sample -- all four are about uncompressed audio and none of them
        # tell us anything an AAC track has not already said.
        entry.skip(16)
    elif version == 2:
        entry.skip(4)                        # size of the struct that follows
        track.sample_rate = struct.unpack(">d", entry.take(8))[0]
        track.channels = entry.u32be()
        entry.skip(20)                       # constant bits/bytes per packet
    _parse_sample_extensions(entry, track)


# MPEG-4 descriptor tags, ISO/IEC 14496-1. Only the three on the path from
# the `esds` box down to the AudioSpecificConfig are named, because the rest
# are skipped by length like any other descriptor.
_TAG_ES = 0x03
_TAG_DECODER_CONFIG = 0x04
_TAG_DECODER_SPECIFIC = 0x05


def _descriptors(reader):
    """Yield (tag, body reader) for one level of an MPEG-4 descriptor chain.

    The length is the awkward part: one to four bytes, seven bits of payload
    each, with the top bit meaning "another byte follows". Four is the limit
    the standard sets and the limit enforced here, because the encoding
    itself has no end -- a run of 0x80 bytes is a valid prefix of a length
    forever, and a reader that keeps taking them is a reader a file can hang.
    """
    seen = 0
    while reader.remaining() >= 2:
        seen += 1
        if seen > MAX_CHUNKS:
            raise MediaError("MPEG-4 descriptor chain does not terminate")
        tag = reader.u8()
        size = 0
        for _ in range(4):
            byte = reader.u8()
            size = (size << 7) | (byte & 0x7F)
            if not byte & 0x80:
                break
        # A length longer than what is left is a truncated file, not a reason
        # to read the descriptor after it: clamp and carry on, the same way
        # the RIFF and box walkers do with a short tail.
        size = min(size, reader.remaining())
        start = reader.pos
        yield tag, _Reader(reader.data, start, start + size)
        # The header is already consumed, so this advances even for a
        # zero-length descriptor and the walk cannot stand still.
        reader.pos = start + size


def _parse_esds(box, track):
    """`esds` -> ES_Descriptor -> DecoderConfigDescriptor -> the config.

    The AudioSpecificConfig at the bottom is what an AAC decoder is
    configured from -- sample rate, channel configuration, object type -- and
    an MP4's audio samples carry none of it, exactly as an H.264 sample
    carries no SPS. So it is stored verbatim in the same field `avcC` uses:
    the two are the same kind of thing, a codec's private setup blob, and
    neither is interpreted here.
    """
    box.skip(4)                              # full box version and flags
    for tag, es in _descriptors(box):
        if tag != _TAG_ES:
            continue
        es.skip(2)                           # ES_ID
        flags = es.u8()
        if flags & 0x80:
            es.skip(2)                       # dependsOn_ES_ID
        if flags & 0x40:
            es.skip(es.u8())                 # URL, as a Pascal string
        if flags & 0x20:
            es.skip(2)                       # OCR_ES_ID
        for config_tag, config in _descriptors(es):
            if config_tag != _TAG_DECODER_CONFIG:
                continue
            track.object_type = config.u8()
            config.skip(12)                  # stream type, buffer, bitrates
            for specific_tag, specific in _descriptors(config):
                if specific_tag == _TAG_DECODER_SPECIFIC:
                    track.extradata = bytes(
                        specific.data[specific.pos:specific.end])
                    return
            return
        return


def _parse_sample_extensions(entry, track, depth=1):
    for kind, box in _boxes(entry, depth):
        if kind == "avcC" and not track.extradata:
            track.extradata = bytes(box.data[box.pos:box.end])
        elif kind == "esds" and not track.extradata:
            _parse_esds(box, track)
        elif kind == "wave" and depth < MAX_DEPTH:
            # QuickTime does not put `esds` beside the sample entry's other
            # children; it wraps it, and whatever else the codec brought,
            # in a `wave` box. Files written by iTunes and by every version
            # of QuickTime Player look like this, so both layouts are read.
            _parse_sample_extensions(box, track, depth + 1)


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
    """Per-sample (pts, duration) in seconds, from `stts`.

    In decode order, and equal to the presentation times only for a track
    with no `ctts`. `_mp4_order` below is what turns these into the order
    and the times a viewer sees.
    """
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


def _mp4_order(track, times, count):
    """Presentation order and presentation times, from `ctts`.

    Returns `(order, times)` where `order[p]` is the decode-order sample
    shown p-th, or `(None, times)` when the file says the two orders are
    the same -- which is every file without B frames, and is why nothing
    here existed until there was a decoder that could produce them.

    A sample's composition time is its decode time plus its `ctts` offset,
    and presentation order is those times sorted. The sort is stable, so
    two samples that claim the same instant stay in decode order rather
    than swapping on a detail of the sort; a file that does that is broken
    either way and this at least makes it decode.

    Durations are recomputed as the gap to the next frame shown rather than
    carried across from `stts`, because `stts` durations are decode-order
    gaps and a B frame's is not how long it is on screen. The last frame
    keeps its `stts` duration -- there is no next frame to measure against
    and its own is the only number anybody has.
    """
    if not track.ctts or count <= 0:
        return None, times
    timescale = track.timescale or 600
    offsets = []
    for run, offset in track.ctts:
        if len(offsets) >= count:
            break
        offsets.extend([offset] * min(run, count - len(offsets)))
    # A short `ctts` means the rest of the samples have no offset, which is
    # what a muxer that stopped writing B frames partway leaves behind.
    offsets.extend([0] * (count - len(offsets)))
    composed = [times[i][0] + offsets[i] / timescale for i in range(count)]
    order = sorted(range(count), key=lambda i: composed[i])
    if order == list(range(count)):
        # A `ctts` that shifts every sample by the same amount, which is how
        # some muxers spell "the first frame starts at zero". It reorders
        # nothing, so say so and let the caller keep the simple path.
        return None, times
    # Composition times need not start at zero -- a positive offset on every
    # sample is one way of spelling "there is nothing before this" -- and the
    # rest of the browser takes the first frame to be at t=0, so the whole
    # timeline slides back to meet it. Edit lists, which are the proper way
    # to say the same thing, are not read here either.
    base = composed[order[0]]
    shown = []
    for p, i in enumerate(order):
        pts = composed[i] - base
        if p + 1 < count:
            duration = composed[order[p + 1]] - composed[i]
        else:
            duration = times[i][1]
        shown.append((pts, max(0.0, duration)))
    return order, shown


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
    order, times = _mp4_order(video, times, len(samples))
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
    keyframes = [i for i, s in enumerate(samples) if s[2]]
    if order is not None:
        shown_at = [0] * len(order)
        for position, packet in enumerate(order):
            shown_at[packet] = position
        keyframes = sorted(shown_at[i] for i in keyframes)
    return VideoTrack(data, info, samples, decoder, frame_rate, keyframes,
                      times=times, order=order)


def _open_mp3(data):
    """An `AudioTrack` over a bare MPEG Layer III stream.

    There is no container here to hold a duration or a frame table, so both
    are built by walking the frames: each header says how long its frame is
    and how many samples it carries, and a stream is allowed to change
    bitrate between frames, which is what a variable-bitrate file is. The
    times are accumulated rather than divided out of one rate for the same
    reason `AudioTrack` carries `stts` verbatim -- a length computed from
    the first frame's bitrate is wrong for every VBR file there is.

    A frame this walk cannot use is skipped rather than fatal: an ID3v1 tag
    at the end, a stray byte between frames, or rubbish appended by
    something that thought the file was text are all things a player is
    expected to keep playing through.
    """
    ball = _mp3_module()
    packets = []
    times = []
    clock = 0.0
    rate = 0
    channels = 0
    try:
        for offset, length in ball.frames(data):
            header = ball.frame_header(data[offset:offset + 4])
            if header is None or not header["sample_rate"]:
                continue
            rate = header["sample_rate"]
            channels = header["channels"]
            duration = header["samples"] / float(rate)
            packets.append((offset, length, True))
            times.append((clock, duration))
            clock += duration
    except ball.Mp3Error as exc:
        raise MediaError(_mp3_reason(exc))
    if not packets:
        raise MediaError("MP3: no frames in this file")
    info = AudioInfo("MP3", codec="MP3", sample_rate=rate, channels=channels,
                     duration=clock, frame_count=len(packets), supported=True)
    return AudioTrack(data, info, packets, _Mp3(), times=times)


def _probe_mp3(data):
    """What a bare Layer III stream is, whether or not we can decode it.

    A machine with no gfortran still has to be able to say "MP3, and here is
    why it will not play", so the failure to build a track is turned into a
    reason rather than raised.
    """
    try:
        return _open_mp3(data).info
    except MediaError as exc:
        return AudioInfo("MP3", codec="MP3", reason=str(exc))


def _probe_mp4(data, container="MP4"):
    try:
        return _open_mp4(data, container).info
    except _Unsupported as exc:
        return exc.info


def _audio_reason(codec, object_type):
    """What to say about a sound track we are not going to decode.

    One sentence, naming the codec, in the same spirit as the video table:
    an `mp4a` entry is a family rather than a codec, so the descriptor's
    objectTypeIndication is what actually decides, and an MP3 in an `mp4a`
    box has to be called MP3 rather than AAC.
    """
    if codec == "mp4a":
        if object_type in MP3_OBJECT_TYPES:
            return "MP3: no decoder"
        if object_type and object_type not in AAC_OBJECT_TYPES:
            return ("no decoder for MPEG-4 audio object type 0x%02X"
                    % object_type)
    return KNOWN_UNDECODABLE_AUDIO.get(
        codec, "no decoder for %s" % (codec or "this file's audio codec"))


def _open_mp4_audio(data, container="MP4"):
    """An `AudioTrack` over an MP4/MOV, or `_Unsupported` carrying what we
    learned about it -- the same bargain `_open_mp4` makes for pictures, and
    for the same reason: the numbers in the "no decoder" message are the
    numbers a player that had one would have used."""
    movie_duration, tracks = _parse_mp4(data)
    audio = None
    for track in tracks:
        if track.handler == "soun":
            audio = track
            break
    if audio is None:
        info = AudioInfo(container, reason="%s has no audio track" % container)
        raise _Unsupported(info)

    codec = audio.codec or ""
    duration = movie_duration
    if audio.timescale and audio.duration_ticks:
        duration = audio.duration_ticks / audio.timescale
    info = AudioInfo(container, codec, int(round(audio.sample_rate)),
                     audio.channels, duration, 0)
    try:
        samples = _mp4_samples(audio)
    except MediaError as exc:
        info.reason = _audio_reason(codec, audio.object_type)
        if codec == "mp4a" and audio.object_type in AAC_OBJECT_TYPES:
            info.reason = _aac_reason(exc)
        raise _Unsupported(info)
    info.frame_count = len(samples)
    times = _mp4_times(audio, len(samples))
    if duration <= 0 and times:
        duration = times[-1][0] + times[-1][1]
        info.duration = duration

    if codec != "mp4a" or audio.object_type not in AAC_OBJECT_TYPES:
        info.reason = _audio_reason(codec, audio.object_type)
        raise _Unsupported(info)
    if not audio.extradata:
        info.reason = ("AAC: this track carries no AudioSpecificConfig, and "
                       "an AAC decoder cannot be configured without one")
        raise _Unsupported(info)
    try:
        aac = _aac_module()
        if not aac.available():
            raise MediaError(aac.unavailable_reason() or "no decoder")
        why = aac.probe(audio.extradata)
        if why:
            raise MediaError(why)
        decoder = _Aac(audio.extradata)
    except MediaError as exc:
        info.reason = _aac_reason(exc)
        raise _Unsupported(info)
    # The AudioSpecificConfig is what made the samples, so where it and the
    # sample entry disagree -- which is what HE-AAC does, coding at half the
    # rate the track declares -- the config wins, and `info` is corrected
    # rather than left saying something the frames will contradict.
    if decoder.sample_rate > 0:
        info.sample_rate = decoder.sample_rate
    if decoder.channels > 0:
        info.channels = decoder.channels
    info.supported = True
    return AudioTrack(data, info, samples, decoder, times=times,
                      asc=audio.extradata)


def _probe_mp4_audio(data, container="MP4"):
    try:
        return _open_mp4_audio(data, container).info
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
            elif codec.startswith("A_"):
                # The audio track's name, kept in `timing` rather than on
                # `info`: `info` is a MediaInfo with one `codec` field, and
                # a file has both kinds of track. `_probe_webm_audio` reads
                # it back out; `probe()` never looks and so cannot change.
                timing.setdefault("audio", codec)
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


# What Matroska's CodecIDs are called in a sentence a person reads.
_WEBM_AUDIO_NAMES = {
    "A_AAC": "AAC",
    "A_OPUS": "Opus",
    "A_VORBIS": "Vorbis",
    "A_MPEG/L3": "MP3",
    "A_AC3": "AC-3",
    "A_FLAC": "FLAC",
}


def _probe_webm_audio(data):
    """The same walk as `_probe_webm`, read for its sound instead.

    Nothing is demuxed: a Matroska block is inside a cluster inside a
    lacing scheme, none of which this parser reads. What it can do is give
    the codec a name and the file a duration, which is the whole of what an
    honest refusal needs.
    """
    scratch = MediaInfo("WebM")
    timing = {"scale": 1000000, "ticks": 0.0}
    _ebml_walk(_Reader(data), scratch, timing)
    codec = timing.get("audio", "")
    info = AudioInfo("WebM", _WEBM_AUDIO_NAMES.get(codec, codec))
    info.duration = ((timing["ticks"] or 0.0)
                     * (timing["scale"] or 1000000) / 1e9)
    if not codec:
        info.reason = "this WebM has no audio track"
    else:
        info.reason = ("%s in WebM: audio is demuxed out of MP4 and MOV only"
                       % info.codec)
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


def _looks_like_mp3(data):
    """Is there an MPEG audio frame header at the front of this?

    Pure Python, and a duplicate of a few lines the Fortran already has, on
    purpose: sniffing is what `probe()` does on a machine with no gfortran,
    and a sniffer that needed the decoder would make an MP3 unidentifiable
    on exactly the machines where saying what it is matters most.

    Only the fields that cannot legally hold their reserved value are
    checked. That is enough to reject text and pictures and not enough to
    accept a false sync inside audio data as a whole file -- but nothing
    here has to: this decides which parser looks next, and that parser
    finds the real frames itself.
    """
    if len(data) >= 10 and data[:3] == b"ID3":
        return True
    if len(data) < 4 or data[0] != 0xFF or (data[1] & 0xE0) != 0xE0:
        return False
    version = (data[1] >> 3) & 3
    layer = (data[1] >> 1) & 3
    bitrate = (data[2] >> 4) & 15
    rate = (data[2] >> 2) & 3
    return version != 1 and layer != 0 and bitrate != 15 and rate != 3


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
    # Last of the four-byte tests, because an MPEG audio frame header is the
    # weakest evidence of the four: eleven bits of sync and a handful of
    # fields that merely have to be legal. Everything with a real magic
    # number has already had its say.
    if _looks_like_mp3(data):
        return "MP3"
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
    if kind == "MP3":
        return MediaInfo("MP3", codec="MP3",
                         reason="an MP3 is sound and nothing else: there is "
                                "no picture in it")
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


def probe_audio(data):
    """What the file's sound is, whether or not we can play it.

    The audio half of `probe()`, with the same bargain: never raises for a
    file we merely lack a codec for -- including a file with no audio track
    at all, which comes back as an AudioInfo saying so -- and still raises
    `MediaError` for bytes that are not a container we know, or for one that
    is malformed.
    """
    kind = sniff(data)
    if kind in ("MP4", "MOV"):
        return _probe_mp4_audio(data, kind)
    if kind == "AVI":
        return _probe_avi_audio(data)
    if kind == "WebM":
        return _probe_webm_audio(data)
    if kind == "MJPEG":
        return AudioInfo("MJPEG",
                         reason="a bare MJPEG stream is pictures and nothing "
                                "else: there is no audio in it")
    if kind == "MP3":
        return _probe_mp3(data)
    raise MediaError("not a media container we recognise")


def open_audio(data):
    """An `AudioTrack` for a file whose sound we can actually decode.

    Raises `MediaError` otherwise -- including for a perfectly good MP4 with
    an AC-3 track in it, whose `probe_audio()` result is still worth showing.
    """
    kind = sniff(data)
    if kind in ("MP4", "MOV"):
        return _open_mp4_audio(data, kind)
    if kind == "MP3":
        return _open_mp3(data)
    raise _Unsupported(probe_audio(data))
