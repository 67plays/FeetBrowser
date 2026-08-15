"""Real sound out of a Windows box, with no bindings package and no sound library.

``winmm`` is a C API, so ctypes is all it takes to drive it: load
``winmm.dll``, declare the signatures, call the functions. No pywin32, no
comtypes, no compiled shim -- the same rule ``win32.py`` follows, and for the
same reason.


Why waveOut and not WASAPI
--------------------------

WASAPI is the modern interface and this is not it, so the choice is worth
defending rather than assuming.

WASAPI has no C API. It is COM, and all of it is COM: to play one sample you
must ``CoCreateInstance`` an ``IMMDeviceEnumerator``, call
``GetDefaultAudioEndpoint`` for an ``IMMDevice``, ``Activate`` an
``IAudioClient`` off that, ``GetMixFormat``, ``Initialize``, ``GetService``
an ``IAudioRenderClient``, and then drive ``GetBuffer``/``ReleaseBuffer``
against an event handle. Through ctypes there are no COM headers, so every
one of those calls is a hand-counted vtable slot -- ``IAudioClient::Start`` is
slot 10 if you have counted ``IUnknown``'s three and the seven before it
correctly, and slot 9 or 11 if you have not. A mis-numbered slot is not an
exception. It is a call through whatever pointer was at that offset, which is
an access violation that takes the process with it, on a machine that nobody
working on this branch has, in a subsystem whose failure mode is already
"silence, and you cannot tell why".

waveOut is a flat C API of eight functions, which is exactly the shape every
other binding in this project has. It is not deprecated: on Windows 10 and 11
it is implemented on top of WASAPI shared mode by the OS itself, so the
samples travel the same path -- we simply are not the ones assembling the COM
call to put them there. What it costs is two things, and neither of them is
something ``<video>`` audio needs. It cannot open a device in exclusive mode,
which a browser tab has no business doing to the rest of the machine anyway.
And it has more latency, because the OS mixer's own buffering sits under our
queue; that is a fixed offset, the audio clock is what A/V sync reads, and a
fixed offset is the one kind of error a sync loop does not care about.

If exclusive mode or sub-ten-millisecond latency ever matters here, this file
is the thing to replace, and the rest of the audio stack will not notice --
which is the actual argument. The interface a backend has to satisfy is five
methods wide.


How it works
------------

Like the ALSA backend and unlike the CoreAudio one, **there is no realtime
callback**: ``waveOutOpen`` is given ``CALLBACK_EVENT`` rather than a function
pointer, so Windows signals an event object when a buffer finishes and a
thread of ours wakes up and queues another. That means there is no ctypes
trampoline on an OS audio thread anywhere in this file -- which is worth
having, because the alternative, ``CALLBACK_FUNCTION``, is documented as
being called at interrupt time with almost every Win32 call forbidden inside
it, and "may not call anything" and "is a Python function" do not belong in
the same sentence.

Four buffers are prepared once, at open, and recycled forever. Frames go from
the ring into whichever one Windows has handed back, using the same
``read_into`` the realtime backend uses, so nothing in the loop allocates and
the two backends cannot drift apart in how a short read is handled.

Sixteen-bit samples, not floats. ``waveOut`` will take
``WAVE_FORMAT_IEEE_FLOAT``, but only via ``WAVEFORMATEXTENSIBLE`` on some
drivers and plain ``WAVEFORMATEX`` on others, and the failure is a driver
that accepts the format and plays noise. Sixteen-bit PCM is the format every
sound card on earth has supported since 1992. ``heel`` mixes in floats and
converts once, on the mixer thread, so the only thing this costs is the
conversion that was going to happen anyway.

Everything here that is arithmetic or a lookup lives in a module-level
function, so the part that can only be exercised on Windows is as small as it
can be made -- see ``tests/test_audio.py`` for both halves.
"""
import ctypes
import sys
import threading

from .heel import INT16, SAMPLE_BYTES, AudioUnavailable

# -- types -----------------------------------------------------------------
#
# Spelled out rather than taken from ctypes.wintypes, because wintypes derives
# LONG from c_long -- which is 8 bytes on a 64-bit Unix and 4 on Windows.
# WAVEFORMATEX and WAVEHDR are wire formats handed to a driver that reads
# exactly as many bytes as its own header says, so a field of the wrong width
# is not a mismatch, it is whatever happened to be on the stack.
DWORD = ctypes.c_uint32
WORD = ctypes.c_uint16
UINT = ctypes.c_uint32
BOOL = ctypes.c_int32
HANDLE = ctypes.c_void_p
MMRESULT = ctypes.c_uint32
# Pointer-sized on both ABIs, so these take the pointer-sized C types rather
# than a fixed width.
DWORD_PTR = ctypes.c_size_t

# waveOutOpen flags.
_WAVE_MAPPER = 0xFFFFFFFF       # "whatever the user has chosen", as -1
_WAVE_FORMAT_QUERY = 0x0001
_CALLBACK_EVENT = 0x00050000
_WAVE_FORMAT_PCM = 1

# WAVEHDR flags. WHDR_DONE is the driver saying "this one has been played and
# is yours again", and it is the only one this file reads.
_WHDR_DONE = 0x00000001

# How much sound to keep queued. Four blocks of twenty milliseconds is eighty,
# which is inside the ring's eighty-five and outside anything the mixer thread
# will plausibly be late by. Fewer, larger blocks would be cheaper and would
# make every seek take longer to be heard; more, smaller ones cost a wakeup
# each and buy latency the OS mixer under us then gives back.
BLOCK_MILLISECONDS = 20
BLOCKS = 4

# Rates to try if the device refuses the one that was asked for. The wave
# mapper resamples for anybody, so in practice the first attempt succeeds;
# this is for the machine whose only device is a fixed-rate one.
RATES = (48000, 44100, 32000, 22050, 16000, 11025, 8000)

_libs = {}
_problem = ""


class WAVEFORMATEX(ctypes.Structure):
    """The format a wave device is being asked for.

    ``cbSize`` is zero because this is plain PCM and there is no extension
    following. A non-zero one here with nothing behind it is a driver reading
    past the end of the structure.

    Packed, because ``mmsystem.h`` wraps every one of its structures in
    ``pshpack1.h``. It is why ``sizeof(WAVEFORMATEX)`` is the famous 18 and
    not the 20 that natural alignment would give it.
    """

    _pack_ = 1
    _fields_ = [("wFormatTag", WORD), ("nChannels", WORD),
                ("nSamplesPerSec", DWORD), ("nAvgBytesPerSec", DWORD),
                ("nBlockAlign", WORD), ("wBitsPerSample", WORD),
                ("cbSize", WORD)]


class WAVEHDR(ctypes.Structure):
    """One queued block. The driver owns it between Write and WHDR_DONE.

    ``lpNext`` and ``reserved`` are the driver's, not ours, and are declared
    only so that the fields before them are at the right offsets -- which is
    the difference between a played buffer and a driver writing its linked
    list over our sample data.

    ``sizeof`` this one is passed to every call that takes it, so it has to be
    the 48 bytes a 64-bit driver expects. Packed for the same reason as
    WAVEFORMATEX, though here every field is already aligned and the pragma
    changes nothing -- which is exactly why it is worth stating rather than
    relying on.
    """

    _pack_ = 1


WAVEHDR._fields_ = [("lpData", ctypes.c_void_p),
                    ("dwBufferLength", DWORD), ("dwBytesRecorded", DWORD),
                    ("dwUser", DWORD_PTR), ("dwFlags", DWORD),
                    ("dwLoops", DWORD),
                    ("lpNext", ctypes.POINTER(WAVEHDR)),
                    ("reserved", DWORD_PTR)]


# -- pure helpers ----------------------------------------------------------

def wave_format(rate, channels, fmt=INT16):
    """A WAVEFORMATEX for interleaved PCM.

    ``nBlockAlign`` and ``nAvgBytesPerSec`` are derived rather than passed in,
    because a driver believes them: a wrong block align plays at the wrong
    speed rather than failing to open.
    """
    if fmt != INT16:
        raise AudioUnavailable("waveOut is only given 16-bit samples here")
    bits = SAMPLE_BYTES[fmt] * 8
    align = channels * SAMPLE_BYTES[fmt]
    return WAVEFORMATEX(_WAVE_FORMAT_PCM, channels, int(rate),
                        int(rate) * align, align, bits, 0)


def block_frames(rate, milliseconds=BLOCK_MILLISECONDS):
    """Frames in one queued block, never zero and never absurd."""
    frames = int(rate * milliseconds / 1000.0)
    return max(64, frames)


def rate_candidates(wanted):
    """The rates to try, wanted one first and no repeats."""
    out = [int(wanted)]
    out.extend(rate for rate in RATES if rate != int(wanted))
    return out


def error_message(action, code):
    """What to tell a user when winmm returns a non-zero MMRESULT.

    ``waveOutGetErrorTextW`` is what Windows itself would print, and it knows
    the names of its own errors, so it is worth asking before falling back to
    a number nobody can look up.
    """
    text = ""
    if _libs:
        try:
            buf = ctypes.create_unicode_buffer(256)
            if _libs["winmm"].waveOutGetErrorTextW(code, buf, 256) == 0:
                text = buf.value
        except Exception:                       # noqa: BLE001 - diagnostics
            text = ""
    return "%s failed: %s (%d)" % (action, text or "MMSYSERR %d" % code, code)


# -- loading ---------------------------------------------------------------

def _load():
    """Open the DLLs and declare every signature. Idempotent.

    Declaring signatures is not optional. ctypes defaults a return type to
    ``c_int``, which truncates a 64-bit HWAVEOUT to a wild handle -- the same
    class of bug that shipped once in the Cocoa backend as a segfault on the
    first frame, and here it would be a segfault inside a driver.
    """
    if _libs:
        return
    if sys.platform != "win32":
        raise AudioUnavailable("waveOut needs Windows")
    windll = getattr(ctypes, "WinDLL", None)
    if windll is None:      # pragma: no cover - unreachable on Windows
        raise AudioUnavailable("this Python has no stdcall support")
    try:
        for name in ("winmm", "kernel32"):
            _libs[name] = windll(name + ".dll", use_last_error=True)
    except OSError as exc:
        _libs.clear()
        raise AudioUnavailable("cannot load %s: %s" % (name, exc)) from exc
    _declare()


def _declare():
    winmm, kernel32 = _libs["winmm"], _libs["kernel32"]
    hwo = HANDLE
    header = ctypes.POINTER(WAVEHDR)
    signatures = [
        (winmm, "waveOutGetNumDevs", UINT, []),
        (winmm, "waveOutOpen", MMRESULT,
         [ctypes.POINTER(hwo), UINT, ctypes.POINTER(WAVEFORMATEX),
          DWORD_PTR, DWORD_PTR, DWORD]),
        (winmm, "waveOutClose", MMRESULT, [hwo]),
        (winmm, "waveOutPrepareHeader", MMRESULT, [hwo, header, UINT]),
        (winmm, "waveOutUnprepareHeader", MMRESULT, [hwo, header, UINT]),
        (winmm, "waveOutWrite", MMRESULT, [hwo, header, UINT]),
        (winmm, "waveOutReset", MMRESULT, [hwo]),
        (winmm, "waveOutGetErrorTextW", MMRESULT,
         [MMRESULT, ctypes.c_wchar_p, UINT]),
        (kernel32, "CreateEventW", HANDLE,
         [ctypes.c_void_p, BOOL, BOOL, ctypes.c_wchar_p]),
        (kernel32, "SetEvent", BOOL, [HANDLE]),
        (kernel32, "CloseHandle", BOOL, [HANDLE]),
        (kernel32, "WaitForSingleObject", DWORD, [HANDLE, DWORD]),
    ]
    for lib, name, restype, argtypes in signatures:
        fn = getattr(lib, name)
        fn.restype = restype
        fn.argtypes = argtypes


def _device_count():
    """How many wave output devices Windows admits to having."""
    return int(_libs["winmm"].waveOutGetNumDevs())


# -- the device ------------------------------------------------------------

class Device:
    """A waveOut queue fed from a :class:`heel.Ring` by a thread of our own.

    Construction opens the device, agrees a format with it, prepares the
    blocks and stops. Nothing is queued until :meth:`start`, and nothing is
    freed until :meth:`close`, which stops the thread and waits for it -- see
    rule 6.
    """

    name = "winmm"

    def __init__(self, rate=48000, channels=2, fmt=INT16, blocks=BLOCKS):
        _load()
        if fmt != INT16:
            # Nothing asks for anything else -- heel.open_output lets each
            # backend pick its own format and this one picks INT16 -- so this
            # is a caller who passed fmt= by hand, and saying no plainly beats
            # opening a device that will play noise.
            raise AudioUnavailable("waveOut is opened with 16-bit samples")
        self.fmt = fmt
        self.channels = int(channels)
        self.frame_bytes = self.channels * SAMPLE_BYTES[fmt]
        self.failure = None
        self.latency = 0.0
        self.blocks = max(2, int(blocks))
        self._handle = HANDLE()
        self._event = None
        self._headers = []
        self._buffers = []
        self._queued = []
        self._prepared = False
        self._ring = None
        self._clock = None
        self._thread = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._open(rate)

    # -- opening -----------------------------------------------------------

    def _open(self, rate):
        if _device_count() <= 0:
            raise AudioUnavailable("this machine has no wave output device")
        # Auto-reset, initially unsignalled, unnamed. Auto-reset is what makes
        # the feeder thread's wait mean "one more buffer came back" rather
        # than "at least one did at some point in the past".
        self._event = _libs["kernel32"].CreateEventW(None, False, False, None)
        if not self._event:
            raise AudioUnavailable("cannot create the wave-out event object")
        try:
            self.rate = self._agree_format(rate)
            self._prepare_blocks()
        except BaseException:
            self._release()
            raise
        self.period = block_frames(self.rate)
        # What is in flight when the queue is full. The OS mixer adds its own
        # buffering under this and does not say how much, so this is a floor
        # rather than a measurement -- see the module docstring on why a fixed
        # offset is the error a sync loop minds least.
        self.latency = self.blocks * self.period / float(self.rate)

    def _agree_format(self, wanted):
        """Find a rate the device will take, asking before committing.

        ``WAVE_FORMAT_QUERY`` opens nothing: it is waveOutOpen used as a
        question, which is the only way to ask a wave device what it supports
        without a device-capabilities structure per format.
        """
        winmm = _libs["winmm"]
        last = 0
        for candidate in rate_candidates(wanted):
            fmt = wave_format(candidate, self.channels, self.fmt)
            status = winmm.waveOutOpen(None, _WAVE_MAPPER, ctypes.byref(fmt),
                                       0, 0, _WAVE_FORMAT_QUERY)
            if status:
                last = status
                continue
            status = winmm.waveOutOpen(
                ctypes.byref(self._handle), _WAVE_MAPPER, ctypes.byref(fmt),
                self._event or 0, 0, _CALLBACK_EVENT)
            if not status and self._handle:
                return candidate
            last = status
        raise AudioUnavailable(
            error_message("opening a wave output device for %d channels"
                          % self.channels, last))

    def _prepare_blocks(self):
        """Allocate and prepare every block once, here, and never again.

        ``waveOutPrepareHeader`` is the call that page-locks the buffer for
        DMA. Doing it per block per write is the standard way to write this
        and it is a page-table operation on the feeding thread every twenty
        milliseconds forever; doing it once means the steady state is one
        ``waveOutWrite`` per block and nothing else.
        """
        winmm = _libs["winmm"]
        size = block_frames(self.rate) * self.frame_bytes
        for _ in range(self.blocks):
            buf = ctypes.create_string_buffer(size)
            header = WAVEHDR()
            header.lpData = ctypes.addressof(buf)
            header.dwBufferLength = size
            status = winmm.waveOutPrepareHeader(self._handle,
                                                ctypes.byref(header),
                                                ctypes.sizeof(header))
            if status:
                raise AudioUnavailable(
                    error_message("preparing a wave-out block", status))
            self._buffers.append(buf)
            self._headers.append(header)
            self._queued.append(False)
        self._prepared = True

    # -- the feeding thread ------------------------------------------------

    def _run(self):
        """Ring to driver, one block at a time, until told to stop.

        This is the consumer, and it is the counterpart of the CoreAudio
        render callback: the same short-read handling, the same clock
        bookkeeping, the same refusal to raise. Frames are counted on the
        clock when the driver hands a block *back*, not when it is queued,
        because the clock is frames that have been played and a queued block
        has not been.
        """
        winmm, kernel32 = _libs["winmm"], _libs["kernel32"]
        ring, clock = self._ring, self._clock
        frame_bytes = self.frame_bytes
        wanted = self.period * frame_bytes
        wait = max(1, int(BLOCK_MILLISECONDS * 2))
        while not self._stop.is_set():
            moved_any = False
            for index, header in enumerate(self._headers):
                if self._queued[index]:
                    if not header.dwFlags & _WHDR_DONE:
                        continue
                    self._queued[index] = False
                    clock.frames += header.dwBufferLength // frame_bytes
                address = header.lpData
                moved = ring.read_into(address, wanted)
                if moved < wanted:
                    ctypes.memset(address + moved, 0, wanted - moved)
                    clock.silent_frames += (wanted - moved) // frame_bytes
                    clock.underruns += 1
                status = winmm.waveOutWrite(self._handle,
                                            ctypes.byref(header),
                                            ctypes.sizeof(header))
                if status:
                    self.failure = AudioUnavailable(
                        error_message("queueing a wave-out block", status))
                    self._stop.set()
                    return
                self._queued[index] = True
                moved_any = True
            if not moved_any:
                # Every block is with the driver. Sleeping on the event is
                # what makes this thread cost nothing while sound is playing;
                # the timeout is only there so that stop() is noticed by a
                # device that has stopped signalling.
                kernel32.WaitForSingleObject(self._event, wait)

    # -- running -----------------------------------------------------------

    def start(self, ring, clock):
        if ring.frame_bytes != self.frame_bytes:
            raise AudioUnavailable(
                "the ring holds %d-byte frames and the device wants %d"
                % (ring.frame_bytes, self.frame_bytes))
        with self._lock:
            if self._thread is not None:
                return
            if not self._handle:
                raise AudioUnavailable("this wave output device is closed")
            self._ring = ring
            self._clock = clock
            self._stop.clear()
            self._thread = threading.Thread(target=self._run,
                                            name="heel-winmm", daemon=True)
            self._thread.start()

    def stop(self):
        """Stop feeding, wake the thread, and wait for it to be out of winmm.

        ``waveOutReset`` marks every queued block done and returns it, which
        is both how the queue is emptied and how a thread waiting on the
        event is woken; ``SetEvent`` covers the case where the reset finds
        nothing to return. Joining afterwards is what makes :meth:`close`
        safe.
        """
        with self._lock:
            thread, self._thread = self._thread, None
        self._stop.set()
        if self._handle:
            _libs["winmm"].waveOutReset(self._handle)
        if self._event:
            _libs["kernel32"].SetEvent(self._event)
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2.0)
        for index in range(len(self._queued)):
            self._queued[index] = False

    def close(self):
        """Stop, then let go. In that order, and only that order.

        The thread is joined and the driver is reset *before* the headers it
        was handed are unprepared and the buffers behind them dropped. Freeing
        a block a driver still owns is not an exception, it is DMA into freed
        memory.
        """
        self.stop()
        with self._lock:
            self._release()
            self._ring = None
            self._clock = None

    def _release(self):
        winmm, kernel32 = _libs["winmm"], _libs["kernel32"]
        if self._prepared and self._handle:
            for header in self._headers:
                winmm.waveOutUnprepareHeader(self._handle,
                                             ctypes.byref(header),
                                             ctypes.sizeof(header))
        self._prepared = False
        self._headers = []
        self._buffers = []
        self._queued = []
        if self._handle:
            winmm.waveOutClose(self._handle)
            self._handle = HANDLE()
        if self._event:
            kernel32.CloseHandle(self._event)
            self._event = None

    def __repr__(self):
        return ("<winmm.Device %d Hz x%d %s, %d blocks, latency %.1f ms>"
                % (self.rate, self.channels, self.fmt, self.blocks,
                   self.latency * 1000))


def available():
    """True when a wave output device can actually be opened here."""
    global _problem
    _problem = ""
    if sys.platform != "win32":
        return False
    try:
        device = Device(fmt=INT16)
    except (AudioUnavailable, OSError) as exc:
        _problem = str(exc)
        return False
    device.close()
    return True


def unavailable_reason():
    """Why available() last said no, or "" when this is simply not Windows.

    "waveOut needs Windows" is not news to anyone running Linux, so the wrong
    platform says nothing at all and only a real failure speaks up.
    """
    return _problem
