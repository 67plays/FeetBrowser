"""Real sound out of a Linux box, with no bindings package and no sound library.

ALSA is a C library, so ctypes is all it takes to drive it: open
``libasound.so.2``, declare the signatures, call the functions. No pyalsaaudio,
no PortAudio, no sounddevice, no compiled shim -- the same rule ``x11.py``
follows, and for the same reason.

What this adds to ``heel.Ring`` is the one thing a ring cannot have on its
own: something that empties it in real time. A PCM handle is opened on the
``default`` device, told what our samples look like, and then written to from
a thread of ours in a loop that blocks until the card has room.

**There is no realtime callback here, and that is the point.** ALSA offers
one -- ``snd_async_add_pcm_handler`` runs on a SIGIO handler -- and it is the
wrong tool from Python twice over: a signal handler cannot take the GIL when
the interpreter is between bytecodes, and CPython only runs Python-level
signal handlers on the main thread anyway. So the model is inverted relative
to the CoreAudio backend: instead of the driver calling us on a thread we do
not own, we call the driver on a thread we do, and ``snd_pcm_writei`` blocks
until the card is ready for more. That is an ordinary Python thread with an
ordinary Python stack, so the six rules in ``heel``'s docstring are much
easier to keep here than they are on macOS -- but the code keeps them anyway,
because the shape of the two backends being the same is worth more than the
few bytes a ``bytes`` allocation per period would cost. Frames go from the
ring into one preallocated buffer via ``read_into`` and out to
``snd_pcm_writei`` from that same buffer, and nothing in the loop allocates.

Underruns are recovered rather than reported. A card that has run dry gives
``-EPIPE`` and stays broken until it is prepared again, and the machine that
did that was almost certainly busy laying out a page for a moment.
``snd_pcm_recover`` puts it back and the loop carries on, having counted the
gap on the clock, which is exactly what the CoreAudio callback does with a
``memset``.

Two things this deliberately does not do. It does not install an ALSA error
handler: ``snd_lib_error_set_handler`` takes a *variadic* callback, and a
variadic ctypes trampoline is a thing that would only ever be executed on a
machine that is already failing -- so ALSA's own complaints go to stderr,
untouched and unhidden. And it does not use ``snd_pcm_hw_params``: the twenty
calls that involves buy the ability to enumerate rates, which the four-line
loop in :func:`_negotiate` gets by trying them.

Everything here that is arithmetic or a lookup lives in a module-level
function, so the part that can only be exercised against a real sound card is
as small as it can be made -- see ``tests/test_audio.py`` for both halves.
"""
import ctypes
import ctypes.util
import errno
import os
import sys
import threading

from .heel import FLOAT32, INT16, SAMPLE_BYTES, AudioUnavailable

SONAMES = ("libasound.so.2", "libasound.so")

# The PCM to open. "default" is what every other program on the machine opens,
# which means it is whatever the user's ~/.asoundrc, PulseAudio or PipeWire
# has arranged to be the right answer -- including "mix with everyone else"
# rather than "take the card exclusively", which is the only behaviour a
# browser tab is entitled to.
DEFAULT_PCM = "default"

# How much sound the card should hold. Forty milliseconds is two frames of
# video, comfortably more than the mixer thread's worst case and comfortably
# less than the point where a click in a video becomes a click you can see
# was late. ALSA rounds it to something the hardware likes and tells us what
# it picked; the number we then trust is that one, not this one.
BUFFER_MICROSECONDS = 40000

# Rates to try when the card refuses the one that was asked for, in the order
# a browser cares about them. 48000 and 44100 first because they are what
# audio on the web actually is.
RATES = (48000, 44100, 96000, 88200, 32000, 24000, 22050, 16000, 8000)

_STREAM_PLAYBACK = 0
_ACCESS_RW_INTERLEAVED = 3

# snd_pcm_format_t. Little-endian unconditionally, because heel.pack byte-swaps
# on a big-endian machine so that device bytes are always little-endian; there
# is one place in this program that knows about byte order and it is that one.
_FORMAT = {FLOAT32: 14, INT16: 2}       # SND_PCM_FORMAT_FLOAT_LE, S16_LE

# Errors worth naming. These come back from libasound as negative errno, and
# they are the platform's errno rather than Linux's constants written out,
# because this file also loads on a FreeBSD with the ALSA compatibility port
# installed and EAGAIN is 35 there.
_ESTRPIPE = getattr(errno, "ESTRPIPE", 86)

_libs = {}
_problem = ""


# -- types -----------------------------------------------------------------
#
# Spelled out rather than guessed at. snd_pcm_uframes_t is an unsigned long
# and snd_pcm_sframes_t is a signed one, which are eight bytes on the machines
# anyone runs a browser on and four on a 32-bit ARM. ctypes' c_ulong is right
# on both; leaving it to default to c_int is a frame count truncated to
# nonsense on one of them and a silent success on the other, which is the
# worst way for a bug like this to behave.

PCM = ctypes.c_void_p
Uframes = ctypes.c_ulong
Sframes = ctypes.c_long


# -- pure helpers ----------------------------------------------------------

def sample_format(fmt):
    """The snd_pcm_format_t for one of heel's formats."""
    try:
        return _FORMAT[fmt]
    except KeyError:
        raise AudioUnavailable("ALSA cannot be given %r samples" % (fmt,)) \
            from None


def pcm_name():
    """Which PCM to open.

    ``FEETBROWSER_ALSA_DEVICE`` exists because "default" is occasionally the
    HDMI output on a machine whose speakers are the analogue one, and telling
    somebody to export a variable is a great deal better than telling them to
    rewrite their asoundrc.
    """
    return (os.environ.get("FEETBROWSER_ALSA_DEVICE") or "").strip() \
        or DEFAULT_PCM


def rate_candidates(wanted):
    """The rates to try, wanted one first and no repeats."""
    out = [int(wanted)]
    out.extend(rate for rate in RATES if rate != int(wanted))
    return out


def error_message(action, code):
    """What to tell a user when libasound returns a negative errno.

    ``snd_strerror`` is what ALSA itself would print, and it knows the names
    of its own errors as well as the system's, so it is worth asking before
    falling back to a number nobody can look up.
    """
    text = ""
    if _libs:
        try:
            raw = _libs["asound"].snd_strerror(code)
            if raw:
                text = raw.decode("utf-8", "replace")
        except Exception:                       # noqa: BLE001 - diagnostics
            text = ""
    if not text:
        text = os.strerror(-code) if code < 0 else str(code)
    return "%s failed: %s (%d)" % (action, text, code)


# -- loading ---------------------------------------------------------------

def _load():
    """Open libasound and declare every signature. Idempotent.

    Declaring signatures is not optional. ctypes defaults a return type to
    ``c_int``, and ``snd_pcm_writei`` returns a signed long: on a 64-bit
    machine the truncation is invisible for every count a browser will ever
    write and then wrong exactly once, for a negative error code, which is the
    one return value that must not be misread.
    """
    if _libs:
        return
    if sys.platform == "darwin" or sys.platform == "win32":
        raise AudioUnavailable("ALSA is a Linux interface")
    names = list(SONAMES)
    found = ctypes.util.find_library("asound")
    if found:
        names.append(found)
    problem = None
    for name in names:
        try:
            _libs["asound"] = ctypes.CDLL(name)
            break
        except OSError as exc:
            problem = exc
    else:
        raise AudioUnavailable("cannot load libasound (tried %s): %s"
                               % (", ".join(names), problem))
    _declare()


def _declare():
    alsa = _libs["asound"]
    cint, cuint = ctypes.c_int, ctypes.c_uint
    void = ctypes.c_void_p
    signatures = [
        ("snd_pcm_open", cint,
         [ctypes.POINTER(PCM), ctypes.c_char_p, cint, cint]),
        ("snd_pcm_close", cint, [PCM]),
        ("snd_pcm_set_params", cint,
         [PCM, cint, cint, cuint, cuint, cint, cuint]),
        ("snd_pcm_get_params", cint,
         [PCM, ctypes.POINTER(Uframes), ctypes.POINTER(Uframes)]),
        ("snd_pcm_writei", Sframes, [PCM, void, Uframes]),
        ("snd_pcm_prepare", cint, [PCM]),
        ("snd_pcm_recover", cint, [PCM, cint, cint]),
        ("snd_pcm_drop", cint, [PCM]),
        ("snd_pcm_delay", cint, [PCM, ctypes.POINTER(Sframes)]),
        ("snd_strerror", ctypes.c_char_p, [cint]),
    ]
    for name, restype, argtypes in signatures:
        fn = getattr(alsa, name)
        fn.restype = restype
        fn.argtypes = argtypes


def _open_pcm(name):
    """One blocking playback handle, or raise saying why not."""
    handle = PCM()
    status = _libs["asound"].snd_pcm_open(
        ctypes.byref(handle), name.encode("utf-8"), _STREAM_PLAYBACK, 0)
    if status < 0 or not handle:
        raise AudioUnavailable(
            error_message("opening the ALSA device %r" % name, status))
    return handle


def _negotiate(handle, name, fmt, channels, rate):
    """Agree a rate with the card, reopening the handle for each attempt.

    Asking ALSA to resample for us would always work -- ``soft_resample=1``
    makes ``snd_pcm_set_params`` accept anything -- and it would put a second
    resampler in the chain that we neither wrote nor measured. Ours is
    upstream, on a thread that can afford it, so the thing to do is find out
    what the hardware wants and want that. Only if the card refuses every rate
    worth trying does ALSA's own resampler get switched on, and then the
    device says so in its repr.

    Returns ``(handle, rate, resampled)``; the handle may not be the one that
    went in, because a ``set_params`` that failed leaves the PCM in a state
    the next attempt should not have to reason about.
    """
    alsa = _libs["asound"]
    fmt_id = sample_format(fmt)
    last = 0
    for soft in (0, 1):
        for candidate in rate_candidates(rate):
            status = alsa.snd_pcm_set_params(
                handle, fmt_id, _ACCESS_RW_INTERLEAVED, channels, candidate,
                soft, BUFFER_MICROSECONDS)
            if status >= 0:
                return handle, candidate, bool(soft)
            last = status
            alsa.snd_pcm_close(handle)
            handle = _open_pcm(name)
            if soft:
                break       # soft resampling takes any rate or none of them
    alsa.snd_pcm_close(handle)
    raise AudioUnavailable(
        error_message("configuring the ALSA device %r for %d channels"
                      % (name, channels), last))


# -- the device ------------------------------------------------------------

class Device:
    """An ALSA PCM fed from a :class:`heel.Ring` by a thread of our own.

    Construction opens the PCM, agrees a format with it and stops. Nothing is
    running until :meth:`start`, and nothing is freed until :meth:`close`,
    which stops the thread and waits for it -- see rule 6.
    """

    name = "alsa"

    def __init__(self, rate=48000, channels=2, fmt=FLOAT32, device=None):
        _load()
        self.fmt = fmt
        self.channels = int(channels)
        self.frame_bytes = self.channels * SAMPLE_BYTES[fmt]
        self.failure = None
        self.latency = 0.0
        self.resampled = False
        self.device = device or pcm_name()
        self.period = 0
        self._handle = None
        self._buffer = None
        self._address = 0
        self._ring = None
        self._clock = None
        self._thread = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._open(rate)

    # -- opening -----------------------------------------------------------

    def _open(self, rate):
        handle, self.rate, self.resampled = _negotiate(
            _open_pcm(self.device), self.device, self.fmt, self.channels,
            rate)
        self._handle = handle
        buffer_frames, period_frames = self._measure()
        self.period = period_frames
        self.latency = buffer_frames / float(self.rate)
        # One buffer, allocated here and reused until close. The write loop
        # moves frames into it with the same read_into the realtime backend
        # uses, so neither backend allocates per period and neither one can
        # drift from the other in how a short read is handled.
        self._buffer = ctypes.create_string_buffer(
            period_frames * self.frame_bytes)
        self._address = ctypes.addressof(self._buffer)

    def _measure(self):
        """What ALSA actually gave us, in frames. Falls back on a refusal.

        ``snd_pcm_get_params`` reports the buffer and period it settled on,
        which are the only numbers worth believing: the microseconds we asked
        for are a request, and a card whose period must be a power of two will
        have quietly rounded them.
        """
        buffer_frames = Uframes(0)
        period_frames = Uframes(0)
        status = _libs["asound"].snd_pcm_get_params(
            self._handle, ctypes.byref(buffer_frames),
            ctypes.byref(period_frames))
        if status < 0 or not period_frames.value:
            # A device that will not say costs a lip sync that is out by a
            # few milliseconds, not a failure to play.
            frames = max(1, int(self.rate * BUFFER_MICROSECONDS / 1000000.0))
            return frames, max(64, frames // 4)
        return int(buffer_frames.value), int(period_frames.value)

    # -- the writing thread ------------------------------------------------

    def _run(self):
        """Ring to card, one period at a time, until told to stop.

        This is the consumer, and it is the counterpart of the CoreAudio
        render callback: the same short read handling, the same clock
        bookkeeping, the same refusal to raise. What it has that the callback
        does not is somewhere to put an error, so a card that has genuinely
        gone away ends the loop instead of spinning on it.
        """
        alsa = _libs["asound"]
        ring, clock = self._ring, self._clock
        handle, address = self._handle, self._address
        frame_bytes = self.frame_bytes
        wanted = self.period * frame_bytes
        while not self._stop.is_set():
            moved = ring.read_into(address, wanted)
            if moved < wanted:
                ctypes.memset(address + moved, 0, wanted - moved)
                clock.silent_frames += (wanted - moved) // frame_bytes
                clock.underruns += 1
            done = 0
            while done < self.period and not self._stop.is_set():
                count = alsa.snd_pcm_writei(
                    handle, address + done * frame_bytes, self.period - done)
                if count < 0:
                    # -EPIPE is an underrun and -ESTRPIPE is a machine that
                    # was suspended; snd_pcm_recover knows both and puts the
                    # stream back. Anything it does not know ends the loop.
                    if count == -errno.EAGAIN:
                        continue
                    recovered = alsa.snd_pcm_recover(handle, count, 1)
                    if recovered < 0:
                        self.failure = AudioUnavailable(
                            error_message("writing to the ALSA device",
                                          recovered))
                        self._stop.set()
                        return
                    if count == -errno.EPIPE or count == -_ESTRPIPE:
                        clock.underruns += 1
                    continue
                done += count
            clock.frames += done

    # -- running -----------------------------------------------------------

    def start(self, ring, clock):
        if ring.frame_bytes != self.frame_bytes:
            raise AudioUnavailable(
                "the ring holds %d-byte frames and the device wants %d"
                % (ring.frame_bytes, self.frame_bytes))
        with self._lock:
            if self._thread is not None:
                return
            if self._handle is None:
                raise AudioUnavailable("this ALSA device is closed")
            self._ring = ring
            self._clock = clock
            status = _libs["asound"].snd_pcm_prepare(self._handle)
            if status < 0:
                raise AudioUnavailable(
                    error_message("preparing the ALSA device", status))
            self._stop.clear()
            self._thread = threading.Thread(target=self._run,
                                            name="heel-alsa", daemon=True)
            self._thread.start()

    def stop(self):
        """Stop writing and wait for the thread to be out of libasound.

        ``snd_pcm_drop`` is what unblocks a ``snd_pcm_writei`` that is waiting
        for a card with nowhere to put the frames -- which is the normal state
        of a healthy card -- so the flag is set, the thread is given a period
        or two to notice, and only a thread that did not notice gets dropped
        out from under. Joining afterwards is what makes :meth:`close` safe.
        """
        with self._lock:
            thread, self._thread = self._thread, None
        self._stop.set()
        if thread is None or thread is threading.current_thread():
            return
        thread.join(timeout=max(0.25, self.latency * 4))
        if thread.is_alive() and self._handle:
            _libs["asound"].snd_pcm_drop(self._handle)
            thread.join(timeout=1.0)

    def close(self):
        """Stop, then let go. In that order, and only that order.

        The thread is joined *before* the handle and the buffer it writes from
        are dropped. Closing a PCM another thread is inside is not an
        exception, it is a use of freed memory in somebody else's library.
        """
        self.stop()
        with self._lock:
            handle, self._handle = self._handle, None
            if handle:
                _libs["asound"].snd_pcm_drop(handle)
                _libs["asound"].snd_pcm_close(handle)
            self._buffer = None
            self._address = 0
            self._ring = None
            self._clock = None

    def __repr__(self):
        return ("<alsa.Device %r %d Hz x%d %s, latency %.1f ms%s>"
                % (self.device, self.rate, self.channels, self.fmt,
                   self.latency * 1000,
                   ", ALSA resampling" if self.resampled else ""))


def available():
    """True when an ALSA output can actually be opened here."""
    global _problem
    _problem = ""
    if sys.platform == "darwin" or sys.platform == "win32":
        return False
    try:
        device = Device()
    except (AudioUnavailable, OSError) as exc:
        # A missing libasound means this is not a machine with ALSA on it,
        # which is no more worth reporting than CoreAudio being absent from
        # Linux. A libasound that loads and then cannot open a PCM -- no
        # /dev/snd, no card, somebody else holding it -- is the entire story,
        # so that one is kept for whoever wants to print it.
        _problem = str(exc) if _libs else ""
        return False
    device.close()
    return True


def unavailable_reason():
    """Why available() last said no, or "" when there is nothing to say."""
    return _problem
