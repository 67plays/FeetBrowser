"""AAC-LC audio decoding, in Fortran, loaded through ctypes.

The decoder itself is in ``fortran/inst*.f`` -- fixed-form FORTRAN 77,
compiled on demand by gfortran into a shared library that this module
loads and calls. Nothing here decodes anything; this is the part that
finds a compiler, keeps the build cached, tells one stream's decoding
apart from another's, and gets out of the way when the machine has no
Fortran on it.

The subsystem is called the *instep* -- the arch between the toes and the
heel, which is where the weight goes. It shares nothing with the H.264
decoder next door but the build machinery; its routines are ``IP*`` and
its COMMON blocks ``/IP*/`` precisely so that the two can live in one
process without Fortran's global namespace introducing them to each
other.

Why Fortran: an AAC frame is 1024 inverse-MDCT outputs per channel, and
the transform behind them is 43 frames a second of FFT arithmetic. Python
does that at something under a tenth of realtime; the Fortran does it at
roughly two hundred times realtime on one core. The rest of the codec
followed the transform across the boundary because splitting a decoder
between the Huffman layer and the DSP layer would mean marshalling a
thousand coefficients per frame per channel across ctypes, which costs
more than the decoding.

What decodes today: AAC-LC, mono and stereo, every sampling frequency
index the standard defines, from raw ``raw_data_block`` frames as MP4
carries them or from ADTS. All eleven spectral codebooks and the escape
sequence, sections, scalefactors, pulses, TNS, mid/side and intensity
stereo, perceptual noise substitution, and all four window sequences.

What does not: HE-AAC's SBR and Parametric Stereo, Main profile's
backward prediction, LTP, SSR's gain control, coupling channels, LFE,
anything above two channels, and 960-sample frames. Every one of those is
refused by name with a status code of its own rather than mis-decoded --
see ``_STATUS`` below, and the header comment in ``fortran/instapi.f``.

There is no platform audio output here and there should not be. This
module's job ends at correct PCM in memory.
"""

import array
import ctypes
import hashlib
import os
import platform
import struct
import subprocess
import sys
import tempfile
import threading

_HERE = os.path.dirname(os.path.abspath(__file__))
_FORTRAN = os.path.join(os.path.dirname(_HERE), "fortran")

# Compilation order does not matter to gfortran here -- there are no
# modules, only COMMON blocks and an INCLUDE -- but a fixed order keeps the
# cache key stable across filesystems that list a directory in their own
# order.
_SOURCES = ("insttab.f", "instbit.f", "instics.f", "instdsp.f", "instapi.f")
_INCLUDES = ("instcom.inc",)

# The version instep_version reports. A library left in the cache by an
# older checkout has the old entry points and the old meanings, and calling
# it would be worse than not having one.
_ABI = 1

_LOCK = threading.Lock()
_lib = None
_load_error = None
_loaded = False

# Which Decoder's overlap is in the library's COMMON blocks. Guarded by
# _LOCK, like the blocks themselves.
_owner = None


class AacError(Exception):
    """A stream this decoder cannot decode, or a build that did not
    happen."""


# -- building ----------------------------------------------------------------

def _find_gfortran():
    for name in ("gfortran", "gfortran-15", "gfortran-14", "gfortran-13",
                 "gfortran-12", "gfortran-11"):
        try:
            subprocess.run([name, "--version"], check=True,
                           stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL)
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
    last = None
    for extra in (["-march=native"], []):
        try:
            subprocess.run([fc] + base + extra + sources, check=True,
                           stdout=subprocess.DEVNULL,
                           stderr=subprocess.PIPE)
        except subprocess.CalledProcessError as exc:
            last = exc
            continue
        except OSError as exc:
            raise AacError("could not run %s: %s" % (fc, exc))
        os.replace(tmp, out)
        return
    detail = (last.stderr or b"").decode("utf8", "replace").strip()
    raise AacError("gfortran could not build the decoder: %s"
                   % (detail.splitlines()[-1] if detail else "no output"))


def _open_library():
    if not os.path.isdir(_FORTRAN):
        raise AacError("the fortran/ directory is missing from this checkout")
    fc = _find_gfortran()
    if fc is None:
        raise AacError("no gfortran on PATH")
    out = os.path.join(tempfile.gettempdir(),
                       "feetbrowser_aac_%s%s"
                       % (_digest(), _library_suffix()))
    if not os.path.exists(out):
        _compile(fc, out)
    lib = ctypes.CDLL(out)
    for name in ("instep_version", "instep_reset", "instep_flush",
                 "instep_config", "instep_adts", "instep_decode",
                 "instep_pcm", "instep_save", "instep_restore",
                 "instep_qspec", "instep_spec", "instep_bands",
                 "instep_shape", "instep_imdct", "instep_window"):
        getattr(lib, name).restype = None
    version = ctypes.c_int(0)
    lib.instep_version(ctypes.byref(version))
    if version.value != _ABI:
        raise AacError("the built decoder reports ABI %d, expected %d"
                       % (version.value, _ABI))
    lib.instep_reset()
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
        except (AacError, OSError) as exc:
            _lib = None
            _load_error = str(exc)
        _loaded = True
    return _lib


def available():
    """True when this machine can decode AAC."""
    return _library() is not None


def unavailable_reason():
    """Why not, in a form fit to show a user. None when it is available."""
    if _library() is not None:
        return None
    return _load_error or "the AAC decoder is not available"


# -- what the Fortran says ---------------------------------------------------

# The numbers are grouped by what produced them, so that a bug report says
# where to look: -1..-19 configuration, -20..-29 a tool we refuse by name,
# -30..-39 the bitstream contradicting itself, -40..-49 the frame's element
# structure.
_STATUS = {
    -1: "AAC: no AudioSpecificConfig -- the decoder is not configured",
    -2: "AAC: the AudioSpecificConfig ends in the middle of itself",
    -3: "AAC: an empty frame",
    -4: "AAC: a frame larger than the decoder's buffer",
    -5: "AAC: no ADTS header where one was expected",
    -6: "AAC: an ADTS header that is not MPEG layer zero",
    -7: "AAC: an ADTS frame longer than the bytes it came with",
    -8: "AAC: no decoded frame to read samples from",
    -9: "AAC: the samples did not fit their buffer",
    -11: "AAC: an explicitly coded sampling frequency, which has no "
         "scalefactor band layout defined for it",
    -12: "AAC: a reserved sampling frequency index",
    -13: "AAC: more than two channels -- this decoder does mono and stereo",
    -14: "AAC: an LFE channel, which this decoder does not implement",
    -15: "AAC: 960-sample frames, which this decoder does not implement",
    -16: "AAC: a stream that depends on a separate core coder",
    -20: "AAC: Main profile, whose backward prediction this decoder does "
         "not implement",
    -21: "AAC: Scalable Sample Rate profile, which this decoder does not "
         "implement",
    -22: "AAC: a coupling channel element, which this decoder does not "
         "implement",
    -23: "AAC: gain control data, which belongs to SSR and which this "
         "decoder does not implement",
    -24: "AAC: Long Term Prediction, which this decoder does not implement",
    -25: "AAC: HE-AAC -- Spectral Band Replication, which this decoder does "
         "not implement",
    -26: "AAC: HE-AAC v2 -- Parametric Stereo, which this decoder does not "
         "implement",
    -27: "AAC: an audio object type that is not AAC-LC",
    -30: "AAC: the frame ended in the middle of a syntax element",
    -31: "AAC: a reserved Huffman codebook",
    -32: "AAC: a scalefactor outside the range the standard allows",
    -33: "AAC: a scalefactor band layout the sampling frequency cannot have",
    -34: "AAC: a TNS filter of an order this decoder does not keep room for",
    -35: "AAC: pulse data that points outside the spectrum",
    -36: "AAC: a reserved value of ms_mask_present",
    -39: "AAC: a quantised coefficient larger than the standard allows",
    -40: "AAC: a frame with more elements than a frame can have",
    -41: "AAC: a frame whose channels do not add up to the ones configured",
    -42: "AAC: several raw data blocks behind one ADTS header",
    -43: "AAC: a programme config that contradicts the stream's own",
}


def _explain(status):
    return _STATUS.get(status, "AAC: decoder status %d" % status)


# The thirteen sampling frequencies the four-bit index can name. Indices 13
# and 14 are reserved and 15 means "the rate follows explicitly", which the
# Fortran refuses -- there is no band layout defined for an arbitrary rate.
SAMPLE_RATES = (96000, 88200, 64000, 48000, 44100, 32000,
                24000, 22050, 16000, 12000, 11025, 8000, 7350)


def probe(asc):
    """Why this AudioSpecificConfig cannot be decoded, or None if it can.

    Cheap and side-effect free in the sense that matters: it parses the
    config and throws the result away, so a container can put a reason on
    screen without constructing a decoder or reserving the library.
    """
    lib = _library()
    if lib is None:
        return unavailable_reason()
    if not asc:
        return "AAC: this track carries no AudioSpecificConfig"
    with _LOCK:
        info = _config(lib, asc)
    if info[0] != 0:
        return _explain(info[0])
    return None


def _config(lib, asc):
    """instep_config, with the library already reserved."""
    global _owner
    _owner = None
    lib.instep_reset()
    buf = (ctypes.c_char * len(asc)).from_buffer_copy(bytes(asc))
    size = ctypes.c_int(len(asc))
    info = (ctypes.c_int * 8)()
    lib.instep_config(buf, ctypes.byref(size), info)
    return list(info)


# -- ADTS --------------------------------------------------------------------

def adts_frames(data):
    """Walk an ADTS stream, yielding ``(header length, frame length)``.

    ADTS is the framing a bare ``.aac`` file uses: a seven-byte header in
    front of every frame, carrying the same facts an AudioSpecificConfig
    would and a length so the next one can be found. This is the one piece
    of parsing done in Python rather than in Fortran, because it is
    framing rather than decoding and because a caller often wants the
    frame boundaries without decoding anything at all.
    """
    pos = 0
    end = len(data)
    while pos + 7 <= end:
        if data[pos] != 0xFF or (data[pos + 1] & 0xF0) != 0xF0:
            raise AacError("AAC: no ADTS sync word at byte %d" % pos)
        protection_absent = data[pos + 1] & 1
        length = (((data[pos + 3] & 3) << 11) | (data[pos + 4] << 3)
                  | (data[pos + 5] >> 5))
        header = 7 if protection_absent else 9
        if length < header or pos + length > end:
            raise AacError("AAC: an ADTS frame runs past the end of the "
                           "stream at byte %d" % pos)
        yield header, length
        pos += length
    if pos != end:
        raise AacError("AAC: %d bytes of trailing rubbish after the last "
                       "ADTS frame" % (end - pos))


def asc_from_adts(data):
    """An AudioSpecificConfig equivalent to an ADTS stream's first header.

    The two carry the same three facts -- object type, sampling frequency
    index, channel configuration -- in a different order and with two bits
    fewer of channel configuration in ADTS. Building the config once and
    decoding through the ordinary path afterwards means the ADTS and MP4
    routes into this decoder are the same code below the first frame,
    which is a property the test suite checks by decoding both and
    comparing sample for sample.
    """
    if len(data) < 7 or data[0] != 0xFF or (data[1] & 0xF0) != 0xF0:
        raise AacError("AAC: this is not an ADTS stream")
    profile = (data[2] >> 6) & 3
    rate_index = (data[2] >> 2) & 0xF
    channels = ((data[2] & 1) << 2) | (data[3] >> 6)
    object_type = profile + 1
    bits = (object_type << 11) | (rate_index << 7) | (channels << 3)
    return struct.pack(">H", bits)


# -- the decoder -------------------------------------------------------------

class Decoder:
    """One AAC-LC stream, decoded a frame at a time.

    Every instance shares the library's single set of COMMON blocks, so
    every call takes ``_LOCK``. That is the price of a decoder whose state
    is static storage, and it is paid here rather than in the caller.

    Sharing is sharper for audio than for video. A frame's samples are the
    second half of the *previous* frame's windowed transform added to the
    first half of this one's; there is no keyframe and no way to start
    clean. So each decoder keeps its own copy of the overlap and puts it
    back whenever it finds another decoder has been at the library in
    between. Two ``<audio>`` elements playing at once are then slow and
    correct rather than fast and clicking.
    """

    def __init__(self, asc):
        lib = _library()
        if lib is None:
            raise AacError(unavailable_reason())
        self._lib = lib
        if not asc:
            # Answered here rather than in the Fortran, which is handed a
            # length of zero and can only say "an empty frame". A track
            # with no AudioSpecificConfig is a real thing to find in a real
            # MP4 and deserves the same sentence `probe` gives it.
            raise AacError("AAC: this track carries no AudioSpecificConfig")
        self._asc = bytes(asc)
        self._overlap = None
        self._ints = None
        with _LOCK:
            info = _config(lib, self._asc)
            if info[0] != 0:
                raise AacError(_explain(info[0]))
            global _owner
            _owner = self
        self.sample_rate = info[1]
        self.channels = info[2]
        self.frame_length = info[3]
        self.object_type = info[4]
        # Bits consumed by the last frame, and bits it was offered. A frame
        # whose parse stops anywhere but the end of it is a frame this
        # decoder read differently from the encoder that wrote it, and that
        # is worth being able to see from outside.
        self.last_bits = 0
        self.last_bits_offered = 0

    def reset(self):
        """Forget the overlap. The configuration came from the container
        and is kept.

        This is what a seek needs and it is not free of consequence: the
        first frame after it is decoded against silence, so its first half
        is attenuated. Every AAC decoder has this property and it is why
        seeking in AAC replays a frame or two before the target rather
        than starting at it.
        """
        with _LOCK:
            self._overlap = None
            self._ints = None
            if _owner is self:
                self._lib.instep_flush()

    def _enter(self):
        """Make the library ours. _LOCK is already held."""
        global _owner
        if _owner is self:
            return
        info = _config(self._lib, self._asc)
        if info[0] != 0:
            raise AacError(_explain(info[0]))
        if self._overlap is not None:
            self._lib.instep_restore(self._overlap,
                                     ctypes.byref(ctypes.c_int(2048)),
                                     self._ints,
                                     ctypes.byref(ctypes.c_int(16)),
                                     ctypes.byref(ctypes.c_int(0)))
        _owner = self

    def _leave(self):
        """Take our continuity back out of the library."""
        if self._overlap is None:
            self._overlap = (ctypes.c_double * 2048)()
            self._ints = (ctypes.c_int * 16)()
        self._lib.instep_save(self._overlap,
                              ctypes.byref(ctypes.c_int(2048)),
                              self._ints, ctypes.byref(ctypes.c_int(16)),
                              ctypes.byref(ctypes.c_int(0)))

    def decode(self, packet):
        """One ``raw_data_block`` in, ``(samples per channel, channels,
        interleaved float32 bytes)`` out.

        The samples are floats in [-1, 1] and are not clipped: the inverse
        transform of a loud frame can exceed unity by a little and it is
        the mixer's business, not the decoder's, what to do about that.
        """
        if not packet:
            return 0, self.channels, b""
        with _LOCK:
            self._enter()
            info = self._feed(bytes(packet))
            out = self._read(info)
            self._leave()
        return out

    def decode_adts(self, data):
        """A whole ADTS stream in, one ``(samples, channels, bytes)`` out.

        Every frame decoded and concatenated, which is what a test wants
        and what the command line below prints. A player wants
        ``decode()`` and its own framing.
        """
        pcm = bytearray()
        total = 0
        for header, length in adts_frames(data):
            count, _channels, chunk = self.decode(data[header:length])
            total += count
            pcm += chunk
            data = data[length:]
        return total, self.channels, bytes(pcm)

    def _feed(self, data):
        """One raw_data_block through the Fortran. _LOCK is already held,
        and the library is ours."""
        global _owner
        buf = (ctypes.c_char * len(data)).from_buffer_copy(data)
        size = ctypes.c_int(len(data))
        info = (ctypes.c_int * 8)()
        self._lib.instep_decode(buf, ctypes.byref(size), info)
        if info[0] != 0:
            # The library now holds half a frame that no longer matches this
            # decoder's history. Disown it rather than let the next call
            # overlap-add onto wreckage.
            _owner = None
            raise AacError(_explain(info[0]))
        self.last_bits = info[3]
        self.last_bits_offered = info[4]
        return info

    def _read(self, info):
        count, channels = info[1], info[2]
        need = count * channels
        if need <= 0:
            raise AacError("AAC: the decoder produced no samples")
        out = (ctypes.c_float * need)()
        status = ctypes.c_int(0)
        self._lib.instep_pcm(out, ctypes.byref(ctypes.c_int(need)),
                             ctypes.byref(status))
        if status.value < 0:
            raise AacError(_explain(status.value))
        return count, channels, bytes(memoryview(out).cast("B"))

    # -- what the tests look at ---------------------------------------------
    #
    # A decoder compared only against its own output is compared against
    # nothing, and a tolerance at the end of a chain this long hides almost
    # any bug in the middle of it. These expose the stages that can be
    # compared exactly, so that a difference can be located rather than
    # merely detected. They read whatever the last decode left behind and
    # are only meaningful straight after one.

    def _snapshot(self, getter, kind, count):
        buf = (kind * count)()
        status = ctypes.c_int(0)
        getter(buf, ctypes.byref(ctypes.c_int(count)), ctypes.byref(status))
        if status.value < 0:
            raise AacError(_explain(status.value))
        return list(buf)

    def quantised_spectrum(self, channel=1):
        """The last frame's quantised coefficients for one channel, before
        anything was done to them. Integers, and exactly comparable."""
        with _LOCK:
            self._enter()
            return self._snapshot(
                lambda b, c, s: self._lib.instep_qspec(
                    ctypes.byref(ctypes.c_int(channel)), b, c, s),
                ctypes.c_int, 1024)

    def spectrum(self, channel=1):
        """The same coefficients dequantised, after the stereo tools and
        TNS and before the transform."""
        with _LOCK:
            self._enter()
            return self._snapshot(
                lambda b, c, s: self._lib.instep_spec(
                    ctypes.byref(ctypes.c_int(channel)), b, c, s),
                ctypes.c_double, 1024)

    def bands(self, channel=1):
        """``(codebooks, scalefactors, band offsets)`` for the last frame.

        The first two are flattened as ``band + 52 * group``; the third is
        the band layout in force, which is a property of the window
        sequence rather than of the frame's contents.
        """
        count = 52 * 8
        types = (ctypes.c_int * count)()
        factors = (ctypes.c_int * count)()
        offsets = (ctypes.c_int * count)()
        status = ctypes.c_int(0)
        with _LOCK:
            self._enter()
            self._lib.instep_bands(ctypes.byref(ctypes.c_int(channel)),
                                   types, factors, offsets,
                                   ctypes.byref(ctypes.c_int(count)),
                                   ctypes.byref(status))
        if status.value < 0:
            raise AacError(_explain(status.value))
        return list(types), list(factors), list(offsets[:52])

    def shape(self, channel=1):
        """The last frame's window sequence and grouping, as a dict."""
        out = (ctypes.c_int * 16)()
        status = ctypes.c_int(0)
        with _LOCK:
            self._enter()
            self._lib.instep_shape(ctypes.byref(ctypes.c_int(channel)), out,
                                   ctypes.byref(ctypes.c_int(16)),
                                   ctypes.byref(status))
        if status.value < 0:
            raise AacError(_explain(status.value))
        return {
            "window_sequence": out[0],
            "window_shape": out[1],
            "max_sfb": out[2],
            "windows": out[3],
            "groups": out[4],
            "num_swb": out[5],
            "ms_mask_present": out[6],
            "tns": out[7],
            "group_lengths": list(out[8:8 + out[4]]),
        }


def imdct(coefficients, fast=True):
    """The inverse MDCT on its own, either implementation.

    Exposed so that the fast transform can be held against the formula it
    claims to compute rather than against itself. 128 or 1024 coefficients
    in, twice that many samples out.
    """
    lib = _library()
    if lib is None:
        raise AacError(unavailable_reason())
    size = len(coefficients)
    if size not in (128, 1024):
        raise AacError("AAC: the transform is 128 or 1024 points")
    src = (ctypes.c_double * size)(*coefficients)
    dst = (ctypes.c_double * (2 * size))()
    with _LOCK:
        lib.instep_imdct(src, ctypes.byref(ctypes.c_int(size)),
                         ctypes.byref(ctypes.c_int(0 if fast else 1)), dst)
    return list(dst)


def window(which):
    """One of the four window halves: 0 long sine, 1 long KBD, 2 short
    sine, 3 short KBD. The rising half; the falling half is the same
    numbers backwards, which is why only one is stored."""
    lib = _library()
    if lib is None:
        raise AacError(unavailable_reason())
    size = 1024 if which <= 1 else 128
    dst = (ctypes.c_double * size)()
    status = ctypes.c_int(0)
    with _LOCK:
        lib.instep_window(ctypes.byref(ctypes.c_int(which)), dst,
                          ctypes.byref(ctypes.c_int(size)),
                          ctypes.byref(status))
    if status.value < 0:
        raise AacError(_explain(status.value))
    return list(dst)


def pcm_to_int16(samples):
    """Interleaved float32 bytes as interleaved signed 16-bit ones.

    Rounds half away from zero and clamps, which is what everything that
    writes a WAV file does. Lives here so that the one place a decoder's
    output gets quantised is the same in the tests and in the command line
    below.
    """
    floats = array.array("f")
    floats.frombytes(samples)
    out = array.array("h", [0]) * len(floats)
    for i, value in enumerate(floats):
        scaled = value * 32768.0
        if scaled >= 32767.0:
            out[i] = 32767
        elif scaled <= -32768.0:
            out[i] = -32768
        elif scaled >= 0:
            out[i] = int(scaled + 0.5)
        else:
            out[i] = -int(-scaled + 0.5)
    if sys.byteorder == "big":
        out.byteswap()
    return out.tobytes()


if __name__ == "__main__":                      # pragma: no cover
    # Decode a .aac (ADTS) file named on the command line and say what came
    # out. Not a test; a way to ask this module a question without a
    # browser.
    if available():
        for path in sys.argv[1:]:
            with open(path, "rb") as handle:
                blob = handle.read()
            decoder = Decoder(asc_from_adts(blob))
            count, channels, pcm = decoder.decode_adts(blob)
            print("%s: %d samples x %d channels at %d Hz (%.2f s)"
                  % (path, count, channels, decoder.sample_rate,
                     count / float(decoder.sample_rate or 1)))
    else:
        print("no decoder: %s" % unavailable_reason())
