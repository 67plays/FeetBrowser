"""Audio output: the heel, because it is the part of a foot that makes a noise.

Everything downstream of "here are some PCM samples" lives here. A decoder
hands frames to a :class:`Source`; the source resamples them to whatever rate
the hardware actually runs at; a mixer sums the sources, applies per-source
gain and one master volume, and writes interleaved device-format frames into
a :class:`Ring`; and a platform device empties that ring in real time. The
number of frames the device has taken out is the audio clock, and it is the
only clock a player should ever synchronise video against.

The four layers, and why they are four:

  * ``coreaudio.py``, ``alsa.py`` and ``winmm.py``: **the hardware**. One
    module per platform, ctypes against the system library, the same shape as
    ``cocoa.py``, ``x11.py`` and ``win32.py``. Each one knows how to take
    bytes out of a ring and nothing else at all -- no mixing, no gain, no
    format conversion, no knowledge that a decoder exists.
  * :class:`Ring`: **the seam between a Python thread and a realtime one**. A
    single-producer, single-consumer byte ring over one preallocated buffer,
    with no lock in it, because a lock is the one thing an audio callback
    must never wait on. See "Rules for the realtime side" below.
  * :class:`Resampler`: **rate matching**. A polyphase windowed-sinc filter.
    Device rates and stream rates disagree constantly -- 44100 against 48000
    is the ordinary case -- and the cheap answers to that (drop a sample,
    repeat a sample, interpolate linearly) are all audible.
  * :class:`Mixer` and :class:`Source`: **several sounds at once**. Summed in
    floating point, clamped once at the end.

Nothing above this module has to know which of those is running.
:func:`open_output` returns an :class:`Output` on every machine, including one
with no sound card
at all, where the device is a clock that consumes samples in real time and
throws them away. That is not a stub for testing: a browser on a headless box
still has to keep a video playing at the right speed, and a paced silent
device does exactly that. ``available()`` and ``unavailable_reason()`` say
which one you got, in the same terms ``h264.py`` uses, and the answer is
worked out once rather than per frame.

Rules for the realtime side
---------------------------

On macOS the device callback runs on a CoreAudio thread with a hard deadline
-- a few milliseconds -- and no tolerance at all for being late. Everything
that touches that thread obeys these, and every one of them is a rule about
*not* doing something:

 1. **The callback never blocks on a lock.** The ring is lock-free: the
    producer alone writes ``_write``, the consumer alone writes ``_read``,
    and each only reads the other's. Both are plain integer attributes, whose
    stores are atomic. A mutex here would make the audio thread wait on a
    Python thread that might be doing anything at all, which is the same as
    having no audio thread.
 2. **The callback never waits for data.** An empty ring is filled with
    silence and counted, not retried. Underrun is a statistic, not an error
    path.
 3. **The callback never raises.** Its whole body is wrapped, and a failure
    is recorded in an attribute for the main thread to notice. An exception
    escaping into a C callback prints a traceback from the realtime thread,
    which turns one glitch into a second of them.
 4. **The callback allocates nothing that can grow.** No list, no dict, no
    bytes object, no string formatting, no ctypes structure construction, no
    method it did not already have a reference to. The payload moves with
    ``memmove`` straight out of the ring's buffer into the one CoreAudio
    handed us; the buffer on both ends existed before the stream started.
    (Integer temporaries are not avoidable in Python and are not worth
    avoiding: CPython takes them off a freelist, which never calls the system
    allocator and never blocks. What matters is that nothing in the callback
    can trigger a large allocation, a resize, or a collection of a container
    the callback built.)
 5. **The callback does no work that belongs to somebody else.** No mixing,
    no gain, no format conversion, no resampling, no logging, no clock
    arithmetic beyond two additions. All of that happens on the mixer thread,
    which has the whole depth of the ring -- 85 ms by default -- as slack.
 6. **Nothing is freed while the device is running.** ``close()`` stops the
    unit and waits for it before releasing the ring, and an ``atexit`` hook
    does the same for anything still open at shutdown, because calling into
    Python from a foreign thread during interpreter finalisation is a crash
    rather than an exception.

Linux and Windows do not need most of that, and get it anyway. Neither ALSA
nor ``waveOut`` requires a callback on a realtime thread: both are driven
from an ordinary Python thread that blocks on the device and pulls from the
same ring. That thread is allowed to be a little slower and a little less
careful, and it is written as if it were not.

What this module does not do
----------------------------

It does not decode anything. It has no opinion about containers, codecs or
`<video>`; a source is a rate, a channel count and a stream of samples. And
it does no A/V synchronisation itself -- it publishes the clock that A/V sync
has to be built on, which is a different job. :meth:`Output.clock` and
:meth:`Source.position` are that seam, and they are duck-compatible with
``media.Clock`` on purpose, so a ``Scheduler`` can be handed one without any
adapter at all.
"""

import array
import atexit
import ctypes
import math
import os
import sys
import threading
import time
from operator import mul

__all__ = ["Ring", "Resampler", "Source", "Mixer", "AudioClock", "Output",
           "NullDevice", "AudioError", "AudioUnavailable", "open_output",
           "available", "unavailable_reason", "close_all", "tone",
           "FLOAT32", "INT16", "DEFAULT_RATE", "DEFAULT_CHANNELS"]


# -- formats ---------------------------------------------------------------
#
# Two, and only two. Float is what CoreAudio wants and what the mixer already
# holds, so on macOS the last conversion in the chain is a copy. 16-bit signed
# is what every ALSA device and every waveOut device on earth accepts, and is
# the format a decoder hands us anyway.
FLOAT32 = "f32"
INT16 = "s16"

SAMPLE_BYTES = {FLOAT32: 4, INT16: 2}

DEFAULT_RATE = 48000
DEFAULT_CHANNELS = 2

# How much audio sits between the mixer and the speaker. 4096 frames is 85 ms
# at 48 kHz: long enough that a Python thread descheduled for 50 ms does not
# produce a hole, short enough that a pause takes effect while the user still
# thinks they caused it. It is also the number `Source.position` has to
# subtract to answer "what is being heard right now", so it is not free.
RING_FRAMES = 4096

# How often the mixer thread tops the ring up. Two milliseconds is far more
# often than it needs to be woken; the cost is a sleeping thread and the
# benefit is that a late wakeup still lands inside the ring's slack.
MIX_INTERVAL = 0.002

# Taps per polyphase branch, and the Kaiser window's beta. 64 and 9.0 put the
# stopband at about -90 dB with a transition band of roughly 0.09 of the
# lower of the two sample rates, so with the cutoff at Nyquist the response
# is flat to about 20 kHz on a 44.1 kHz stream and everything that folds back
# lands above it. Fewer taps is cheaper and audibly worse near the top of the
# band; see `tests/test_audio.py`, which measures both.
TAPS = 64
KAISER_BETA = 9.0

# The largest number of polyphase branches we will build a table for. Every
# rate pair anyone actually meets reduces to far less than this (44100 to
# 48000 is 160; 11025 to 48000 is 640), and a pair that does not is
# approximated by the nearest ratio that does -- a rate error of under a part
# in a million, against a table that would otherwise be tens of megabytes.
MAX_PHASES = 1024


class AudioError(RuntimeError):
    """Something went wrong in the audio stack."""


class AudioUnavailable(AudioError):
    """Raised by a platform backend that cannot open a device here."""


# -- the ring --------------------------------------------------------------

class Ring:
    """A lock-free single-producer, single-consumer ring of whole frames.

    One preallocated ctypes buffer, two monotonically increasing byte
    counters, and no lock. ``_write`` is written only by the producer and
    ``_read`` only by the consumer, so neither ever sees a torn value of its
    own, and a stale read of the other's counter is always stale in the safe
    direction: the producer can only underestimate the free space and the
    consumer can only underestimate the data available. Both then do less
    than they could have, and the next call does the rest.

    The counters never wrap. They are Python integers, so they cannot, and at
    48 kHz stereo it would take a bit over half a million years to reach the
    point where that stopped being a rhetorical claim.

    The buffer is ctypes-backed so that :meth:`read_into` can be a
    ``memmove`` into a buffer the operating system handed us, with nothing
    allocated in between -- see rule 4 in the module docstring.
    """

    __slots__ = ("frame_bytes", "capacity", "_size", "_buf", "_view",
                 "_base", "_write", "_read")

    def __init__(self, frame_bytes, capacity):
        if frame_bytes <= 0 or capacity <= 0:
            raise ValueError("a ring needs a positive frame size and depth")
        self.frame_bytes = int(frame_bytes)
        self.capacity = int(capacity)           # frames
        self._size = self.frame_bytes * self.capacity
        self._buf = ctypes.create_string_buffer(self._size)
        self._view = memoryview(self._buf).cast("B")
        self._base = ctypes.addressof(self._buf)
        self._write = 0
        self._read = 0

    # -- what each side can see --------------------------------------------

    @property
    def written_frames(self):
        """Frames the producer has put in, ever. Monotonic."""
        return self._write // self.frame_bytes

    @property
    def read_frames(self):
        """Frames the consumer has taken out, ever. Monotonic."""
        return self._read // self.frame_bytes

    @property
    def backlog(self):
        """Frames sitting in the ring: written but not yet consumed."""
        return (self._write - self._read) // self.frame_bytes

    @property
    def space(self):
        """Frames the producer could write right now without blocking."""
        return self.capacity - self.backlog

    # -- producer side -----------------------------------------------------

    def write(self, data):
        """Copy as much of ``data`` as fits. Returns the bytes taken.

        Short writes are the normal case rather than an error: the ring is
        the flow control, and a producer that finds it full is a producer
        that is ahead, which is where it is supposed to be.
        """
        available = self._size - (self._write - self._read)
        count = len(data)
        if count > available:
            count = available - available % self.frame_bytes
        if count <= 0:
            return 0
        offset = self._write % self._size
        first = self._size - offset
        if first > count:
            first = count
        self._view[offset:offset + first] = data[:first]
        if count > first:
            self._view[0:count - first] = data[first:count]
        self._write += count
        return count

    def write_frames(self, data):
        """:meth:`write`, counted in frames."""
        return self.write(data) // self.frame_bytes

    # -- consumer side -----------------------------------------------------

    def read(self, count):
        """Take up to ``count`` bytes out, as a ``bytes``. Whole frames only.

        This one allocates, so it is for the backends driven from an ordinary
        Python thread. The realtime path uses :meth:`read_into`.
        """
        available = self._write - self._read
        if count > available:
            count = available
        count -= count % self.frame_bytes
        if count <= 0:
            return b""
        offset = self._read % self._size
        first = self._size - offset
        if first >= count:
            out = bytes(self._view[offset:offset + count])
        else:
            out = (bytes(self._view[offset:self._size])
                   + bytes(self._view[0:count - first]))
        self._read += count
        return out

    def read_into(self, address, count):
        """Move up to ``count`` bytes to ``address``. Returns bytes moved.

        The realtime path. Two ``memmove`` calls at the very worst, nothing
        allocated, nothing that can raise on a well-formed argument, and no
        reference to anything this call created.
        """
        available = self._write - self._read
        if count > available:
            count = available
        count -= count % self.frame_bytes
        if count <= 0:
            return 0
        offset = self._read % self._size
        first = self._size - offset
        if first > count:
            first = count
        ctypes.memmove(address, self._base + offset, first)
        if count > first:
            ctypes.memmove(address + first, self._base, count - first)
        self._read += count
        return count

    def discard(self, count):
        """Drop up to ``count`` bytes without copying them anywhere."""
        available = self._write - self._read
        if count > available:
            count = available
        count -= count % self.frame_bytes
        if count > 0:
            self._read += count
        return count

    def clear(self):
        """Throw away everything queued. Producer side; used on a seek."""
        self._read = self._write


# -- filter design ---------------------------------------------------------
#
# Pure arithmetic over plain numbers, so every one of these is testable with
# no device, no thread and no audio anywhere near it.

def bessel_i0(x):
    """The modified Bessel function of the first kind, order zero.

    By its power series, which converges quickly for the arguments a Kaiser
    window asks for (beta is single digits) and is exact enough long before
    the loop guard matters.
    """
    total = 1.0
    term = 1.0
    half = x / 2.0
    k = 1
    while k < 200:
        term *= (half / k) * (half / k)
        total += term
        if term < total * 1e-18:
            break
        k += 1
    return total


def kaiser_window(length, beta):
    """A Kaiser window of ``length`` points.

    Kaiser rather than Hann or Blackman because beta is a single knob that
    trades stopband depth against transition width continuously, which is
    exactly the trade a resampler wants to be able to make.
    """
    if length <= 1:
        return [1.0] * max(0, length)
    half = (length - 1) / 2.0
    denominator = bessel_i0(beta)
    window = []
    for i in range(length):
        ratio = (i - half) / half
        inner = 1.0 - ratio * ratio
        window.append(bessel_i0(beta * math.sqrt(inner if inner > 0.0 else 0.0))
                      / denominator)
    return window


def sinc(x):
    """``sin(pi x) / (pi x)``, and 1 at zero rather than a ZeroDivisionError."""
    if x == 0.0:
        return 1.0
    px = math.pi * x
    return math.sin(px) / px


def lowpass(length, cutoff, beta):
    """A windowed-sinc low-pass of ``length`` taps.

    ``cutoff`` is in cycles per sample, so 0.5 is Nyquist. The impulse
    response of an ideal low-pass at that cutoff is ``2 * cutoff *
    sinc(2 * cutoff * n)``, centred, and the window is what makes it finite
    without ringing the stopband up to -20 dB.
    """
    window = kaiser_window(length, beta)
    half = (length - 1) / 2.0
    twice = 2.0 * cutoff
    return [twice * sinc(twice * (i - half)) * window[i]
            for i in range(length)]


def _closer(trial, best, numerator, denominator):
    """Is ``trial`` a better approximation of ``numerator/denominator``?

    Compared as integers -- ``|t0*d - n*t1| / t1`` against the same for
    ``best`` -- because the two candidates can agree to more decimal places
    than a float has, and then the float comparison picks whichever way the
    rounding fell rather than the closer one.
    """
    if best is None:
        return True
    return (abs(trial[0] * denominator - numerator * trial[1]) * best[1]
            < abs(best[0] * denominator - numerator * best[1]) * trial[1])


def best_ratio(numerator, denominator, limit):
    """``(n, d)`` closest to ``numerator/denominator`` with ``n <= limit``.

    Continued fractions. Only reached for rate pairs that do not reduce --
    every ordinary one does, and returns unchanged.

    The seeds are the standard ones and the order of them is not arbitrary:
    the convergents are ``p[k] = q[k]*p[k-1] + p[k-2]`` from ``p[-1] = 1`` and
    ``p[-2] = 0``, and swapping those two seeds gives the reciprocal of the
    answer -- which resamples 48 kHz to 40.5 rather than to 44.1 and sounds
    exactly like a tape running slow.

    Stopping at the last convergent that fits is not quite the best answer,
    and the gap is worth the four lines it costs to close. When the next
    convergent overshoots ``limit``, some *semiconvergent* below it -- the
    same recurrence with the quotient reduced -- may still fit and be closer
    than anything before it. 11025 Hz into 32 kHz is the case that shows it:
    the last convergent is 119/41, off by 1.9e-5, where 923/318 also fits
    under 1024 and is off by 7.4e-6. That is 69 ms of drift an hour against
    27, which nobody hears as pitch but which a long stream accumulates.
    """
    if numerator <= limit:
        return numerator, denominator
    a, b = numerator, denominator
    previous = (0, 1)               # p[-2] / q[-2]
    current = (1, 0)                # p[-1] / q[-1]
    best = None
    while b:
        quotient = a // b
        convergent = (quotient * current[0] + previous[0],
                      quotient * current[1] + previous[1])
        if convergent[0] > limit or convergent[1] <= 0:
            # The full step does not fit. The largest partial one that does
            # is the best remaining candidate; take it if it beats what we
            # already have, and stop either way.
            if current[0] > 0 and current[1] > 0:
                room = (limit - previous[0]) // current[0]
                if room > 0:
                    trial = (room * current[0] + previous[0],
                             room * current[1] + previous[1])
                    if trial[1] > 0 and _closer(trial, best, numerator,
                                                denominator):
                        best = trial
            break
        previous, current = current, convergent
        best = convergent
        a, b = b, a - quotient * b
    if best is None:
        # The very first convergent is already too big, which needs a ratio
        # steeper than the limit itself. No pair of audio rates is.
        return limit, max(1, round(limit * denominator / numerator))
    return best


def phase_table(up, down, taps=TAPS, beta=KAISER_BETA):
    """The polyphase branches for resampling by ``up/down``.

    The construction, because it is worth being able to check by eye. Upsample
    by ``up`` (stuff ``up - 1`` zeros between input samples), low-pass at the
    lower of the two Nyquist frequencies, keep every ``down``-th sample. The
    zeros mean that for output ``n`` only the taps at ``j == n * down (mod
    up)`` multiply a nonzero input, so the one long filter decomposes into
    ``up`` short ones -- branch ``p`` being ``h[p], h[p + up], h[p + 2 up]``
    and so on -- and no multiplication by a stuffed zero is ever performed.

    Each branch is normalised to sum to one. At DC every input sample is the
    same, so a branch's sum *is* its gain, and branches whose sums differ by
    even a part in a thousand modulate the signal at the rate the branches
    cycle, which is a tone rather than a hiss. Returned reversed, because the
    input samples a branch wants run backwards from the newest and a forward
    slice is worth an awful lot in Python.
    """
    # Cutoff is measured at the *upsampled* rate, and has to sit at whichever
    # of the two Nyquist frequencies is lower: too high when downsampling and
    # the discarded band folds straight back in.
    cutoff = 0.5 / (up if up > down else down)
    prototype = lowpass(up * taps, cutoff, beta)
    branches = []
    for p in range(up):
        coefficients = prototype[p::up]
        total = math.fsum(coefficients)
        if total:
            coefficients = [c / total for c in coefficients]
        coefficients.reverse()
        branches.append(coefficients)
    return branches


_TABLES = {}
_TABLES_LOCK = threading.Lock()


def cached_phase_table(up, down, taps=TAPS, beta=KAISER_BETA):
    """:func:`phase_table`, built once per distinct rate pair.

    Two ``<video>`` elements at 44.1 kHz should not each spend a tenth of a
    second designing the same filter, and the table is read-only once built.
    """
    key = (up, down, taps, beta)
    table = _TABLES.get(key)
    if table is None:
        with _TABLES_LOCK:
            table = _TABLES.get(key)
            if table is None:
                table = phase_table(up, down, taps, beta)
                _TABLES[key] = table
    return table


# -- the resampler ---------------------------------------------------------

class Resampler:
    """Streaming polyphase resampling of interleaved float frames.

    Feed it any number of frames at ``in_rate`` and it returns whatever
    frames at ``out_rate`` are complete. State carries across calls, so
    ``process(a) + process(b)`` is sample-for-sample what ``process(a + b)``
    would have been -- which is the only property that makes it usable
    against a decoder that hands over whatever a packet happened to contain.

    Equal rates are a genuine passthrough rather than a filter that happens
    to be nearly flat: no delay, no rounding, no cost.
    """

    def __init__(self, in_rate, out_rate, channels=1, taps=TAPS,
                 beta=KAISER_BETA):
        if in_rate <= 0 or out_rate <= 0:
            raise ValueError("sample rates are positive")
        if channels <= 0:
            raise ValueError("a stream has at least one channel")
        self.in_rate = int(in_rate)
        self.out_rate = int(out_rate)
        self.channels = int(channels)
        self.taps = int(taps)
        self.passthrough = self.in_rate == self.out_rate
        if self.passthrough:
            self.up = self.down = 1
            self._branches = None
            self.delay = 0.0
            return
        divisor = math.gcd(self.out_rate, self.in_rate)
        self.up, self.down = best_ratio(self.out_rate // divisor,
                                        self.in_rate // divisor, MAX_PHASES)
        self._branches = cached_phase_table(self.up, self.down, taps, beta)
        # Group delay of the prototype, in output frames. The filter is
        # linear phase, so this is the whole of it, and it is what a caller
        # has to subtract to line the output up with the input in time.
        self.delay = (self.up * self.taps - 1) / 2.0 / self.down
        # One history per channel, primed with the taps-1 zeros the first
        # output needs. Global input index 0 is the first real sample, so the
        # priming samples sit at negative indices and `_base` starts negative.
        self._history = [[0.0] * (self.taps - 1) for _ in range(self.channels)]
        self._base = 1 - self.taps
        self._phase = 0        # n * down mod up
        self._cursor = 0       # floor(n * down / up), the newest input needed

    def reset(self):
        """Forget the tail. For a seek, where continuity is a lie anyway."""
        if self.passthrough:
            return
        self._history = [[0.0] * (self.taps - 1) for _ in range(self.channels)]
        self._base = 1 - self.taps
        self._phase = 0
        self._cursor = 0

    def pending(self):
        """Input frames held back waiting for the rest of a filter window."""
        if self.passthrough:
            return 0
        return len(self._history[0]) + self._base

    def process(self, frames):
        """Resample interleaved floats. Returns interleaved floats.

        ``frames`` may be a list, an ``array`` or anything else indexable and
        sliceable; the return is always a list, because that is what the
        mixer sums and what packing wants.
        """
        if self.passthrough:
            return list(frames)
        channels = self.channels
        history = self._history
        if channels == 1:
            history[0].extend(frames)
        else:
            for c in range(channels):
                history[c].extend(frames[c::channels])
        limit = self._base + len(history[0])
        # Every channel is filtered from the same starting phase and cursor,
        # because the phase, the cursor and the base describe the *stream*
        # and not a channel. Running them from one saved state and committing
        # the end state once is what keeps them from drifting apart, which
        # would be a stereo image that slowly rotates.
        columns = [self._filter(history[c], limit) for c in range(channels)]
        phase, cursor = self._advance(len(columns[0]))
        self._phase, self._cursor = phase, cursor
        drop = cursor - self.taps + 1 - self._base
        if drop > 0:
            for column in history:
                del column[:drop]
            self._base += drop
        if channels == 1:
            return columns[0]
        count = len(columns[0])
        out = [0.0] * (count * channels)
        for c, column in enumerate(columns):
            out[c::channels] = column
        return out

    def _filter(self, history, limit):
        """One channel, from the saved phase and cursor. Leaves both alone."""
        branches = self._branches
        taps = self.taps
        up, down = self.up, self.down
        base = self._base
        phase, cursor = self._phase, self._cursor
        out = []
        append = out.append
        while cursor < limit:
            start = cursor - taps + 1 - base
            append(sum(map(mul, branches[phase], history[start:start + taps])))
            phase += down
            if phase >= up:
                cursor += phase // up
                phase %= up
        return out

    def _advance(self, count):
        """Where the phase and cursor land after ``count`` outputs.

        Each output adds ``down`` to the phase and carries into the cursor,
        so ``count`` of them is one division rather than a loop.
        """
        total = self._phase + count * self.down
        return total % self.up, self._cursor + total // self.up


# -- sample formats --------------------------------------------------------

_INT16_SCALE = 32768.0
_INT16_MAX = 32767


def floats_from_int16(data):
    """Signed 16-bit little-endian PCM to floats in [-1, 1).

    Divided by 32768 rather than 32767, so that the full negative excursion
    maps to exactly -1.0 and the conversion is the exact inverse of the one
    below for every value it can round-trip.
    """
    values = array.array("h")
    values.frombytes(bytes(data))
    if sys.byteorder != "little":
        values.byteswap()
    return [v / _INT16_SCALE for v in values]


def floats_from_float32(data):
    """32-bit float PCM to a list of Python floats."""
    values = array.array("f")
    values.frombytes(bytes(data))
    if sys.byteorder != "little":
        values.byteswap()
    return list(values)


def pack(values, fmt):
    """Floats to device bytes, clamped once, on the way out.

    Clamping matters and is done here rather than per source: two sources at
    full scale sum to twice full scale, and what a device does with a float
    above 1.0 ranges from clipping it politely to wrapping it into a loud
    click.
    """
    if fmt == FLOAT32:
        out = array.array("f", [-1.0 if v < -1.0 else (1.0 if v > 1.0 else v)
                                for v in values])
    elif fmt == INT16:
        out = array.array("h", [_to_int16(v) for v in values])
    else:
        raise ValueError("unknown sample format %r" % (fmt,))
    if sys.byteorder != "little":
        out.byteswap()
    return out.tobytes()


def _to_int16(value):
    scaled = int(value * _INT16_SCALE)
    if scaled > _INT16_MAX:
        return _INT16_MAX
    if scaled < -_INT16_SCALE:
        return -32768
    return scaled


def silence(frames, channels, fmt):
    """``frames`` frames of nothing, in device bytes."""
    return bytes(frames * channels * SAMPLE_BYTES[fmt])


def remap_channels(frames, source_channels, target_channels):
    """Interleaved floats from one channel count to another.

    Mono to stereo duplicates, which is what every player does. Stereo to
    mono averages rather than dropping a channel, because dropping one loses
    anything panned to the other entirely. More channels than we have simply
    keeps the first few, which is wrong for 5.1 and honest about it.
    """
    if source_channels == target_channels:
        return list(frames)
    if source_channels == 1:
        out = [0.0] * (len(frames) * target_channels)
        for c in range(target_channels):
            out[c::target_channels] = frames
        return out
    if target_channels == 1:
        count = len(frames) // source_channels
        scale = 1.0 / source_channels
        out = [0.0] * count
        for c in range(source_channels):
            column = frames[c::source_channels]
            for i in range(count):
                out[i] += column[i] * scale
        return out
    count = len(frames) // source_channels
    out = [0.0] * (count * target_channels)
    for c in range(target_channels):
        out[c::target_channels] = (frames[c::source_channels] if
                                   c < source_channels else [0.0] * count)
    return out


# -- the clock -------------------------------------------------------------

class AudioClock:
    """Frames the device has actually consumed, and nothing else.

    This is the clock, and the direction of the dependency is the whole
    point: **video follows audio, never the reverse.** A dropped video frame
    costs one frame of one picture and most people will not see it. A gap in
    audio is a click, and everybody hears every one of them. So the audio
    device is allowed to run at whatever rate its crystal actually runs at,
    and the picture is scheduled against that rather than against
    ``time.monotonic``, which is a different crystal and will disagree by
    tens of milliseconds an hour.

    ``now()`` is seconds, monotonic, and is duck-compatible with
    ``media.Clock`` so that a ``Scheduler`` can be handed one directly.

    Two counters, because they answer two different questions. ``frames`` is
    how much the hardware has taken, silence included, which is real time as
    the device measures it. ``silent_frames`` is how much of that was silence
    we invented because the mixer was late, which is the number that says
    whether the machine is keeping up.
    """

    __slots__ = ("rate", "frames", "silent_frames", "underruns", "started")

    def __init__(self, rate):
        self.rate = int(rate)
        self.frames = 0
        self.silent_frames = 0
        self.underruns = 0
        self.started = False

    def now(self):
        """Seconds of audio the device has consumed. Monotonic."""
        return self.frames / self.rate

    def seconds(self):
        """An alias, for callers who find ``now()`` too Tk-shaped."""
        return self.now()

    def reset(self):
        self.frames = 0
        self.silent_frames = 0
        self.underruns = 0

    def __repr__(self):
        return ("AudioClock(%.3fs, %d frames, %d silent, %d underruns)"
                % (self.now(), self.frames, self.silent_frames,
                   self.underruns))


# -- sources and the mixer -------------------------------------------------

class Source:
    """One stream of PCM on its way to the speaker.

    Written to from whichever thread decoded the samples, read from by the
    mixer thread. The resampling and the channel mapping happen on the
    *writing* side, on purpose: the decoder thread has a whole packet's worth
    of slack and the mixer thread has a couple of milliseconds, so the
    expensive half of the work belongs to the one that can afford it. What
    the mixer does per frame is a multiply and an add.

    A source that is written to faster than it is played drops the excess
    rather than growing without bound, and counts what it dropped. A browser
    playing a file it has entirely in memory can otherwise turn a two-hour
    podcast into two hours of decoded float samples on the heap.
    """

    def __init__(self, output, rate, channels=1, gain=1.0, name="",
                 max_queue=2.0, taps=TAPS):
        self.output = output
        self.rate = int(rate)
        self.channels = int(channels)
        self.gain = float(gain)
        self.name = name or "source"
        self.max_queue = float(max_queue)
        self.dropped = 0
        self.ended = False
        self._lock = threading.Lock()
        self._queue = []                # device-rate, device-channel floats
        self._pulled = 0                # device frames handed to the mixer
        self._written = 0               # source frames accepted, ever
        self._closed = False
        # Where this source's timeline began. Both move only in restart(),
        # and their initial values are the identity, so a source nobody
        # seeks reports exactly what it reported before they existed.
        self._origin_pulled = 0         # _pulled when the timeline started
        self._origin_at = 0.0           # the position it started at
        self._resampler = Resampler(self.rate, output.rate, self.channels,
                                    taps=taps)

    # -- the producing side ------------------------------------------------

    def write(self, data, fmt=INT16):
        """Queue PCM. Returns the source frames accepted.

        ``fmt`` is :data:`INT16` or :data:`FLOAT32` for raw bytes, or the
        string ``"float"`` for a sequence of Python floats already in
        [-1, 1], which is what a test generating a tone has.
        """
        if self._closed:
            return 0
        if fmt == "float":
            samples = list(data)
        elif fmt == INT16:
            samples = floats_from_int16(data)
        elif fmt == FLOAT32:
            samples = floats_from_float32(data)
        else:
            raise ValueError("unknown sample format %r" % (fmt,))
        if not samples:
            return 0
        count = len(samples) // self.channels
        samples = samples[:count * self.channels]
        resampled = self._resampler.process(samples)
        mapped = remap_channels(resampled, self.channels,
                                self.output.channels)
        limit = int(self.max_queue * self.output.rate) * self.output.channels
        with self._lock:
            room = limit - len(self._queue)
            if room < len(mapped):
                room -= room % self.output.channels
                if room < 0:
                    room = 0
                self.dropped += (len(mapped) - room) // self.output.channels
                mapped = mapped[:room]
            self._queue.extend(mapped)
            self._written += count
        return count

    def queued_seconds(self):
        """How much sound is waiting. What a decoder throttles against."""
        with self._lock:
            return (len(self._queue) / self.output.channels
                    / self.output.rate)

    def close(self):
        """No more samples. Whatever is queued still plays out."""
        self._closed = True

    def clear(self):
        """Throw the queue away, and leave the timeline where it is."""
        with self._lock:
            del self._queue[:]
        self._resampler.reset()

    def restart(self, at=0.0):
        """Throw the queue away and begin a new timeline at ``at`` seconds.

        This is what a seek is. A seek is not a gap in one stream, it is a
        different stream through the same speaker, and ``position()`` has to
        say so or every picture scheduled against it lands in the wrong
        place.

        The care is in the second line. What is *queued* is dropped, but what
        has already been handed to the mixer cannot be: it is in the ring,
        possibly in the device's own buffer, and it is going to be heard.
        Remembering how much of it there was is what makes the new timeline
        begin when the last of the old sound comes out of the speaker rather
        than a ring's depth before it, which would be a permanent offset
        between the sound and the picture for as long as the file played.
        """
        with self._lock:
            del self._queue[:]
            self._origin_pulled = self._pulled
            self._origin_at = float(at)
            self.ended = False
        self._resampler.reset()

    # -- the mixing side ---------------------------------------------------

    def _take(self, frames):
        """Up to ``frames`` device frames of interleaved floats, or ``[]``."""
        want = frames * self.output.channels
        with self._lock:
            if not self._queue:
                if self._closed:
                    self.ended = True
                return ()
            if len(self._queue) <= want:
                chunk = self._queue
                self._queue = []
            else:
                chunk = self._queue[:want]
                del self._queue[:want]
        self._pulled += len(chunk) // self.output.channels
        return chunk

    # -- where it is up to -------------------------------------------------

    def position(self):
        """Seconds of this source that have actually been heard.

        Not what has been decoded and not what has been mixed: what has left
        the ring. The mixer runs a ring's depth ahead of the speaker, so the
        frames pulled from this source include everything still queued in it,
        and that has to come back off. Device latency comes off too, where a
        backend can tell us what it is.

        This is the number a `<video>` schedules its pictures against, and
        the reason it is a method on the source rather than on the clock is
        that a stream that starts late, stops, or is seeked has its own
        timeline and the device's does not move with it. :meth:`restart` is
        where that timeline is moved; until it is called this is seconds
        since the source was made.
        """
        heard = self._pulled - self.output.ring.backlog - self._origin_pulled
        if heard < 0:
            # Sound from before the last restart() is still being heard.
            heard = 0
        seconds = heard / self.output.rate - self.output.latency
        return self._origin_at + (seconds if seconds > 0.0 else 0.0)

    def __repr__(self):
        return ("<Source %s %d Hz x%d gain=%.2f queued=%.3fs>"
                % (self.name, self.rate, self.channels, self.gain,
                   self.queued_seconds()))


class Mixer:
    """Sums the sources, applies gain and the master volume, packs bytes.

    Floating point throughout and clamped exactly once, at the end. Clamping
    per source would make a quiet sound change a loud one's shape, and
    clamping in integers at every step is how a mix acquires a crunch that
    nobody can find the source of.

    Nothing here is threaded and nothing here blocks, so a test can call
    :meth:`render` directly and assert on the bytes.
    """

    def __init__(self, rate, channels, fmt=FLOAT32):
        self.rate = int(rate)
        self.channels = int(channels)
        self.fmt = fmt
        self.volume = 1.0
        self.sources = []
        self._lock = threading.Lock()

    def add(self, source):
        with self._lock:
            self.sources.append(source)
        return source

    def remove(self, source):
        with self._lock:
            if source in self.sources:
                self.sources.remove(source)

    def render(self, frames):
        """``frames`` frames of mixed audio, as device bytes."""
        return pack(self.render_floats(frames), self.fmt)

    def render_floats(self, frames):
        """The same mix, before packing. Exactly ``frames`` frames long."""
        width = frames * self.channels
        with self._lock:
            sources = list(self.sources)
        master = self.volume
        total = None
        for source in sources:
            chunk = source._take(frames)
            if not chunk:
                continue
            gain = source.gain * master
            if total is None:
                # The single-source case, which is nearly every case, never
                # touches an accumulator: one list comprehension in C and out.
                total = (list(chunk) if gain == 1.0
                         else [value * gain for value in chunk])
                if len(total) < width:
                    total.extend([0.0] * (width - len(total)))
            elif gain == 1.0:
                for i, value in enumerate(chunk):
                    total[i] += value
            else:
                for i, value in enumerate(chunk):
                    total[i] += value * gain
        if total is None:
            return [0.0] * width
        return total


# -- devices ---------------------------------------------------------------

class NullDevice:
    """A device with no hardware behind it that still keeps time.

    It consumes from the ring at exactly the rate the format says it should,
    against ``time.monotonic``, and throws the bytes away. That makes it two
    useful things at once. In CI it is the whole pipeline under test with no
    sound card in the machine. On a headless box, in a container with no
    ``/dev/snd``, or on a laptop whose only output device just went away, it
    is what keeps a video playing at the right speed with the audio clock
    still advancing -- silent, but not broken, and not a special case
    anywhere above here.

    ``paced=False`` consumes as fast as it is pumped instead, which is what a
    test wants when it does not care to spend real seconds.
    """

    name = "null"
    latency = 0.0

    def __init__(self, rate=DEFAULT_RATE, channels=DEFAULT_CHANNELS,
                 fmt=FLOAT32, paced=True, block=512):
        self.rate = int(rate)
        self.channels = int(channels)
        self.fmt = fmt
        self.paced = paced
        self.block = int(block)
        self.frame_bytes = self.channels * SAMPLE_BYTES[fmt]
        self.failure = None
        self._ring = None
        self._clock = None
        self._thread = None
        self._stop = threading.Event()

    def start(self, ring, clock):
        self._ring = ring
        self._clock = clock
        if not self.paced:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="heel-null",
                                        daemon=True)
        self._thread.start()

    def _run(self):
        ring, clock = self._ring, self._clock
        rate = float(self.rate)
        started = time.monotonic()
        consumed = 0
        while not self._stop.is_set():
            due = int((time.monotonic() - started) * rate) - consumed
            if due > 0:
                taken = ring.discard(due * ring.frame_bytes)
                took = taken // ring.frame_bytes
                clock.frames += due
                if took < due:
                    clock.silent_frames += due - took
                    clock.underruns += 1
                consumed += due
            self._stop.wait(self.block / rate)

    def pump(self, frames):
        """Consume ``frames`` immediately. The unpaced path, for tests."""
        taken = self._ring.discard(frames * self._ring.frame_bytes)
        took = taken // self._ring.frame_bytes
        self._clock.frames += frames
        if took < frames:
            self._clock.silent_frames += frames - took
            self._clock.underruns += 1
        return took

    def stop(self):
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=1.0)

    def close(self):
        self.stop()


# -- putting it together ---------------------------------------------------

class Output:
    """A device, a ring, a mixer and the thread that joins them.

    The thread is the producer. It wakes every couple of milliseconds, asks
    the mixer for however many frames the ring has room for, and writes them.
    It is the only thing in the design that is allowed to be late, and the
    depth of the ring is precisely how late it is allowed to be.

    ``threaded=False`` leaves the thread out and requires :meth:`pump`,
    which is the same bargain ``media.VideoPlayer`` offers and for the same
    reason: a test should be a sequence of calls, not a race.
    """

    def __init__(self, device, ring_frames=RING_FRAMES, threaded=True):
        self.device = device
        self.rate = device.rate
        self.channels = device.channels
        self.fmt = device.fmt
        self.latency = getattr(device, "latency", 0.0)
        self.ring = Ring(device.frame_bytes, ring_frames)
        self.clock = AudioClock(self.rate)
        self.mixer = Mixer(self.rate, self.channels, self.fmt)
        self.threaded = threaded
        self.silent = isinstance(device, NullDevice)
        self.reason = ""
        self._thread = None
        self._stop = threading.Event()
        self._started = False
        self._closed = False

    # -- volume, which is the mixer's ---------------------------------------

    @property
    def volume(self):
        """Master volume, 0.0 to 1.0. Applied once, at the mix."""
        return self.mixer.volume

    @volume.setter
    def volume(self, value):
        self.mixer.volume = 0.0 if value < 0.0 else (
            1.0 if value > 1.0 else float(value))

    # -- sources ------------------------------------------------------------

    def add_source(self, rate, channels=1, gain=1.0, name="", **kwargs):
        """A new stream feeding this output."""
        source = Source(self, rate, channels, gain, name, **kwargs)
        self.mixer.add(source)
        return source

    def remove_source(self, source):
        self.mixer.remove(source)

    @property
    def sources(self):
        return list(self.mixer.sources)

    # -- running ------------------------------------------------------------

    def start(self):
        if self._started or self._closed:
            return self
        self._started = True
        self.clock.started = True
        # Prime the ring before the device is told to run. Starting a device
        # against an empty ring means its first callback underruns, which is
        # a click at the head of every sound the browser ever makes.
        self.pump()
        self.device.start(self.ring, self.clock)
        if self.threaded:
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="heel-mix",
                                            daemon=True)
            self._thread.start()
        _register(self)
        return self

    def pump(self, frames=None):
        """Top the ring up. Returns the frames written.

        Called on a timer by the mixer thread, or by hand when there is not
        one. ``frames`` overrides how much to try for; the default is
        whatever the ring has room for, which is the right answer whenever
        the point is not to be here again in a hurry.
        """
        room = self.ring.space if frames is None else min(frames,
                                                          self.ring.space)
        if room <= 0:
            return 0
        return self.ring.write_frames(self.mixer.render(room))

    def _run(self):
        while not self._stop.is_set():
            try:
                self.pump()
            except Exception as exc:            # noqa: BLE001
                # A broken source must not take the audio thread with it, and
                # must not be retried in a tight loop either.
                self.reason = "the mixer failed: %s" % exc
                self._stop.wait(0.05)
                continue
            self._stop.wait(MIX_INTERVAL)

    def stop(self):
        """Stop the device and the mixer thread. Reversible with start()."""
        self._stop.set()
        try:
            self.device.stop()
        except Exception:                       # noqa: BLE001
            pass
        thread, self._thread = self._thread, None
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=1.0)
        self._started = False

    def close(self):
        """Stop, then let go. Ordering matters -- see rule 6.

        The device is stopped and joined *before* anything it might still be
        reading is dropped. A ring released while a realtime callback is
        inside it is not an exception, it is a segmentation fault on a thread
        with no Python frame to blame it on.
        """
        if self._closed:
            return
        self._closed = True
        self.stop()
        try:
            self.device.close()
        except Exception:                       # noqa: BLE001
            pass
        _unregister(self)

    def __enter__(self):
        return self.start()

    def __exit__(self, *_exc):
        self.close()
        return False

    def __repr__(self):
        return ("<Output %s %d Hz x%d %s, %d sources, %s>"
                % (self.device.name, self.rate, self.channels, self.fmt,
                   len(self.mixer.sources), self.clock))


# -- picking a backend -----------------------------------------------------
#
# The same shape h264.py uses: try once, remember the answer, and never make
# a machine that has already said no say it again on the next frame.

_problem = ""
_probed = False
_probe_lock = threading.Lock()
_open_outputs = []
_open_lock = threading.Lock()


def _backend():
    """The module that talks to this platform's hardware, or None."""
    if sys.platform == "darwin":
        from . import coreaudio
        return coreaudio
    if sys.platform == "win32":
        from . import winmm
        return winmm
    if sys.platform.startswith("linux") or sys.platform.startswith("freebsd"):
        from . import alsa
        return alsa
    return None


def _forced():
    """``FEETBROWSER_AUDIO``, which is how CI and a bug report ask for silence.

    ``null`` takes the hardware out of the picture but keeps the clock
    running in real time, which is the interesting half. ``off`` is the same
    thing without the pacing thread, for a machine where even that is more
    than anyone wants.
    """
    return (os.environ.get("FEETBROWSER_AUDIO") or "").strip().lower()


def _make_device(rate, channels, **kwargs):
    """A real device, or raise :class:`AudioUnavailable` saying why not."""
    forced = _forced()
    if forced in ("null", "off", "none", "silent"):
        raise AudioUnavailable("FEETBROWSER_AUDIO=%s asked for no device"
                               % forced)
    module = _backend()
    if module is None:
        raise AudioUnavailable("no audio backend for %s" % sys.platform)
    return module.Device(rate=rate, channels=channels, **kwargs)


def open_output(rate=DEFAULT_RATE, channels=DEFAULT_CHANNELS, backend=None,
                ring_frames=RING_FRAMES, threaded=True, start=True, **kwargs):
    """An :class:`Output`, on any machine, without ever raising.

    This is the entry point, and the contract is deliberately blunt: it
    always returns something you can write samples to. If the hardware is
    there you get it; if it is not, you get a :class:`NullDevice` that
    consumes in real time, ``Output.silent`` is true and ``Output.reason``
    says what happened in a sentence fit to put in front of a user. Callers
    do not branch on the platform and do not have a no-audio code path,
    because a no-audio code path is a code path that is never tested.

    ``backend="null"`` asks for the silent one outright. It is the only name
    this argument takes, and an unrecognised one raises rather than quietly
    handing back the platform's own device: ``backend="alsa"`` on a Mac used
    to return CoreAudio without a word, which is how a test comes to believe
    it has covered a backend it never loaded. Use ``FEETBROWSER_AUDIO`` to
    force silence from outside the process.
    """
    global _problem
    reason = ""
    device = None
    asked_for = backend in ("null", "none")
    if backend is not None and not asked_for and backend != "unpaced":
        raise ValueError(
            "no audio backend is called %r; the platform's own device is "
            "chosen for you, and 'null' is the only name accepted here"
            % (backend,))
    if asked_for:
        reason = "the silent audio backend was asked for by name"
    else:
        try:
            device = _make_device(rate, channels, **kwargs)
        except (AudioError, OSError, ValueError) as exc:
            reason = str(exc)
        except Exception as exc:                # noqa: BLE001
            # A backend that fails in a way we did not think of is still not
            # allowed to take the browser down with it.
            reason = "the %s audio backend failed: %s" % (sys.platform, exc)
    if device is None:
        paced = backend != "unpaced" and _forced() not in ("off", "none")
        device = NullDevice(rate, channels, FLOAT32, paced=paced)
        # Only a device that was tried and failed is worth remembering.
        # Somebody asking for silence on purpose has not learned anything
        # about the hardware, and must not poison what available() reports.
        if reason and not asked_for:
            _problem = reason
    output = Output(device, ring_frames=ring_frames, threaded=threaded)
    output.reason = reason
    if start:
        output.start()
    return output


def available():
    """True when this machine has a sound device we can actually drive.

    Probed once, by opening a device and closing it again, and the answer is
    kept. A machine with no ``/dev/snd`` must not be asked about it once per
    packet.
    """
    global _problem, _probed
    if _probed:
        return not _problem
    with _probe_lock:
        if _probed:
            return not _problem
        try:
            device = _make_device(DEFAULT_RATE, DEFAULT_CHANNELS)
        except (AudioError, OSError, ValueError) as exc:
            _problem = str(exc)
        except Exception as exc:                # noqa: BLE001
            _problem = "the audio backend failed: %s" % exc
        else:
            _problem = ""
            try:
                device.close()
            except Exception:                   # noqa: BLE001
                pass
        _probed = True
    return not _problem


def unavailable_reason():
    """Why not, in a form fit to show a user. None when it is available."""
    if available():
        return None
    return _problem or "there is no audio output device on this machine"


def forget_probe():
    """Ask again next time. For tests, and for a device appearing later."""
    global _probed, _problem
    _probed = False
    _problem = ""


# -- shutdown --------------------------------------------------------------
#
# Rule 6. An AudioUnit still running when the interpreter starts finalising
# will call a Python callback that no longer has an interpreter to run in,
# and that is a crash rather than an exception. So every open output is
# tracked and stopped first.

def _register(output):
    with _open_lock:
        if output not in _open_outputs:
            _open_outputs.append(output)


def _unregister(output):
    with _open_lock:
        if output in _open_outputs:
            _open_outputs.remove(output)


def close_all():
    """Stop every output this process opened. Idempotent."""
    with _open_lock:
        outputs = list(_open_outputs)
    for output in outputs:
        try:
            output.close()
        except Exception:                       # noqa: BLE001
            pass


atexit.register(close_all)


# -- a tone, for ears ------------------------------------------------------

def tone(seconds, frequency=440.0, rate=DEFAULT_RATE, channels=1,
         amplitude=0.25, phase=0.0):
    """Interleaved floats: a sine, for when the test has to be listened to.

    Not a fixture and not a toy. Every claim this module makes about sound
    coming out of a machine was checked by playing one of these and listening
    to it, and there is no substitute for that -- a test suite that passes
    while the speakers stay silent is precisely the failure this whole file
    is arranged to avoid.
    """
    count = int(seconds * rate)
    step = 2.0 * math.pi * frequency / rate
    out = [0.0] * (count * channels)
    for i in range(count):
        value = amplitude * math.sin(phase + step * i)
        base = i * channels
        for c in range(channels):
            out[base + c] = value
    return out


def _demo(argv):                                # pragma: no cover - by ear
    """``python3 -m feetbrowser.heel`` -- play a tone and say what happened."""
    seconds = float(argv[0]) if argv else 2.0
    frequency = float(argv[1]) if len(argv) > 1 else 440.0
    if available():
        print("device: ready")
    else:
        print("device: none -- %s" % unavailable_reason())
    out = open_output()
    print("output: %r" % (out,))
    if out.silent:
        print("this is the silent device; nothing will be audible")
    source = out.add_source(out.rate, channels=1, name="tone",
                            max_queue=seconds + 1.0)
    source.write(tone(seconds, frequency, out.rate, 1), fmt="float")
    source.close()
    deadline = time.monotonic() + seconds + 5.0
    while time.monotonic() < deadline and not source.ended:
        time.sleep(0.02)
    # The mixer keeps the ring full of silence whether or not anything is
    # playing, so an empty ring is never the signal that a sound has
    # finished. What has to drain is the ring's depth and the device's own
    # latency, and closing before that cuts the tail off the tone.
    time.sleep(out.ring.capacity / out.rate + out.latency + 0.05)
    print("played %.3f s of audio clock, %d underruns, %d silent frames, "
          "%d dropped" % (out.clock.now(), out.clock.underruns,
                          out.clock.silent_frames, source.dropped))
    out.close()


if __name__ == "__main__":                      # pragma: no cover
    _demo(sys.argv[1:])
