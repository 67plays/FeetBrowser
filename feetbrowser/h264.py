"""H.264 video decoding, in Fortran, loaded through ctypes.

The decoder itself is in ``fortran/`` -- fixed-form FORTRAN 77, about seven
thousand lines of it, compiled on demand by gfortran into a shared library
that this module loads and calls. Nothing here decodes anything; this is
the part that finds a compiler, keeps the build cached, converts between
the two shapes an H.264 stream comes in, and gets out of the way when the
machine has no Fortran on it.

Why Fortran: the arithmetic decoder in clause 9.3 is a serial dependency
chain -- one table lookup, one subtract, one compare, per *bit* -- and a
bitstream is millions of bits. Python does that at a few hundred thousand
bins a second, which is about four orders of magnitude short of a video.
Fortran does it at 120 million. The rest of the codec followed the
entropy layer across the boundary because splitting a decoder in half at
the macroblock level would mean marshalling every coefficient.

What decodes today: I, P and B slices, either entropy coder -- CABAC or
CAVLC -- 4:2:0 8-bit, Baseline, Main and High profile. Every intra
prediction mode, both transform sizes, scaling matrices, quarter-sample
motion compensation, explicit and implicit weighted prediction,
bi-prediction, both spatial and temporal direct prediction, up to four
reference frames, the deblocking filter. Not interlace, not SP or SI
slices, and not a B slice under CAVLC -- see the module docstring in
``fortran/h264api.f`` for the status codes each of those produces.

B slices decode in decode order, which is not presentation order. This
module hands back a picture per access unit and says nothing about when
it should be shown -- only what its picture order count is, through
``Decoder.poc``. Reordering is the container's business and lives in
``mediacodec``, which has the composition offsets to do it with.

The library holds one decoder's worth of state in COMMON blocks, which
is to say a single global one. ``_LOCK`` is what stops two ``<video>``
elements from interleaving their macroblocks; it is not an optimisation
to remove later, it is load-bearing.

Inter slices made that state persistent, which makes it sharper still: a
frame is now decoded against the pictures the previous calls left behind,
so a second ``Decoder`` touching the library does not merely slow the
first one down, it invalidates it. ``_owner`` tracks whose pictures are
in the buffer, and a decoder that finds it is not the owner replays its
own stream from the last IDR before decoding. In the ordinary case of one
video playing there is no replay and no copy.
"""

import ctypes
import hashlib
import os
import platform
import subprocess
import sys
import tempfile
import threading

_HERE = os.path.dirname(os.path.abspath(__file__))
_FORTRAN = os.path.join(os.path.dirname(_HERE), "fortran")

# Compilation order does not matter to gfortran here -- there are no modules,
# only COMMON blocks and an INCLUDE -- but a fixed order keeps the cache key
# stable across filesystems that list a directory in their own order.
_SOURCES = ("h264ctx.f", "h264tab.f", "h264bits.f", "h264ps.f", "h264mb.f",
            "h264cav.f", "h264pred.f", "h264rec.f", "h264mc.f", "h264dpb.f",
            "h264dbl.f", "h264api.f")
_INCLUDES = ("h264com.inc",)

# The version H2VERS reports. A library left in the cache by an older
# checkout has the old entry points and the old meanings, and calling it
# would be worse than not having one.
_ABI = 4

_LOCK = threading.Lock()
_lib = None
_load_error = None
_loaded = False

# Which Decoder's pictures are in the library's decoded picture buffer.
# Guarded by _LOCK, like the buffer itself.
_owner = None


class H264Error(Exception):
    """A stream this decoder cannot decode, or a build that did not happen."""


# -- building ----------------------------------------------------------------

def _find_gfortran():
    for name in ("gfortran", "gfortran-15", "gfortran-14", "gfortran-13",
                 "gfortran-12", "gfortran-11"):
        try:
            subprocess.run([name, "--version"], check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return name
        except (OSError, subprocess.CalledProcessError):
            continue
    return None


def _library_suffix():
    system = platform.system()
    if system == "Darwin":
        return ".dylib"
    if system == "Windows":
        return ".dll"
    return ".so"


def _digest():
    """A hash over every source, so a changed decoder rebuilds itself.

    The compiler's identity goes in too. The same sources built by two
    gfortrans are two different libraries, and a cache that cannot tell
    them apart hands the wrong one to whichever runs second.
    """
    sha = hashlib.sha256()
    sha.update(("abi%d" % _ABI).encode("ascii"))
    for name in _INCLUDES + _SOURCES:
        with open(os.path.join(_FORTRAN, name), "rb") as handle:
            sha.update(handle.read())
    return sha.hexdigest()[:16]


def _compile(fc, out):
    """Build the shared library, or raise. Called at most once per machine
    per version of the sources."""
    tmp = out + ".%d.tmp" % os.getpid()
    # -march=native is worth having and is not portable: gfortran on Apple
    # silicon rejects it outright, and a binary built with it on one machine
    # is not safe to run on another. So it is tried and dropped, rather than
    # detected -- the compiler is the only authority on what it accepts.
    base = ["-O3", "-shared", "-fPIC", "-std=legacy", "-fno-align-commons",
            "-I", _FORTRAN, "-o", tmp]
    sources = [os.path.join(_FORTRAN, name) for name in _SOURCES]
    for extra in (["-march=native"], []):
        try:
            subprocess.run([fc] + base + extra + sources, check=True,
                           stdout=subprocess.DEVNULL,
                           stderr=subprocess.PIPE)
        except subprocess.CalledProcessError as exc:
            last = exc
            continue
        except OSError as exc:
            raise H264Error("could not run %s: %s" % (fc, exc))
        os.replace(tmp, out)
        return
    detail = (last.stderr or b"").decode("utf8", "replace").strip()
    raise H264Error("gfortran could not build the decoder: %s"
                    % (detail.splitlines()[-1] if detail else "no output"))


def _open_library():
    if not os.path.isdir(_FORTRAN):
        raise H264Error("the fortran/ directory is missing from this checkout")
    fc = _find_gfortran()
    if fc is None:
        raise H264Error("no gfortran on PATH")
    out = os.path.join(tempfile.gettempdir(),
                       "feetbrowser_h264_%s%s" % (_digest(), _library_suffix()))
    if not os.path.exists(out):
        _compile(fc, out)
    lib = ctypes.CDLL(out)
    for name in ("h264_version", "h264_reset", "h264_dims", "h264_decode",
                 "h264_i420", "h264_rgba", "h264_poc"):
        getattr(lib, name).restype = None
    version = ctypes.c_int(0)
    lib.h264_version(ctypes.byref(version))
    if version.value != _ABI:
        raise H264Error("the built decoder reports ABI %d, expected %d"
                        % (version.value, _ABI))
    return lib


def _library():
    """The loaded library, or None. Every failure is remembered: a machine
    with no gfortran must not try to run it once per frame."""
    global _lib, _load_error, _loaded
    if _loaded:
        return _lib
    with _LOCK:
        if _loaded:
            return _lib
        try:
            _lib = _open_library()
        except (H264Error, OSError) as exc:
            _lib = None
            _load_error = str(exc)
        _loaded = True
    return _lib


def available():
    """True when this machine can decode H.264."""
    return _library() is not None


def unavailable_reason():
    """Why not, in a form fit to show a user. None when it is available."""
    if _library() is not None:
        return None
    return _load_error or "the H.264 decoder is not available"


# -- the two shapes a stream comes in ----------------------------------------

_START = b"\x00\x00\x00\x01"


def annexb_from_avcc(sample, length_size):
    """One MP4 sample -- length-prefixed NAL units -- as Annex B bytes.

    MP4 stores each NAL unit behind a big-endian length of 1, 2 or 4 bytes;
    Annex B, which is what the decoder reads and what every .264 file on
    disk contains, separates them with start codes instead. The payload is
    identical, so this is a reframing and not a transcode.
    """
    if length_size not in (1, 2, 4):
        raise H264Error("avcC says NAL lengths are %d bytes" % length_size)
    out = bytearray()
    pos = 0
    end = len(sample)
    while pos + length_size <= end:
        size = int.from_bytes(sample[pos:pos + length_size], "big")
        pos += length_size
        if size <= 0 or pos + size > end:
            # A truncated sample is common in files that were cut with a tool
            # that did not understand them. Everything up to the damage is
            # still a decodable picture, so keep it rather than refuse.
            break
        out += _START
        out += sample[pos:pos + size]
        pos += size
    if not out:
        raise H264Error("no NAL units in this sample")
    return bytes(out)


def parameter_sets_from_avcc(avcc):
    """The SPS and PPS out of an `avcC` box, as Annex B, plus the NAL
    length size the samples use.

    The box is AVCDecoderConfigurationRecord: a fixed seven-byte head, then
    a count of SPSs and a count of PPSs, each entry a big-endian length and
    that many bytes. Anything after those is an extension this decoder has
    no use for -- the High-profile trailer repeats chroma_format_idc and
    the bit depths, which the SPS itself already said.
    """
    if len(avcc) < 7:
        raise H264Error("avcC box is %d bytes" % len(avcc))
    if avcc[0] != 1:
        raise H264Error("avcC configurationVersion %d" % avcc[0])
    length_size = (avcc[4] & 3) + 1
    out = bytearray()
    pos = 5
    for count_mask in (0x1F, 0xFF):
        if pos >= len(avcc):
            raise H264Error("avcC box ends before its parameter sets")
        count = avcc[pos] & count_mask
        pos += 1
        for _ in range(count):
            if pos + 2 > len(avcc):
                raise H264Error("avcC box ends inside a parameter set")
            size = int.from_bytes(avcc[pos:pos + 2], "big")
            pos += 2
            if pos + size > len(avcc):
                raise H264Error("avcC box ends inside a parameter set")
            out += _START
            out += avcc[pos:pos + size]
            pos += size
    if not out:
        raise H264Error("avcC box carries no parameter sets")
    return bytes(out), length_size


# -- the decoder -------------------------------------------------------------

# What the Fortran returns. The numbers are grouped by the routine that
# produces them so that a bug report says where to look: -1..-8 the sequence
# parameter set, -11..-15 the picture parameter set, -20..-22 the slice data,
# -30..-33 the framing, -41..-50 the slice header, -51..-54 the decoded
# picture buffer and inter prediction.
_STATUS = {
    -1: "the SPS has more than 255 poc reference frames",
    -2: "the SPS ran off the end of its own NAL unit",
    -3: "not 8-bit 4:2:0 -- this decoder does one chroma format",
    -4: "an interlaced stream (frame_mbs_only_flag is 0)",
    -5: "the SPS gives the picture no size",
    -6: "the picture is larger than this decoder's fixed buffers",
    -7: "the SPS crops the picture away to nothing",
    -8: "the stream asks for more reference frames than this decoder keeps",
    -9: "lossless coding (transform bypass), which x264 uses at --qp 0",
    -11: "a PPS arrived before any SPS",
    -12: "the PPS ran off the end of its own NAL unit",
    -13: "slice groups (FMO), which no browser stream uses",
    -14: "the PPS gives an impossible pic_init_qp",
    -15: "the PPS gives an impossible default reference list length",
    -20: "the slice claims more macroblocks than the picture has",
    -21: "the arithmetic decoder lost sync with the stream",
    -22: "a macroblock ran off the end of the slice",
    -24: "a CAVLC codeword or syntax element the standard has no value for",
    -30: "a slice arrived before its SPS and PPS",
    -32: "no slice in this access unit",
    -33: "the NAL unit is larger than the decoder's buffer",
    -41: "a slice arrived before its SPS and PPS",
    -42: "pic_order_cnt_type 1, which no browser stream uses",
    -43: "an SP or SI slice -- this decoder does I, P and B slices",
    -44: "the slice header ran off the end of its own NAL unit",
    -45: "the slice header gives an impossible quantiser",
    -46: "the slice starts past the end of the picture",
    -47: "an unknown deblocking filter mode",
    -48: "an unknown cabac_init_idc",
    -49: "the slice gives an impossible reference list length",
    -50: "long-term references, which this decoder does not implement",
    -51: "an inter slice with no reference picture to predict from",
    -52: "the slice reorders in a picture that is not in the buffer",
    -53: "the decoded picture buffer has no free slot",
    -54: "a partition points at a reference index with no picture behind it",
    -55: "temporal direct prediction without direct_8x8_inference_flag",
    -56: "a B slice coded with CAVLC, a combination this decoder refuses",
}


def _explain(status):
    return _STATUS.get(status, "decoder status %d" % status)


def _nal_types(data):
    """The nal_unit_type of every NAL unit in some Annex B bytes.

    Only the header byte after each start code is read, which is all that
    is needed to spot an IDR; the payload is the Fortran's business.
    """
    out = []
    pos = data.find(b"\x00\x00\x01")
    while pos >= 0 and pos + 3 < len(data):
        out.append(data[pos + 3] & 0x1F)
        pos = data.find(b"\x00\x00\x01", pos + 3)
    return out


def _unescape(chunk):
    """7.4.1.1 in the small: drop the emulation prevention bytes.

    Only ever called on the first few bytes of a slice, where an inserted
    0x03 is vanishingly unlikely -- but "unlikely" is how a decoder reads a
    field one bit out of place on somebody else's file.
    """
    out = bytearray()
    zeros = 0
    for byte in chunk:
        if zeros >= 2 and byte == 3:
            zeros = 0
            continue
        out.append(byte)
        zeros = zeros + 1 if byte == 0 else 0
    return bytes(out)


def _ue(bits, pos):
    """ue(v) at bit `pos`, returning the value and the bit after it."""
    zeros = 0
    while pos < len(bits) * 8 and not (bits[pos // 8] >> (7 - pos % 8)) & 1:
        zeros += 1
        pos += 1
        if zeros > 31:
            raise ValueError("exp-Golomb code with no end")
    pos += 1
    value = (1 << zeros) - 1
    for _ in range(zeros):
        if pos >= len(bits) * 8:
            raise ValueError("exp-Golomb code past the end")
        value += ((bits[pos // 8] >> (7 - pos % 8)) & 1) << (zeros - 1)
        zeros -= 1
        pos += 1
    return value, pos


def slice_types(data):
    """Which kinds of slice some Annex B bytes contain, as a set of the
    slice_type values of 7.4.3 reduced modulo 5: 0 P, 1 B, 2 I, 3 SP, 4 SI.

    This exists so that a container can refuse a file it cannot finish
    before it puts a poster frame on screen. slice_type is the second
    exp-Golomb field of the slice header, so reading it costs a couple of
    bytes per NAL and no arithmetic decoding at all -- which is the point:
    trial-decoding cannot tell you that frame 400 is a B frame, and a video
    that stops a quarter of the way through is worse than one that never
    started. Malformed headers are simply not reported; the Fortran is the
    thing that gets to have opinions about those.
    """
    found = set()
    pos = data.find(b"\x00\x00\x01")
    while pos >= 0:
        nxt = data.find(b"\x00\x00\x01", pos + 3)
        if pos + 3 < len(data) and (data[pos + 3] & 0x1F) in (1, 2, 5):
            end = len(data) if nxt < 0 else nxt
            head = _unescape(data[pos + 4:min(end, pos + 4 + 16)])
            try:
                _first_mb, at = _ue(head, 0)
                kind, _at = _ue(head, at)
            except (ValueError, IndexError):
                kind = None
            if kind is not None and kind < 10:
                found.add(kind % 5)
        pos = nxt
    return found


class Decoder:
    """One H.264 stream, decoded a frame at a time.

    Every instance shares the library's single set of COMMON blocks, so
    every call takes ``_LOCK``. That is the price of a decoder whose state
    is static storage, and it is paid here rather than in the caller: two
    ``<video>`` elements on one page must not be able to corrupt each
    other, however slowly they play.

    ``_since_idr`` is what makes sharing safe now that frames depend on the
    frames before them. It holds the access units decoded since the last
    IDR, and is replayed when another decoder has been at the library in
    between. An IDR clears it, so it is bounded by the stream's keyframe
    interval rather than by its length.
    """

    def __init__(self, extradata=b""):
        lib = _library()
        if lib is None:
            raise H264Error(unavailable_reason())
        self._lib = lib
        self._length_size = 0
        self._headers = b""
        self._since_idr = []
        self._poc = 0
        if extradata and extradata[:1] == b"\x01":
            self._headers, self._length_size = parameter_sets_from_avcc(
                extradata)
        elif extradata:
            # Some containers store the parameter sets as Annex B already,
            # which is legal in Matroska and happens in the wild in MP4 too.
            self._headers = extradata

    def reset(self):
        """Forget every decoded picture. The parameter sets come from the
        container rather than from the stream and are kept."""
        global _owner
        with _LOCK:
            self._since_idr = []
            self._poc = 0
            if _owner is self:
                self._lib.h264_reset()
                _owner = None

    def _framed(self, packet):
        if self._length_size:
            return self._headers + annexb_from_avcc(packet, self._length_size)
        if packet[:3] == b"\x00\x00\x01" or packet[:4] == _START:
            return self._headers + bytes(packet)
        raise H264Error("this packet is neither Annex B nor a known "
                        "MP4 sample")

    def _feed(self, data):
        """One access unit through the Fortran. _LOCK is already held."""
        lib = self._lib
        buf = (ctypes.c_char * len(data)).from_buffer_copy(data)
        size = ctypes.c_int(len(data))
        status = ctypes.c_int(0)
        lib.h264_decode(buf, ctypes.byref(size), ctypes.byref(status))
        if status.value != 0:
            raise H264Error(_explain(status.value))

    def _decode(self, packet):
        """Decode one access unit, leaving the picture in the library.

        Returns nothing; the caller reads the picture out in whichever
        colour space it wants while still holding the lock.
        """
        global _owner
        data = self._framed(packet)
        if 5 in _nal_types(data):
            self._since_idr = []
        if _owner is not self:
            self._lib.h264_reset()
            _owner = None
            for earlier in self._since_idr:
                self._feed(earlier)
            _owner = self
        try:
            self._feed(data)
        except H264Error:
            # The library now holds a half-decoded picture that no longer
            # matches this decoder's history. Disown it so that the next
            # call rebuilds the buffer instead of predicting from wreckage.
            _owner = None
            raise
        self._since_idr.append(data)
        poc = ctypes.c_int(0)
        self._lib.h264_poc(ctypes.byref(poc))
        self._poc = poc.value

    @property
    def poc(self):
        """The picture order count of the last picture decoded.

        A stream with B pictures hands them over out of order: this is the
        key you sort on to get presentation order back. Zero before any
        picture has been decoded, which is also an IDR's own count, so it
        only means anything once you have decoded something.
        """
        return self._poc

    def decode(self, packet):
        """One access unit in, ``(width, height, rgba)`` out.

        ``packet`` is Annex B bytes, or an MP4 sample when the ``avcC`` this
        decoder was built with said how long its length prefixes are.
        """
        lib = self._lib
        with _LOCK:
            self._decode(packet)
            width = ctypes.c_int(0)
            height = ctypes.c_int(0)
            status = ctypes.c_int(0)
            lib.h264_dims(ctypes.byref(width), ctypes.byref(height))
            if width.value < 1 or height.value < 1:
                raise H264Error("the decoder produced no picture")
            need = width.value * height.value * 4
            out = (ctypes.c_char * need)()
            cap = ctypes.c_int(need)
            lib.h264_rgba(out, ctypes.byref(cap), ctypes.byref(status))
            if status.value < 0:
                raise H264Error("the picture did not fit its buffer")
            return width.value, height.value, bytes(out)

    def decode_i420(self, packet):
        """The same picture in the decoder's own colour space.

        This is what the tests compare, because H.264 is bit-exact in YUV
        and says nothing whatever about anybody's RGB matrix: a mismatch
        against a reference decoder's RGB output would be a disagreement
        about colour, not about decoding.
        """
        lib = self._lib
        with _LOCK:
            self._decode(packet)
            width = ctypes.c_int(0)
            height = ctypes.c_int(0)
            status = ctypes.c_int(0)
            lib.h264_dims(ctypes.byref(width), ctypes.byref(height))
            need = width.value * height.value * 3 // 2
            if need < 6:
                raise H264Error("the decoder produced no picture")
            out = (ctypes.c_char * need)()
            cap = ctypes.c_int(need)
            lib.h264_i420(out, ctypes.byref(cap), ctypes.byref(status))
            if status.value < 0:
                raise H264Error("the picture did not fit its buffer")
            return width.value, height.value, bytes(out)


def probe(extradata=b""):
    """Would a stream with this ``avcC`` decode? Returns None when yes and
    a reason when no. Used by the container code to decide whether to say
    "H.264" or "H.264, and here is why not"."""
    if not available():
        return unavailable_reason()
    try:
        Decoder(extradata)
    except H264Error as exc:
        return str(exc)
    return None


__all__ = ["Decoder", "H264Error", "available", "unavailable_reason", "probe",
           "annexb_from_avcc", "parameter_sets_from_avcc", "slice_types"]

if __name__ == "__main__":                      # pragma: no cover
    # Decode a .264 or .mp4 named on the command line and say what came out.
    # Not a test; a way to ask this module a question without a browser.
    if available():
        for path in sys.argv[1:]:
            with open(path, "rb") as handle:
                blob = handle.read()
            w, h, planes = Decoder().decode_i420(blob)
            print("%s: %dx%d, %d bytes of I420" % (path, w, h, len(planes)))
    else:
        print("no decoder: %s" % unavailable_reason())
