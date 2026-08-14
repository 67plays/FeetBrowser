"""Video containers and video codecs, decoded to raw RGBA.

This is the bytes half of video: it turns a file into a sequence of frames
with times on them. It knows nothing about clocks, threads, layout or the
screen -- `media.py` does that -- so everything here is a pure function of the
input file and can be tested by reading a byte array.

What is actually decoded to pixels:

  AVI (RIFF) with `BI_RGB`   uncompressed 8/24/32-bit DIB frames
  AVI (RIFF) with `BI_RLE8`  Microsoft's 8-bit run-length codec, including
                             the delta frames that make it inter-frame

Those two are here because they are decodable from scratch in a few hundred
lines and are encumbered by nothing. Everything else in this module *reads*
the file and refuses honestly: an MP4 or a WebM is walked far enough to
report its dimensions, duration and codec name, and then declines, because
saying "1280x720, H.264, 4.0s, no decoder" is useful and pretending to play
it is not. See docs/media.md.

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

from .imagecodec import MAX_PIXELS

__all__ = ["MediaError", "VideoFrame", "VideoTrack", "MediaInfo",
           "open_video", "probe", "sniff", "MAX_FRAMES", "MAX_CHUNKS"]


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


# fourccs we can name but not decode. Naming them is the point: the element
# can say "MJPG" instead of "unknown", and the next contributor can see
# exactly where a decoder plugs in.
KNOWN_UNDECODABLE = {
    "MJPG": "Motion JPEG: needs a baseline JPEG decoder, which this project "
            "does not have yet",
    "mjpg": "Motion JPEG: needs a baseline JPEG decoder, which this project "
            "does not have yet",
    "jpeg": "Motion JPEG: needs a baseline JPEG decoder, which this project "
            "does not have yet",
    "H264": "H.264: a from-scratch decoder is a multi-month project",
    "h264": "H.264: a from-scratch decoder is a multi-month project",
    "avc1": "H.264: a from-scratch decoder is a multi-month project",
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

    def __init__(self, data, info, packets, codec, frame_rate, keyframes):
        self._data = data
        self.info = info
        self._packets = packets            # (offset, length, keyframe)
        self._codec = codec
        self.frame_rate = frame_rate
        self._keyframes = keyframes
        self._cursor = -1                  # last index handed to the codec
        self.width = info.width
        self.height = info.height
        self.frame_count = info.frame_count
        self.duration = info.duration
        self.codec_name = info.codec
        self.container = info.container

    def frame_time(self, index):
        return index / self.frame_rate if self.frame_rate else 0.0

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
                          1.0 / self.frame_rate if self.frame_rate else 0.0,
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

    ours = []
    ordinal = 0
    for stream_id, kind, offset, length in packets:
        if stream_id != video_index or kind not in ("dc", "db"):
            continue
        flags = index_flags.get((stream_id, ordinal))
        if flags is None:
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

    codec_name = _fourcc_of(compression)
    duration = len(ours) / frame_rate
    info = MediaInfo("AVI", codec_name, width, height, duration, len(ours))

    if compression == BI_RGB:
        codec = _RawDib(width, height, bit_count, palette, top_down)
    elif compression == BI_RLE8:
        if bit_count != 8:
            raise MediaError("BI_RLE8 with %d bits per pixel" % bit_count)
        codec = _Rle8(width, height, palette, top_down)
    else:
        info.reason = KNOWN_UNDECODABLE.get(
            codec_name, "no decoder for %s" % codec_name)
        raise _Unsupported(info)
    info.supported = True
    return VideoTrack(data, info, ours, codec, frame_rate,
                      [i for i, p in enumerate(ours) if p[2]])


class _Unsupported(MediaError):
    """Raised inside the openers when the file parsed fine and we simply do
    not have the codec. Carries the MediaInfo so `probe()` can report real
    numbers for a file `open_video()` refuses."""

    def __init__(self, info):
        MediaError.__init__(self, info.reason or "unsupported codec")
        self.info = info


# -- MP4 / ISO base media (probe only) ---------------------------------------

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


def _probe_mp4(data):
    info = MediaInfo("MP4")
    reader = _Reader(data)
    for kind, box in _boxes(reader):
        if kind == "moov":
            _probe_moov(box, info)
    if not info.reason:
        info.reason = KNOWN_UNDECODABLE.get(
            info.codec,
            "no decoder for %s" % (info.codec or "this MP4's video codec"))
    return info


def _probe_moov(moov, info):
    for kind, box in _boxes(moov, 1):
        if kind == "mvhd":
            version = box.u8()
            box.skip(3)
            if version == 1:
                box.skip(16)
                timescale = box.u32be()
                duration = box.u64be()
            else:
                box.skip(8)
                timescale = box.u32be()
                duration = box.u32be()
            if timescale:
                info.duration = duration / timescale
        elif kind == "trak":
            _probe_trak(box, info)


def _probe_trak(trak, info):
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
                info.width = int(round(width))
                info.height = int(round(height))
        elif kind == "mdia":
            for sub_kind, sub in _boxes(box, 3):
                if sub_kind == "minf":
                    for m_kind, m in _boxes(sub, 4):
                        if m_kind == "stbl":
                            _probe_stbl(m, info)


def _probe_stbl(stbl, info):
    for kind, box in _boxes(stbl, 5):
        if kind != "stsd":
            continue
        box.skip(4)                          # version + flags
        count = box.u32be()
        for _ in range(min(count, 16)):
            if box.remaining() < 8:
                break
            entry_size = box.u32be()
            fourcc = box.fourcc()
            if fourcc in ("avc1", "hvc1", "hev1", "mp4v", "vp09", "av01"):
                info.codec = fourcc
            if entry_size < 8:
                break
            box.skip(min(entry_size - 8, box.remaining()))


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


# -- entry points ------------------------------------------------------------

def sniff(data):
    """The container this looks like, or "" -- by magic bytes only, because
    a URL's extension is a claim and the first twelve bytes are evidence."""
    if len(data) < 12:
        return ""
    if data[:4] == b"RIFF" and data[8:12] == b"AVI ":
        return "AVI"
    if data[4:8] == b"ftyp":
        return "MP4"
    if data[:4] == b"\x1a\x45\xdf\xa3":
        return "WebM"
    return ""


def probe(data):
    """What the file is, whether or not we can play it.

    Never raises for a file we merely lack a codec for; that case comes back
    as a MediaInfo with `supported` False and a `reason`. It still raises
    `MediaError` for bytes that are not a container we know at all, and for a
    container that is malformed.
    """
    kind = sniff(data)
    if kind == "AVI":
        try:
            track = _open_avi(data)
        except _Unsupported as exc:
            return exc.info
        return track.info
    if kind == "MP4":
        return _probe_mp4(data)
    if kind == "WebM":
        return _probe_webm(data)
    raise MediaError("not a media container we recognise")


def open_video(data):
    """A `VideoTrack` for a file we can actually decode.

    Raises `MediaError` otherwise -- including for a well-formed MP4, whose
    `probe()` result is still worth showing.
    """
    kind = sniff(data)
    if kind != "AVI":
        info = probe(data)
        raise _Unsupported(info)
    return _open_avi(data)
