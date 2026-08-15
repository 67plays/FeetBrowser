"""H.264 video decoding, in Fortran, loaded through ctypes.

The decoder itself is in ``fortran/`` -- fixed-form FORTRAN 77, about seven
thousand lines of it, compiled by gfortran into a shared library that this
module loads and calls. Nothing here decodes anything; this is the part
that finds the library, converts between the two shapes an H.264 stream
comes in, and gets out of the way when the machine has no Fortran on it.

Where the library comes from depends on who is running. From a checkout it
is compiled on demand and cached in the temporary directory, keyed by the
sources and the compiler. In a packaged application there is no compiler,
so the packaging compiles it on the build machine and ships it inside this
package under the name ``prebuilt_name()`` -- see ``build_library``, which
is the one entry point all three packagers use.
>>>>>>> d6003a3 (Ship the H.264 decoder in the packaged applications)

Why Fortran: the arithmetic decoder in clause 9.3 is a serial dependency
chain -- one table lookup, one subtract, one compare, per *bit* -- and a
bitstream is millions of bits. Python does that at a few hundred thousand
bins a second, which is about four orders of magnitude short of a video.
Fortran does it at 120 million. The rest of the codec followed the
entropy layer across the boundary because splitting a decoder in half at
the macroblock level would mean marshalling every coefficient.

What decodes today: I and P slices, either entropy coder -- CABAC or
CAVLC -- 4:2:0 8-bit, Baseline, Main and High profile. Every intra
prediction mode, both transform sizes, scaling matrices, quarter-sample
motion compensation with weighted prediction, up to four reference
frames, the deblocking filter. Not B slices, not interlace -- see the
module docstring in ``fortran/h264api.f`` for the status codes each of
those produces.

The library holds one decoder's worth of state in COMMON blocks, which
is to say a single global one. ``_LOCK`` is what stops two ``<video>``
elements from interleaving their macroblocks; it is not an optimisation
to remove later, it is load-bearing.

P slices made that state persistent, which makes it sharper still: a
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
_ABI = 3

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
    """A hash over the ABI and every source, so a changed decoder rebuilds.

    Nothing about the machine goes in, deliberately: the same sources hash
    the same everywhere, which is what lets the packaging build a library
    on one machine, name it after this digest, and have _open_library find
    it on another. Which gfortran built a given file is a separate
    question, answered by _compiler_id for the cache and by the h264_version
    check for anything that gets loaded.
    """
    sha = hashlib.sha256()
    sha.update(("abi%d" % _ABI).encode("ascii"))
    for name in _INCLUDES + _SOURCES:
        with open(os.path.join(_FORTRAN, name), "rb") as handle:
            sha.update(handle.read())
    return sha.hexdigest()[:16]


def _compiler_id(fc):
    """Which gfortran this is, in eight hex digits.

    The same sources built by two gfortrans are two different libraries --
    different runtime, different instruction set, different bugs -- and a
    cache in a shared temporary directory that cannot tell them apart hands
    the wrong one to whichever process runs second.
    """
    sha = hashlib.sha256()
    for flag in ("--version", "-dumpmachine"):
        try:
            done = subprocess.run([fc, flag], capture_output=True)
        except OSError:
            return "unknown0"
        sha.update(done.stdout)
    return sha.hexdigest()[:8]


# What a library has to be built with to be *shipped* rather than used where
# it was built: gfortran's own runtime linked in, instead of left behind as
# three dependencies on a compiler installation the user does not have.
# -static-libquadmath is GCC 10 and later and older gfortrans reject the flag
# outright, hence a second attempt without it -- and hence the linkage checks
# in the packaging scripts, which is where a dangling libquadmath would
# otherwise ship. -march=native is left out for the same reason the rest is
# in: the machine that runs this is not the machine that built it.
_PORTABLE = ("-static-libgfortran", "-static-libgcc")
_SHIP_ATTEMPTS = (list(_PORTABLE) + ["-static-libquadmath"], list(_PORTABLE))

# From a checkout: tuned for this machine, because it will only ever run on
# this machine, and dropped if the compiler will not have it (gfortran on
# Apple silicon rejects -march=native outright).
_LOCAL_ATTEMPTS = (["-march=native"], [])


def _compile(fc, out, attempts=_LOCAL_ATTEMPTS):
    """Build the shared library at `out`, or raise.

    `attempts` is a list of extra-flag lists, tried in order until one
    compiles: the compiler is the only authority on what it accepts.
    """
    tmp = out + ".%d.tmp" % os.getpid()
    base = ["-O3", "-shared", "-fPIC", "-std=legacy", "-fno-align-commons",
            "-I", _FORTRAN, "-o", tmp]
    if platform.system() == "Darwin":
        # Otherwise the library records its own bare filename as its install
        # name, which resolves against nothing once it is inside a bundle --
        # and packaging/macos/verify.sh rejects it, correctly.
        base += ["-Wl,-install_name,@loader_path/" + os.path.basename(out)]
    sources = [os.path.join(_FORTRAN, name) for name in _SOURCES]
    last = None
    for extra in attempts:
        try:
            subprocess.run([fc] + base + list(extra) + sources, check=True,
                           stdout=subprocess.DEVNULL,
                           stderr=subprocess.PIPE)
        except subprocess.CalledProcessError as exc:
            last = exc
            continue
        except OSError as exc:
            raise H264Error("could not run %s: %s" % (fc, exc))
        os.replace(tmp, out)
        return
    detail = (last.stderr or b"").decode("utf8", "replace").strip() if last else ""
    raise H264Error("gfortran could not build the decoder: %s"
                    % (detail.splitlines()[-1] if detail else "no output"))


def prebuilt_name():
    """What a library built by the packaging has to be called.

    The digest is the whole guarantee. It comes from the sources in
    ``fortran/``, which ship beside the package in every bundle exactly as
    they sit beside it in a checkout, so a file under this name was built
    from precisely the decoder this Python code was written against. Change
    a line of Fortran or the ABI and the name changes with it: a stale
    prebuilt is not preferred over the sources, it is simply not found.
    """
    return "_h264_%s%s" % (_digest(), _library_suffix())


def prebuilt_path():
    """Where a bundled prebuilt library lives -- inside the package, next to
    this file, so that whatever copied the package copied the decoder too."""
    return os.path.join(_HERE, prebuilt_name())


def build_library(out, fc=None):
    """Compile the decoder for shipping, into `out`. Returns `out`.

    This is what packaging/{macos,linux,windows} call, and the only caller
    there is. Keeping the flags here rather than in three scripts is what
    stops what a library is built with from drifting away from what the
    loader expects of it.
    """
    if not os.path.isdir(_FORTRAN):
        raise H264Error("the fortran/ directory is missing from this checkout")
    if fc is None:
        fc = _find_gfortran()
    if fc is None:
        raise H264Error("no gfortran on PATH")
    directory = os.path.dirname(os.path.abspath(out))
    if not os.path.isdir(directory):
        os.makedirs(directory)
    _compile(fc, out, _SHIP_ATTEMPTS)
    return out


def _load(path):
    """Open a built library and check it is the one we think it is."""
    lib = ctypes.CDLL(path)
    for name in ("h264_version", "h264_reset", "h264_dims", "h264_decode",
                 "h264_i420", "h264_rgba"):
        getattr(lib, name).restype = None
    version = ctypes.c_int(0)
    lib.h264_version(ctypes.byref(version))
    if version.value != _ABI:
        raise H264Error("the decoder at %s reports ABI %d, expected %d"
                        % (path, version.value, _ABI))
    return lib


def _open_library():
    if not os.path.isdir(_FORTRAN):
        raise H264Error("the fortran/ directory is missing from this checkout")
    # A library the packaging built and shipped beside this file. Preferred
    # over compiling, because in a bundle there is no compiler to fall back
    # on -- and a shipped library that will not load is worth saying out
    # loud, so its failure is only swallowed if there is a gfortran to try.
    shipped = prebuilt_path()
    failure = None
    if os.path.exists(shipped):
        try:
            return _load(shipped)
        except (H264Error, OSError) as exc:
            failure = H264Error("the bundled decoder %s did not load: %s"
                                % (prebuilt_name(), exc))
    fc = _find_gfortran()
    if fc is None:
        raise failure or H264Error("no gfortran on PATH")
    out = os.path.join(tempfile.gettempdir(),
                       "feetbrowser_h264_%s_%s%s"
                       % (_digest(), _compiler_id(fc), _library_suffix()))
    if not os.path.exists(out):
        _compile(fc, out)
    return _load(out)


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


def library_path():
    """Which file the decoder was loaded from, or None if there is none.

    A bundled library and one a compiler made thirty seconds ago behave
    identically, which is exactly why a check that video works has to be
    able to say which of the two answered.
    """
    lib = _library()
    return getattr(lib, "_name", None) if lib is not None else None


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
    -43: "a B, SP or SI slice -- this decoder does I and P slices",
    -44: "the slice header ran off the end of its own NAL unit",
    -45: "the slice header gives an impossible quantiser",
    -46: "the slice starts past the end of the picture",
    -47: "an unknown deblocking filter mode",
    -48: "an unknown cabac_init_idc",
    -49: "the slice gives an impossible reference list length",
    -50: "long-term references, which this decoder does not implement",
    -51: "a P slice with no reference picture to predict from",
    -52: "the slice reorders in a picture that is not in the buffer",
    -53: "the decoded picture buffer has no free slot",
    -54: "a partition points at a reference index with no picture behind it",
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
           "annexb_from_avcc", "parameter_sets_from_avcc", "slice_types",
           "build_library", "prebuilt_name", "prebuilt_path", "library_path"]


_USAGE = """usage: python3 -m feetbrowser.h264 [--name | --build PATH [--fc GFORTRAN] | FILE...]

  --name          print the filename a prebuilt library must have
  --build PATH    compile the decoder for shipping, into PATH
  --fc GFORTRAN   which compiler --build should use
  FILE...         decode a .264 stream and say what came out
"""


def _cli(argv):                                 # pragma: no cover
    """The packaging's entry point, and a way to ask this module a question
    without a browser in the way."""
    if argv and argv[0] == "--help":
        sys.stdout.write(_USAGE)
        return 0
    if argv and argv[0] == "--name":
        print(prebuilt_name())
        return 0
    if argv and argv[0] == "--build":
        rest = argv[1:]
        if not rest:
            sys.stderr.write(_USAGE)
            return 2
        out, fc = rest[0], None
        if rest[1:2] == ["--fc"]:
            fc = rest[2] if len(rest) > 2 else None
        try:
            build_library(out, fc)
        except H264Error as exc:
            sys.stderr.write("%s\n" % exc)
            return 1
        print(out)
        return 0
    if not available():
        sys.stderr.write("no decoder: %s\n" % unavailable_reason())
        return 1
    for path in argv:
        with open(path, "rb") as handle:
            blob = handle.read()
        w, h, planes = Decoder().decode_i420(blob)
        print("%s: %dx%d, %d bytes of I420" % (path, w, h, len(planes)))
    return 0


if __name__ == "__main__":                      # pragma: no cover
    sys.exit(_cli(sys.argv[1:]))
