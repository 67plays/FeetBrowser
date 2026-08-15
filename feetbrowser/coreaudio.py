"""Real sound out of a Mac, with no bindings package and no sound library.

CoreAudio is a C API, so ctypes is all it takes to drive it: open
AudioToolbox, declare the signatures, call the functions. No PyObjC, no
sounddevice, no PortAudio, no compiled shim -- the same rule ``cocoa.py``
follows, and for the same reason.

What this adds to ``heel.Ring`` is the one thing a ring cannot have on its
own: something that empties it in real time. An output AudioUnit is asked for
the default device, told what our samples look like, given a render callback,
and started. From then on CoreAudio calls that callback on a thread of its
own, every few milliseconds, forever, and the only thing the callback does is
move bytes from the ring into the buffer it was handed.

**That callback is the realtime thread**, and every rule in the ``heel``
module docstring is a rule about it. It is worth restating the two that this
file is where they are actually kept:

  * it never blocks, never waits and never allocates a container -- the whole
    body is integer arithmetic and two ``memmove`` calls, and both buffers
    existed before the stream started;
  * it never lets an exception out. A Python exception escaping a ctypes
    callback prints a traceback *from the audio thread*, which turns one late
    buffer into a second of them. So the body is wrapped, the failure is
    stashed in an attribute, and the buffer is filled with silence.

The one thing that cannot be avoided here is that calling a Python function
at all means taking the GIL. CoreAudio's deadline is a couple of
milliseconds; CPython's switch interval is five. So the callback is kept as
short as it can possibly be made -- no mixing, no gain, no conversion, no
resampling, none of which happen on this thread -- and the ring is four
thousand frames deep so that the mixer thread can be late by eighty
milliseconds without the speaker noticing. A design where the callback does
real work in Python is a design that clicks whenever the browser lays out a
page, and it is not recoverable by making the callback faster.

Everything here that is arithmetic or a lookup lives in a module-level
function, so the part that can only be exercised against real hardware is as
small as it can be made -- see ``tests/test_audio.py`` for both halves.
"""
import ctypes
import ctypes.util
import sys
import threading

from .heel import FLOAT32, SAMPLE_BYTES, AudioUnavailable

_FRAMEWORKS = {
    "audiotoolbox":
        "/System/Library/Frameworks/AudioToolbox.framework/AudioToolbox",
    "coreaudio":
        "/System/Library/Frameworks/CoreAudio.framework/CoreAudio",
}


def fourcc(text):
    """A CoreAudio four-character constant as the integer it really is.

    Half the constants in this API are ASCII spelled sideways -- 'lpcm' is
    0x6C70636D -- and writing them out as hex is how a typo becomes an
    unexplained -10879 six months later.
    """
    return int.from_bytes(text.encode("ascii"), "big")


def fourcc_name(value):
    """The other direction, for error messages. Falls back to the number."""
    try:
        raw = int(value & 0xFFFFFFFF).to_bytes(4, "big")
    except (ValueError, OverflowError):
        return str(value)
    if all(0x20 <= b < 0x7F for b in raw):
        return "'%s'" % raw.decode("ascii")
    return str(value)


# Component identity. An output unit of subtype DefaultOutput follows
# whatever the user has chosen in Sound preferences, including following it
# when they change it mid-playback, which is what a browser wants and what
# naming a device by ID specifically does not give you.
_TYPE_OUTPUT = fourcc("auou")
_SUBTYPE_DEFAULT_OUTPUT = fourcc("def ")
_MANUFACTURER_APPLE = fourcc("appl")

# AudioUnit properties and scopes.
_PROP_STREAM_FORMAT = 8
_PROP_LATENCY = 12
_PROP_MAX_FRAMES_PER_SLICE = 14
_PROP_SET_RENDER_CALLBACK = 23
_SCOPE_GLOBAL, _SCOPE_INPUT, _SCOPE_OUTPUT = 0, 1, 2

# Stream format.
_FORMAT_LINEAR_PCM = fourcc("lpcm")
_FLAG_IS_FLOAT = 1 << 0
_FLAG_IS_BIG_ENDIAN = 1 << 1
_FLAG_IS_SIGNED_INTEGER = 1 << 2
_FLAG_IS_PACKED = 1 << 3
_FLAG_IS_NON_INTERLEAVED = 1 << 5

# The HAL, for the two numbers that say how far behind the speaker is.
_SYSTEM_OBJECT = 1
_HW_DEFAULT_OUTPUT_DEVICE = fourcc("dOut")
_DEV_LATENCY = fourcc("ltnc")
_DEV_SAFETY_OFFSET = fourcc("saft")
_DEV_BUFFER_FRAME_SIZE = fourcc("fsiz")
_SCOPE_OUTPUT_HW = fourcc("outp")
_SCOPE_GLOBAL_HW = fourcc("glob")

_libs = {}
_problem = ""


# -- types -----------------------------------------------------------------
#
# Spelled out rather than guessed at. Every one of these is a wire format
# handed to a C function that will read exactly as many bytes as its own
# header says, so a missing field is not a mismatch, it is whatever happened
# to be on the stack.

class AudioComponentDescription(ctypes.Structure):
    _fields_ = [("componentType", ctypes.c_uint32),
                ("componentSubType", ctypes.c_uint32),
                ("componentManufacturer", ctypes.c_uint32),
                ("componentFlags", ctypes.c_uint32),
                ("componentFlagsMask", ctypes.c_uint32)]


class AudioStreamBasicDescription(ctypes.Structure):
    """The forty bytes that describe a stream. All of them, in order."""

    _fields_ = [("mSampleRate", ctypes.c_double),
                ("mFormatID", ctypes.c_uint32),
                ("mFormatFlags", ctypes.c_uint32),
                ("mBytesPerPacket", ctypes.c_uint32),
                ("mFramesPerPacket", ctypes.c_uint32),
                ("mBytesPerFrame", ctypes.c_uint32),
                ("mChannelsPerFrame", ctypes.c_uint32),
                ("mBitsPerChannel", ctypes.c_uint32),
                ("mReserved", ctypes.c_uint32)]


class AudioBuffer(ctypes.Structure):
    _fields_ = [("mNumberChannels", ctypes.c_uint32),
                ("mDataByteSize", ctypes.c_uint32),
                ("mData", ctypes.c_void_p)]


class AudioBufferList(ctypes.Structure):
    """A count and a flexible array. Declared with one element because that
    is what an interleaved stream produces, and the count is checked."""

    _fields_ = [("mNumberBuffers", ctypes.c_uint32),
                ("mBuffers", AudioBuffer * 1)]


class AudioObjectPropertyAddress(ctypes.Structure):
    _fields_ = [("mSelector", ctypes.c_uint32),
                ("mScope", ctypes.c_uint32),
                ("mElement", ctypes.c_uint32)]


# The timestamp is never read here, so it stays an opaque pointer rather than
# forty more bytes of struct that would have to be right.
RENDER_CALLBACK = ctypes.CFUNCTYPE(
    ctypes.c_int32,                             # OSStatus
    ctypes.c_void_p,                            # inRefCon
    ctypes.POINTER(ctypes.c_uint32),            # ioActionFlags
    ctypes.c_void_p,                            # inTimeStamp
    ctypes.c_uint32,                            # inBusNumber
    ctypes.c_uint32,                            # inNumberFrames
    ctypes.POINTER(AudioBufferList))            # ioData


class AURenderCallbackStruct(ctypes.Structure):
    _fields_ = [("inputProc", RENDER_CALLBACK),
                ("inputProcRefCon", ctypes.c_void_p)]


# -- pure helpers ----------------------------------------------------------
#
# No ctypes below this line until the framework itself. These are the parts a
# test can reach on a machine with no sound card, and on Linux.

def stream_format(rate, channels, fmt=FLOAT32):
    """The ASBD for interleaved PCM at ``rate``.

    Interleaved and packed, because that is what the mixer already produces
    and what the ring already holds; asking CoreAudio for the non-interleaved
    layout it prefers would mean splitting every frame into per-channel
    buffers on the realtime thread, which is exactly the kind of work that
    thread is not allowed to do.
    """
    bits = SAMPLE_BYTES[fmt] * 8
    flags = _FLAG_IS_PACKED
    flags |= _FLAG_IS_FLOAT if fmt == FLOAT32 else _FLAG_IS_SIGNED_INTEGER
    if sys.byteorder == "big":
        flags |= _FLAG_IS_BIG_ENDIAN
    frame_bytes = channels * SAMPLE_BYTES[fmt]
    return AudioStreamBasicDescription(
        mSampleRate=float(rate),
        mFormatID=_FORMAT_LINEAR_PCM,
        mFormatFlags=flags,
        mBytesPerPacket=frame_bytes,
        mFramesPerPacket=1,
        mBytesPerFrame=frame_bytes,
        mChannelsPerFrame=channels,
        mBitsPerChannel=bits,
        mReserved=0)


def describes_interleaved(asbd):
    """True when an ASBD we were handed back is the layout we asked for."""
    return not (asbd.mFormatFlags & _FLAG_IS_NON_INTERLEAVED)


def status_message(action, status):
    """What to tell a user when an OSStatus comes back non-zero."""
    return "%s failed: %s" % (action, fourcc_name(status))


# -- loading ---------------------------------------------------------------

def _load():
    """Open the frameworks and declare every signature. Idempotent.

    Declaring signatures is not optional. ctypes defaults a return type to
    ``c_int``, which truncates a 64-bit AudioUnit handle to a wild pointer --
    the same class of bug that shipped once in the Cocoa backend as a
    segfault on the first frame, and here it would be a segfault on the audio
    thread, where there is no Python frame to print.
    """
    if _libs:
        return
    if sys.platform != "darwin":
        raise AudioUnavailable("CoreAudio needs macOS")
    try:
        for name, path in _FRAMEWORKS.items():
            _libs[name] = ctypes.cdll.LoadLibrary(path)
    except OSError as exc:
        _libs.clear()
        raise AudioUnavailable("cannot load AudioToolbox: %s" % exc) from exc
    _declare()


def _declare():
    toolbox, hardware = _libs["audiotoolbox"], _libs["coreaudio"]
    status = ctypes.c_int32
    unit = ctypes.c_void_p
    uint32 = ctypes.c_uint32
    signatures = [
        (toolbox, "AudioComponentFindNext", ctypes.c_void_p,
         [ctypes.c_void_p, ctypes.POINTER(AudioComponentDescription)]),
        (toolbox, "AudioComponentInstanceNew", status,
         [ctypes.c_void_p, ctypes.POINTER(unit)]),
        (toolbox, "AudioComponentInstanceDispose", status, [unit]),
        (toolbox, "AudioUnitInitialize", status, [unit]),
        (toolbox, "AudioUnitUninitialize", status, [unit]),
        (toolbox, "AudioUnitSetProperty", status,
         [unit, uint32, uint32, uint32, ctypes.c_void_p, uint32]),
        (toolbox, "AudioUnitGetProperty", status,
         [unit, uint32, uint32, uint32, ctypes.c_void_p,
          ctypes.POINTER(uint32)]),
        (toolbox, "AudioOutputUnitStart", status, [unit]),
        (toolbox, "AudioOutputUnitStop", status, [unit]),
        (hardware, "AudioObjectGetPropertyData", status,
         [uint32, ctypes.POINTER(AudioObjectPropertyAddress), uint32,
          ctypes.c_void_p, ctypes.POINTER(uint32), ctypes.c_void_p]),
    ]
    for lib, name, restype, argtypes in signatures:
        fn = getattr(lib, name)
        fn.restype = restype
        fn.argtypes = argtypes


def _hardware_uint32(device, selector, scope):
    """One HAL property, or None. Best effort by design.

    These are the numbers that turn "the ring has 900 frames in it" into "the
    speaker is 31 milliseconds behind", and a device that declines to answer
    costs a slightly wrong lip sync rather than a failure to play.
    """
    address = AudioObjectPropertyAddress(selector, scope, 0)
    value = ctypes.c_uint32(0)
    size = ctypes.c_uint32(ctypes.sizeof(value))
    status = _libs["coreaudio"].AudioObjectGetPropertyData(
        device, ctypes.byref(address), 0, None, ctypes.byref(size),
        ctypes.byref(value))
    return None if status else value.value


def _default_output_device():
    """The AudioObjectID of whatever Sound preferences points at, or None."""
    address = AudioObjectPropertyAddress(_HW_DEFAULT_OUTPUT_DEVICE,
                                         _SCOPE_GLOBAL_HW, 0)
    value = ctypes.c_uint32(0)
    size = ctypes.c_uint32(ctypes.sizeof(value))
    status = _libs["coreaudio"].AudioObjectGetPropertyData(
        _SYSTEM_OBJECT, ctypes.byref(address), 0, None, ctypes.byref(size),
        ctypes.byref(value))
    if status or not value.value:
        return None
    return value.value


# -- the device ------------------------------------------------------------

class Device:
    """An output AudioUnit fed from a :class:`heel.Ring`.

    Construction opens the unit, agrees a format with it and stops. Nothing
    is running until :meth:`start`, and nothing is freed until :meth:`close`,
    which stops the unit first and waits for it -- see rule 6.
    """

    name = "coreaudio"

    def __init__(self, rate=48000, channels=2, fmt=FLOAT32, buffer_frames=0):
        _load()
        self.fmt = fmt
        self.channels = int(channels)
        self.frame_bytes = self.channels * SAMPLE_BYTES[fmt]
        self.failure = None
        self.latency = 0.0
        self._unit = ctypes.c_void_p()
        self._ring = None
        self._clock = None
        self._callback = None       # the CFUNCTYPE, which must outlive the AU
        self._running = False
        self._lock = threading.Lock()
        self._open(rate, buffer_frames)

    # -- opening -----------------------------------------------------------

    def _open(self, rate, buffer_frames):
        toolbox = _libs["audiotoolbox"]
        description = AudioComponentDescription(
            _TYPE_OUTPUT, _SUBTYPE_DEFAULT_OUTPUT, _MANUFACTURER_APPLE, 0, 0)
        component = toolbox.AudioComponentFindNext(None,
                                                   ctypes.byref(description))
        if not component:
            raise AudioUnavailable("this Mac has no default output AudioUnit")
        status = toolbox.AudioComponentInstanceNew(component,
                                                   ctypes.byref(self._unit))
        if status or not self._unit:
            raise AudioUnavailable(
                status_message("opening the output AudioUnit", status))
        try:
            self.rate = self._agree_format(rate)
            self._install_callback()
            status = toolbox.AudioUnitInitialize(self._unit)
            if status:
                raise AudioUnavailable(
                    status_message("initialising the output AudioUnit",
                                   status))
        except BaseException:
            self._dispose()
            raise
        self.latency = self._measure_latency(buffer_frames)

    def _agree_format(self, wanted):
        """Take the hardware's own rate, and give it interleaved floats.

        Asking for a rate the device is not running at works -- the default
        output unit will convert -- but it means a second resampler in the
        chain that we neither wrote nor measured, running on the realtime
        thread. Ours is upstream, on a thread that can afford it, so the
        thing to do is find out what the hardware wants and want that.
        """
        toolbox = _libs["audiotoolbox"]
        hardware = AudioStreamBasicDescription()
        size = ctypes.c_uint32(ctypes.sizeof(hardware))
        status = toolbox.AudioUnitGetProperty(
            self._unit, _PROP_STREAM_FORMAT, _SCOPE_OUTPUT, 0,
            ctypes.byref(hardware), ctypes.byref(size))
        rate = int(hardware.mSampleRate) if not status and \
            hardware.mSampleRate > 0 else int(wanted)
        asbd = stream_format(rate, self.channels, self.fmt)
        status = toolbox.AudioUnitSetProperty(
            self._unit, _PROP_STREAM_FORMAT, _SCOPE_INPUT, 0,
            ctypes.byref(asbd), ctypes.sizeof(asbd))
        if status:
            raise AudioUnavailable(
                status_message("setting the output stream format", status))
        # Read it back. An AudioUnit is allowed to accept a format and then
        # hand out a different one, and the difference we cannot survive is
        # non-interleaved, because the callback would be writing one channel
        # into a buffer sized for two.
        check = AudioStreamBasicDescription()
        size = ctypes.c_uint32(ctypes.sizeof(check))
        status = toolbox.AudioUnitGetProperty(
            self._unit, _PROP_STREAM_FORMAT, _SCOPE_INPUT, 0,
            ctypes.byref(check), ctypes.byref(size))
        if not status and not describes_interleaved(check):
            raise AudioUnavailable(
                "the output AudioUnit insists on non-interleaved buffers")
        return rate

    def _install_callback(self):
        """Hand the AudioUnit the function it will call forever.

        The CFUNCTYPE object is kept on ``self``. ctypes does not retain a
        callback for you, and a collected one leaves CoreAudio holding a
        pointer into freed memory that it will call within ten milliseconds.
        """
        self._callback = RENDER_CALLBACK(self._render)
        struct = AURenderCallbackStruct(self._callback, None)
        status = _libs["audiotoolbox"].AudioUnitSetProperty(
            self._unit, _PROP_SET_RENDER_CALLBACK, _SCOPE_INPUT, 0,
            ctypes.byref(struct), ctypes.sizeof(struct))
        if status:
            raise AudioUnavailable(
                status_message("installing the render callback", status))

    def _measure_latency(self, buffer_frames):
        """Seconds between a frame leaving the ring and reaching the air.

        Three numbers the HAL is willing to give up -- the device's own
        latency, its safety offset and the size of the buffer it asks for --
        plus whatever the AudioUnit says it adds. All of them are best
        effort: a device that answers none of them costs a lip sync that is
        out by ten milliseconds, which nobody has ever noticed, and none of
        them can stop the audio playing.
        """
        total = 0.0
        unit_latency = ctypes.c_double(0.0)
        size = ctypes.c_uint32(ctypes.sizeof(unit_latency))
        status = _libs["audiotoolbox"].AudioUnitGetProperty(
            self._unit, _PROP_LATENCY, _SCOPE_GLOBAL, 0,
            ctypes.byref(unit_latency), ctypes.byref(size))
        if not status:
            total += unit_latency.value
        device = _default_output_device()
        if device:
            frames = 0
            for selector in (_DEV_LATENCY, _DEV_SAFETY_OFFSET):
                value = _hardware_uint32(device, selector, _SCOPE_OUTPUT_HW)
                if value:
                    frames += value
            size = _hardware_uint32(device, _DEV_BUFFER_FRAME_SIZE,
                                    _SCOPE_OUTPUT_HW)
            if size:
                frames += size
            elif buffer_frames:
                frames += buffer_frames
            total += frames / float(self.rate)
        return total

    # -- the realtime thread -----------------------------------------------

    def _render(self, _ref, _flags, _timestamp, _bus, frames, iodata):
        """CoreAudio's callback. Read the module docstring before editing.

        Every line of this is chosen for what it does not do. There is no
        lock, no allocation of anything that can grow, no call into the mixer,
        no formatting, no attribute that was not already there, and no path
        out of it that raises. What it does is two integer comparisons, a
        ``memmove`` or two out of the ring, and a ``memset`` over whatever the
        ring could not supply.
        """
        try:
            buffers = iodata.contents
            if buffers.mNumberBuffers != 1:
                # Not the layout we agreed. Silence is the only safe answer,
                # and saying so once is the main thread's job, not ours.
                return 0
            buffer = buffers.mBuffers[0]
            address = buffer.mData
            wanted = frames * self.frame_bytes
            if not address or wanted <= 0:
                return 0
            if wanted > buffer.mDataByteSize:
                wanted = buffer.mDataByteSize
            moved = self._ring.read_into(address, wanted)
            if moved < wanted:
                ctypes.memset(address + moved, 0, wanted - moved)
                self._clock.silent_frames += (wanted - moved) \
                    // self.frame_bytes
                self._clock.underruns += 1
            self._clock.frames += wanted // self.frame_bytes
        except BaseException as exc:            # noqa: BLE001 - see rule 3
            # Whatever this was, it must not become a traceback printed from
            # the audio thread. Record it and go quiet.
            try:
                self.failure = exc
            except BaseException:               # pragma: no cover
                pass
        return 0

    # -- running -----------------------------------------------------------

    def start(self, ring, clock):
        if ring.frame_bytes != self.frame_bytes:
            raise AudioUnavailable(
                "the ring holds %d-byte frames and the device wants %d"
                % (ring.frame_bytes, self.frame_bytes))
        with self._lock:
            if self._running:
                return
            self._ring = ring
            self._clock = clock
            status = _libs["audiotoolbox"].AudioOutputUnitStart(self._unit)
            if status:
                raise AudioUnavailable(
                    status_message("starting the output AudioUnit", status))
            self._running = True

    def stop(self):
        with self._lock:
            if not self._running:
                return
            _libs["audiotoolbox"].AudioOutputUnitStop(self._unit)
            self._running = False

    def close(self):
        """Stop, uninitialise, dispose. In that order, and only that order.

        ``AudioOutputUnitStop`` is what guarantees the callback is not
        running, and ``AudioUnitUninitialize`` is what guarantees it will not
        start again. Dropping the ring before either of those is a use of
        freed memory on a thread with no Python on its stack.
        """
        self.stop()
        with self._lock:
            if self._unit:
                _libs["audiotoolbox"].AudioUnitUninitialize(self._unit)
            self._dispose()
            self._ring = None
            self._clock = None
            self._callback = None

    def _dispose(self):
        if self._unit:
            _libs["audiotoolbox"].AudioComponentInstanceDispose(self._unit)
            self._unit = ctypes.c_void_p()

    def __repr__(self):
        return ("<coreaudio.Device %d Hz x%d %s, latency %.1f ms>"
                % (self.rate, self.channels, self.fmt, self.latency * 1000))


def available():
    """True when a CoreAudio output can actually be opened here."""
    global _problem
    _problem = ""
    if sys.platform != "darwin":
        return False
    try:
        device = Device()
    except (AudioUnavailable, OSError) as exc:
        _problem = str(exc)
        return False
    device.close()
    return True


def unavailable_reason():
    """Why available() last said no, or "" when this is simply not macOS.

    "CoreAudio needs macOS" is not news to anyone running Linux, so the wrong
    platform says nothing at all and only a real failure speaks up.
    """
    return _problem
