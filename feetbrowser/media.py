"""Playback: turning decoded frames into the right frame at the right time.

`mediacodec.py` answers "what does frame 37 look like". This module answers
the two questions that make it playback rather than decoding:

  *when* is frame 37 due, and what do we do when we cannot decode it in time.

The separation is the whole point of the design and is worth being explicit
about, because collapsing it is the single most common way a player goes
wrong. Decoding and presenting are different jobs on different schedules:

  * **Presentation time comes from a clock, never from a frame count.** The
    frame to show is the newest one whose `pts` has passed. If we ever
    computed "show the next frame" we would drift by exactly as much time as
    decoding cost, and the drift would accumulate for the length of the film.
    Because the media position is `clock.now() - started_at + started_from`,
    a decode that takes too long costs us *frames*, never *sync*.

  * **A slow decoder drops frames; it never stalls the UI.** `tick()` takes
    whatever is ready and returns. It has no blocking call in it -- no lock
    held across a decode, no queue `get()` without a zero timeout, no join.
    If the decoder is behind, `tick()` returns None, the last frame stays on
    screen, and the position keeps advancing. When the gap gets large enough
    that catching up frame by frame is hopeless, the scheduler resynchronises
    the decoder onto the keyframe before where the clock now is, which is the
    only correct way to skip in an inter-frame codec.

  * **The clock is injected.** `SystemClock` is `time.monotonic`; `Clock` is
    a plain object a test drives by hand. Every scheduling test in the suite
    uses the manual one, so the assertions are about the scheduler and not
    about how busy the machine was.

Threading: `VideoPlayer` runs the decoder on a daemon thread feeding a bounded
queue, so decoding a frame never happens on the thread that is drawing. The
bound is what keeps a fast decoder from reading a whole film into memory. For
tests -- and for the headless paths that have no event loop to be blocked --
`threaded=False` decodes inline from `tick()` with an explicit per-tick
budget, which is also how a decoder that cannot keep up is simulated
deterministically.

`Clock` is also the seam where the sound goes. A real player syncs video to
the audio clock and never the reverse -- a dropped picture costs one frame
and most people will not see it, a gap in the sound is a click and everybody
hears every one of them -- and because `position()` here is
`_offset + (clock.now() - _origin)`, a clock that already reports media time
makes both of those terms cancel and needs no other change at all. That clock
is `_AudioClock`, `attach_audio()` installs it, and `arch.AudioPlayer` is
what it reads. Without one, or with an output device nobody can hear, none of
this module behaves any differently from the day it had no audio in it.
"""

import threading
import time
from collections import deque

from . import imagecodec, mediacodec
from .canvas import PhotoImage
from .mediacodec import MediaError

__all__ = ["Clock", "SystemClock", "ManualClock", "VideoPlayer", "Scheduler",
           "MediaError", "QUEUE_DEPTH", "RESYNC_FRAMES"]


# How many decoded frames may sit ahead of the playhead. Four is enough to
# ride out a slow frame and small enough that a 1080p stream costs ~33 MB of
# queue rather than the whole film.
QUEUE_DEPTH = 4

# How far behind the playhead the decoder is allowed to fall before we stop
# trying to catch up frame by frame and jump it to a keyframe instead.
RESYNC_FRAMES = 8


class Clock:
    """Monotonic seconds. The base class is the manual one on purpose: a
    clock you can only read is a clock no test can pin down."""

    def now(self):
        raise NotImplementedError


class SystemClock(Clock):
    """The real clock. `monotonic` rather than `time()` because a player must
    not jump when someone corrects the system time mid-frame."""

    def now(self):
        return time.monotonic()


class _AudioClock(Clock):
    """An audio player's media position, wearing a clock's face.

    The whole of A/V sync, and it is four lines because `Scheduler` was
    written to make it four lines. `Scheduler.position()` is
    `_offset + (clock.now() - _origin)`; `_origin` is whatever this returned
    when playback started and `_offset` is the media time at that instant, so
    a clock reporting media time cancels them both and the scheduler is left
    asking the sound where it is.

    What it must read is `heel.Source.position()`, which
    `AudioPlayer.position()` is derived from -- the *stream* timeline, with
    the ring backlog and the device latency already taken off. The other
    audio clock in the tree, `heel.AudioClock.now()`, is the device timeline:
    a fine number, an entirely wrong one to put here, and wrong in a way that
    raises nothing and sounds perfect. It would put every picture a buffer's
    depth ahead of its sound for the length of the film.
    """

    __slots__ = ("player",)

    def __init__(self, player):
        self.player = player

    def now(self):
        return self.player.position()


class ManualClock(Clock):
    """A clock that only moves when a test moves it."""

    def __init__(self, start=0.0):
        self._t = float(start)

    def now(self):
        return self._t

    def advance(self, seconds):
        if seconds < 0:
            raise ValueError("a clock does not go backwards")
        self._t += seconds
        return self._t

    def set(self, seconds):
        self._t = float(seconds)
        return self._t


class Scheduler:
    """Which frame is due, given a clock and a queue of decoded frames.

    Owns no decoder and no thread; it is a pure function of (clock, queue,
    play state) with the counters that make its behaviour observable. Kept
    separate from `VideoPlayer` so the timing rules can be tested without a
    file, a codec or a canvas anywhere near them.
    """

    def __init__(self, duration, frame_rate, clock=None, loop=False,
                 index_at=None):
        self.clock = clock or SystemClock()
        self.duration = duration
        self.frame_rate = frame_rate or 0.0
        # How to turn a media position into a frame number. A track from a
        # container that records per-frame times hands its own lookup over,
        # because dividing by an average rate is only right when the rate is
        # constant, and an MP4's is allowed not to be.
        self.index_at = index_at
        self.loop = loop
        self.playing = False
        self._origin = 0.0          # clock reading when play() was called
        self._offset = 0.0          # media position at that instant
        self.queue = deque()
        self.current = None
        # Counters. Every one of these is asserted somewhere in the suite,
        # because "it dropped frames instead of drifting" is not a claim you
        # can make from watching it.
        self.presented = 0
        self.dropped = 0
        self.starved = 0
        self.resyncs = 0
        self.ended = False

    # -- transport ----------------------------------------------------------

    def play(self):
        if self.playing:
            return
        self._origin = self.clock.now()
        self.playing = True
        self.ended = False

    def pause(self):
        if not self.playing:
            return
        self._offset = self.position()
        self.playing = False

    def seek(self, seconds):
        seconds = max(0.0, min(float(seconds), self.duration))
        self._offset = seconds
        self._origin = self.clock.now()
        self.queue.clear()
        self.ended = False

    def position(self):
        """Media time in seconds. This is the only source of truth about
        where playback is; nothing counts frames."""
        if not self.playing:
            return self._offset
        return self._offset + (self.clock.now() - self._origin)

    def due_index(self):
        """The frame that should be on screen right now."""
        if self.index_at is not None:
            return self.index_at(self.position())
        if self.frame_rate <= 0:
            return 0
        return int(self.position() * self.frame_rate)

    # -- the tick -----------------------------------------------------------

    def tick(self):
        """Return the frame to present now, or None to hold the current one.

        Never blocks and never decodes. Frames in the queue that the playhead
        has already passed are discarded and counted, which is what "drop
        rather than drift" means in code.
        """
        if not self.playing:
            return None
        now = self.position()
        if self.duration and now >= self.duration:
            if self.loop:
                self.seek(0.0)
                return None
            self.playing = False
            self._offset = self.duration
            self.ended = True
            return None
        chosen = None
        while self.queue and self.queue[0].pts <= now:
            frame = self.queue.popleft()
            if chosen is not None:
                # We are handing back a newer frame than this one, so this
                # one is never going to be seen.
                self.dropped += 1
            chosen = frame
        if chosen is None:
            if not self.queue:
                self.starved += 1
            return None
        self.current = chosen
        self.presented += 1
        return chosen

    def push(self, frame):
        self.queue.append(frame)

    def behind_by(self, decoder_index):
        """How many frames the decoder is behind where the clock wants it."""
        return self.due_index() - decoder_index


class VideoPlayer:
    """A `<video>`'s worth of state: a track, a scheduler, a decode worker and
    the `PhotoImage` the renderer blits.

    The image buffer is allocated once and written in place, so presenting a
    frame is one `bytearray` slice assignment and the retained canvas item
    never has to be rebuilt.

    A file we cannot decode still produces a usable player: `info` carries the
    real dimensions and duration, `error` carries a sentence fit to show, and
    the element lays out at the right size with a placeholder in it. That is
    deliberate -- `<video src="clip.mp4">` should reserve 1280x720 and say
    what is wrong, not collapse to nothing.
    """

    def __init__(self, data=None, track=None, clock=None, loop=False,
                 autoplay=False, threaded=True, decode_budget=2):
        self.track = None
        self.info = None
        self.error = ""
        self.photo = None
        self.loop = bool(loop)
        self.threaded = bool(threaded)
        self.decode_budget = max(1, int(decode_budget))
        self._thread = None
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._lock = threading.Lock()
        # Held across every call into the track. The decode worker is not the
        # only thread that decodes: a seek while paused decodes the frame it
        # landed on, inline, so the picture moves under the scrubber, and a
        # codec that keeps a previous frame between calls does not survive two
        # threads walking it at once.
        self._decode_lock = threading.Lock()
        self._next_index = 0        # next frame the decoder will produce
        self._resync_to = None
        self.decoded = 0
        self.decode_errors = 0

        if track is None and data is not None:
            try:
                track = mediacodec.open_video(data)
            except mediacodec.MediaError as exc:
                info = getattr(exc, "info", None)
                if info is None:
                    try:
                        info = mediacodec.probe(data)
                    except mediacodec.MediaError:
                        info = mediacodec.MediaInfo("unknown")
                self.info = info
                self.error = info.reason or str(exc)
        if track is not None:
            self.track = track
            self.info = track.info
        info = self.info or mediacodec.MediaInfo("unknown")
        self.width = info.width
        self.height = info.height
        self.scheduler = Scheduler(info.duration,
                                   track.frame_rate if track else 0.0,
                                   clock=clock, loop=loop,
                                   index_at=track.index_at if track else None)
        # The clock to go back to if the sound is ever detached. Kept rather
        # than rebuilt, because a caller who passed a ManualClock in expects
        # to still be driving after it takes the audio away again.
        self._own_clock = self.scheduler.clock
        self.audio = None
        self.display_size = (self.width, self.height)
        if track is not None:
            self.photo = PhotoImage(width=track.width, height=track.height)
            # Our codecs always write alpha 255, so the surface can take the
            # opaque row-copy path instead of blending every pixel.
            self.photo.opaque = True
            if autoplay:
                self.play()

    def set_display_size(self, width, height):
        """Resize the buffer the renderer blits.

        The rasteriser blits an image at its stored size -- there is no scale
        at paint time -- so `<video width=640>` on a 320-wide file means the
        player scales, once per presented frame, into a buffer of the size
        layout asked for. Nearest-neighbour, through the existing
        `imagecodec.resize`; a real player would ask the compositor for this.
        """
        if self.track is None:
            return False
        width = max(1, int(width))
        height = max(1, int(height))
        if (width, height) == self.display_size:
            return False
        self.display_size = (width, height)
        self.photo = PhotoImage(width=width, height=height)
        self.photo.opaque = True
        frame = self.scheduler.current
        if frame is not None:
            self._present(frame)
        return True

    def _present(self, frame):
        """Write a decoded frame into the buffer the renderer reads."""
        want_w, want_h = self.display_size
        if (want_w, want_h) == (frame.width, frame.height):
            self.photo.rgba[:] = frame.rgba
        else:
            self.photo.rgba[:] = imagecodec.resize(
                frame.rgba, frame.width, frame.height, want_w, want_h)

    # -- transport ----------------------------------------------------------

    @property
    def playing(self):
        return self.scheduler.playing

    @property
    def ended(self):
        return self.scheduler.ended

    def position(self):
        return self.scheduler.position()

    # -- the sound ----------------------------------------------------------

    def attach_audio(self, player):
        """Schedule the pictures against `player`'s sound instead of a clock.

        `player` is anything with `start(position)`, `stop()`, `seek(seconds)`,
        `set_gain(gain)`, `set_rate(rate)` and `position()` --
        `arch.AudioPlayer` in the browser, a handful of lines in a test.
        Returns True when the sound is now driving.

        It declines in the two cases where following the sound would be worse
        than ignoring it: a file whose pictures we cannot decode anyway, and
        a player that says it is `silent`, which is a file with no sound in it
        or an output device nobody can hear. Following a device nobody can
        hear buys nothing and hands the video a new way to stop, so in both
        cases the clock stays exactly what it was and this module behaves
        exactly as it did before there was any audio in the tree.
        """
        if player is None or self.track is None or getattr(player, "silent",
                                                           False):
            return self.detach_audio()
        where = self.scheduler.position()
        self.audio = player
        self.scheduler.clock = _AudioClock(player)
        # Put the sound in the same state the picture is in, at the same
        # place, before anything reads the new clock.
        if self.scheduler.playing:
            player.start(where)
        else:
            player.stop()
        self.seek(where)
        return True

    def detach_audio(self):
        """Go back to the clock this player was made with. Always False, so
        that `if not player.attach_audio(x)` reads the way it should."""
        # Read the playhead while the clock that has been driving it is still
        # installed. The two clocks are not on the same scale -- one is media
        # time and the other is whatever the caller passed in -- so asking
        # after the swap is asking the wrong one.
        where = self.scheduler.position()
        if self.audio is not None:
            self.audio.stop()
        self.audio = None
        self.scheduler.clock = self._own_clock
        self.scheduler._offset = where
        self.scheduler._origin = self.scheduler.clock.now()
        return False

    def set_volume(self, gain):
        """Loudness, 0.0 to 1.0. Silently fine with there being no sound."""
        if self.audio is None:
            return False
        self.audio.set_gain(gain)
        return True

    # -- transport, continued -----------------------------------------------

    def play(self):
        if self.track is None:
            return False
        if self.audio is not None and not self.scheduler.playing:
            # The sound starts first, and from where the picture is paused,
            # so that the clock `scheduler.play()` is about to read already
            # reports the position it is about to resume from. That is what
            # makes `_offset` and `_origin` cancel; see `_AudioClock`.
            self.audio.start(self.scheduler.position())
        self.scheduler.play()
        self._start_worker()
        return True

    def pause(self):
        # The scheduler first: it reads the clock to work out where it
        # stopped, and the clock is the sound.
        self.scheduler.pause()
        if self.audio is not None:
            self.audio.stop()
        return True

    def toggle(self):
        if self.track is None:
            return False
        if self.scheduler.playing:
            self.pause()
        else:
            self.play()
        return True

    def seek(self, seconds):
        if self.track is None:
            return False
        if self.audio is not None:
            # Before the scheduler, which re-origins itself against the
            # clock, and the clock is the sound: seeking it afterwards would
            # leave the pictures scheduled against where the sound used to be.
            self.audio.seek(seconds)
        self.scheduler.seek(seconds)
        target = self.scheduler.due_index()
        with self._lock:
            self._resync_to = target
        self._wake.set()
        if not self.scheduler.playing:
            # Nothing else will draw this. A paused player's tick() returns
            # None by design, so without decoding here the scrubber would
            # move and the picture would not -- which is the one thing a
            # viewer uses a scrubber to look at.
            self._present_index(target)
        return True

    def _present_index(self, index):
        """Decode one frame on the calling thread and put it on screen."""
        track = self.track
        if track is None:
            return False
        index = max(0, min(int(index), track.frame_count - 1))
        try:
            with self._decode_lock:
                frame = track.frame(index)
        except MediaError:
            self.decode_errors += 1
            return False
        self._present(frame)
        self.scheduler.current = frame
        return True

    # -- decoding -----------------------------------------------------------

    def _start_worker(self):
        if not self.threaded or self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="video-decode",
                                        daemon=True)
        self._thread.start()

    def close(self):
        """Stop the worker. Idempotent; safe from any thread.

        The attached audio player is stopped but not closed: this player did
        not open it and does not own the device behind it.
        """
        if self.audio is not None:
            self.audio.stop()
        self._stop.set()
        self._wake.set()
        thread = self._thread
        self._thread = None
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=1.0)

    def _run(self):
        while not self._stop.is_set():
            if not self.scheduler.playing:
                self._wake.wait(0.05)
                self._wake.clear()
                continue
            if len(self.scheduler.queue) >= QUEUE_DEPTH:
                # The queue is full: sleep for about a frame rather than
                # spinning. Bounded ahead-of-time decoding, bounded memory.
                self._wake.wait(self._frame_wait())
                self._wake.clear()
                continue
            if not self._decode_one():
                self._wake.wait(0.05)
                self._wake.clear()

    def _frame_wait(self):
        rate = self.scheduler.frame_rate
        return 1.0 / rate if rate > 0 else 0.02

    def _decode_one(self):
        """Decode exactly one frame into the queue. Returns False when there
        is nothing left to do."""
        track = self.track
        if track is None:
            return False
        with self._lock:
            resync = self._resync_to
            self._resync_to = None
            if resync is not None:
                self._next_index = max(0, min(resync, track.frame_count - 1))
            index = self._next_index
        if index >= track.frame_count:
            if not self.loop:
                return False
            index = 0
        try:
            with self._decode_lock:
                frame = track.frame(index)
        except MediaError:
            # One bad packet is not a reason to lose the film; skip it and
            # keep the position honest.
            self.decode_errors += 1
            with self._lock:
                self._next_index = index + 1
            return True
        self.scheduler.push(frame)
        self.decoded += 1
        with self._lock:
            if self._resync_to is None:
                self._next_index = index + 1
        return True

    def pump(self, budget=None):
        """Decode up to `budget` frames inline. Only used when threaded is
        False -- the deterministic path, and the one the timing tests drive."""
        if self.track is None:
            return 0
        budget = self.decode_budget if budget is None else budget
        done = 0
        while done < budget and len(self.scheduler.queue) < QUEUE_DEPTH:
            if not self._decode_one():
                break
            done += 1
        return done

    # -- the frame --------------------------------------------------------

    def tick(self):
        """Advance to the frame that is due now. Returns True when `photo`
        changed and the element needs repainting.

        This is what the browser calls from its timer. Nothing in it waits on
        the decoder.
        """
        if self.track is None:
            return False
        if not self.threaded and self.scheduler.playing:
            self.pump()
        frame = self.scheduler.tick()
        # Checked on every tick, not only on a starved one: presenting a
        # frame that is already several frames late is exactly the state that
        # needs correcting, and it is the state in which `tick()` succeeds.
        self._maybe_resync()
        if frame is None:
            return False
        self._present(frame)
        self._wake.set()
        return True

    def _maybe_resync(self):
        """When the decoder has fallen so far behind that decoding every
        intervening frame would never catch up, move it to the keyframe
        before the playhead. Frames are lost -- that is the point; the
        alternative is playing further and further behind for ever."""
        if not self.scheduler.playing or self.track is None:
            return
        with self._lock:
            pending = self._next_index
        if self.scheduler.behind_by(pending) <= RESYNC_FRAMES:
            return
        target = min(self.scheduler.due_index(), self.track.frame_count - 1)
        landing = self.track.keyframe_before(target)
        if landing <= pending:
            # The only keyframe behind us is one we have already passed, so
            # jumping would rewind. Nothing to do but keep decoding; an
            # all-delta stream this far behind is simply too slow for us.
            return
        with self._lock:
            self._resync_to = landing
        # Everything between where the decoder was and where it is landing is
        # a frame nobody will ever see, and so is anything already queued.
        self.scheduler.dropped += (landing - pending) + len(self.scheduler.queue)
        self.scheduler.queue.clear()
        self.scheduler.resyncs += 1
        self._wake.set()

    def first_frame(self):
        """Decode and present frame 0 without starting playback, so a paused
        `<video>` shows its own first frame instead of an empty box."""
        if self.track is None or self.scheduler.current is not None:
            return False
        if not self._present_index(0):
            return False
        with self._lock:
            self._resync_to = 0
        return True

    # -- description --------------------------------------------------------

    def status(self):
        """One line for the placeholder, the status bar or a test."""
        info = self.info
        if info is None:
            return "video: unreadable"
        size = "%dx%d" % (info.width, info.height) if info.width else "?"
        if self.error:
            return "%s %s %s -- %s" % (info.container, info.codec or "?", size,
                                       self.error)
        return "%s %s %s %.1fs %s" % (
            info.container, info.codec, size, info.duration,
            "playing" if self.playing else "paused")

    def stats(self):
        scheduler = self.scheduler
        return {"decoded": self.decoded, "presented": scheduler.presented,
                "dropped": scheduler.dropped, "starved": scheduler.starved,
                "resyncs": scheduler.resyncs,
                "decode_errors": self.decode_errors}
