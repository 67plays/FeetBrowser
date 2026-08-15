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

There is no audio here, and no audio anywhere in this branch: no output
device, no clock derived from one, no A/V sync. A real player syncs video to
the audio clock, and `Clock` is the seam where that would go.

What this module *does* now carry, because the JavaScript `HTMLMediaElement`
API on the DOM bridge needs somewhere honest to put it, is the mixer state a
page can set from script: `volume`, `muted` and `playback_rate`. None of them
makes a sound today. They are here rather than on the bridge because they are
playback state, they have to survive the element being re-wrapped for every
property read, and the audio backend that will eventually read them talks to a
player, not to a host object. See `attach_audio()` for what that backend is
expected to be.

The player also keeps the queue of `HTMLMediaElement` events it owes the
document (`drain_events()`). Emitting them here rather than from the bridge is
not a layering accident: `timeupdate` and the end-of-media sequence happen
because a clock moved, and `tick()` is the only thing in the system that
notices a clock moving.
"""

import threading
import time
from collections import deque

from . import imagecodec, mediacodec
from .canvas import PhotoImage
from .mediacodec import MediaError

__all__ = ["Clock", "SystemClock", "ManualClock", "VideoPlayer", "Scheduler",
           "MediaError", "QUEUE_DEPTH", "RESYNC_FRAMES", "PLAYABLE_TYPES",
           "can_play_type", "HAVE_NOTHING", "HAVE_METADATA",
           "HAVE_CURRENT_DATA", "HAVE_FUTURE_DATA", "HAVE_ENOUGH_DATA",
           "TIMEUPDATE_INTERVAL"]


# How many decoded frames may sit ahead of the playhead. Four is enough to
# ride out a slow frame and small enough that a 1080p stream costs ~33 MB of
# queue rather than the whole film.
QUEUE_DEPTH = 4

# How far behind the playhead the decoder is allowed to fall before we stop
# trying to catch up frame by frame and jump it to a keyframe instead.
RESYNC_FRAMES = 8

# `HTMLMediaElement.readyState`. We only ever report the two ends of it: the
# whole file is in memory before a player exists at all, so there is no state
# in which some of the media is buffered and the rest is not. When streaming
# arrives the middle three become reachable and the constants are already here.
HAVE_NOTHING = 0
HAVE_METADATA = 1
HAVE_CURRENT_DATA = 2
HAVE_FUTURE_DATA = 3
HAVE_ENOUGH_DATA = 4

# The floor on the gap between two `timeupdate` events during ordinary
# playback. The HTML specification leaves the rate to the user agent but caps
# it, and every shipping browser lands on four a second; a page that redraws a
# progress bar from `timeupdate` is written against that number, and firing on
# every frame instead would have it redrawing 25 times a second for no more
# information.
TIMEUPDATE_INTERVAL = 0.25

# The `type` values on a `<source>` that are worth fetching, and the ones
# `canPlayType()` answers about. A type we can decode is not the same as a
# container we can decode -- `video/mp4` covers both an H.264 film we may not
# be able to finish and a Motion JPEG .mov we can -- so this list is about
# containers and the codec question is settled by opening the file.
PLAYABLE_TYPES = ("video/x-msvideo", "video/avi", "video/msvideo",
                  "video/vnd.avi", "video/quicktime", "video/x-motion-jpeg",
                  "video/x-jpeg", "video/mjpeg", "multipart/x-mixed-replace")

# Codec identifiers inside a `codecs=` parameter that we decode all the way to
# pixels, so a type carrying one of them can be answered with more confidence
# than the container alone allows.
_CERTAIN_CODECS = ("mjpg", "mjpa", "jpeg", "mp1v", "rle ", "raw", "png")


def can_play_type(content_type):
    """`HTMLMediaElement.canPlayType()`: "", "maybe" or "probably".

    The three-valued answer is not hedging for its own sake. "maybe" is the
    only truthful answer a container name can support here, because whether a
    `.mov` plays depends on the codec inside it, and the only way to know that
    is to open the file -- which is exactly the position a real browser is in
    when it is handed `video/quicktime` with no `codecs` parameter. A page
    picking between sources gets the same information we would act on
    ourselves.

    Audio is always "", and that is the honest answer rather than a placeholder
    one: nothing in this tree decodes or outputs a sample, so a page that asks
    whether we can play `audio/mpeg` and is told "maybe" has been lied to.
    """
    if not content_type:
        return ""
    text = str(content_type).strip().lower()
    parts = [p.strip() for p in text.split(";")]
    mime = parts[0]
    if not mime or mime.startswith("audio/"):
        return ""
    codecs = ""
    for parameter in parts[1:]:
        if parameter.startswith("codecs"):
            _name, _sep, value = parameter.partition("=")
            codecs = value.strip().strip('"').strip("'")
    if mime not in PLAYABLE_TYPES:
        return ""
    if codecs:
        names = [c.strip() for c in codecs.split(",") if c.strip()]
        if names and all(any(n.startswith(c) for c in _CERTAIN_CODECS)
                         for n in names):
            return "probably"
        return ""
    return "maybe"


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
        # Media seconds per clock second. Every rebasing of `_offset` and
        # `_origin` exists so this can change mid-play without the position
        # jumping: the elapsed part is banked at the old rate first.
        self.rate = 1.0
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
        # Bumped every time a looping clip wraps. The player watches it rather
        # than watching the position go backwards, because a seek makes the
        # position go backwards too and the two mean different things to the
        # events a page sees.
        self.loops = 0

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
        return self._offset + (self.clock.now() - self._origin) * self.rate

    def set_rate(self, rate):
        """Change how fast media time runs against clock time.

        The banking is the whole of it: whatever has already elapsed happened
        at the old rate and must not be recomputed at the new one, so the
        position becomes the new `_offset` and the clock reading becomes the
        new `_origin` before `rate` moves. Get that wrong and setting
        `playbackRate` to 2 halfway through a clip does not double the speed,
        it teleports the playhead to twice where it was.

        A negative rate is stored but does not run backwards -- there is no
        reverse decode path here, and the honest behaviour for a rate we
        cannot honour is a playhead that stands still rather than one that
        walks off the front of the file.
        """
        rate = float(rate)
        self._offset = self.position()
        self._origin = self.clock.now()
        self.rate = max(0.0, rate)
        return self.rate

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
                self.loops += 1
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

        # -- HTMLMediaElement state -----------------------------------------
        # Everything a script can set that is not the playhead. `volume` and
        # `muted` are inert today by construction, not by oversight: see
        # attach_audio().
        self.volume = 1.0
        self.muted = False
        self.playback_rate = 1.0
        self.ready_state = HAVE_NOTHING
        self.seeking = False
        self.audio = None
        # Events owed to the document, oldest first. A deque rather than a
        # callback because the thing that has to fire them (the DOM bridge)
        # runs on the UI thread and this can be reached from a decode thread,
        # and because a test can then assert the order without a browser.
        self.events = deque()
        self._last_timeupdate = None
        self._last_position = 0.0
        self._loops_seen = 0

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
        self.display_size = (self.width, self.height)
        if track is not None:
            self.photo = PhotoImage(width=track.width, height=track.height)
            # Our codecs always write alpha 255, so the surface can take the
            # opaque row-copy path instead of blending every pixel.
            self.photo.opaque = True
            # The whole file is already in memory -- `load_videos()` fetches it
            # before this constructor runs -- so the two states a streaming
            # player would pass through on its way here are both true at the
            # same instant, and the two events that mark them are queued back
            # to back. They are still queued rather than skipped, because a
            # page that waits for `canplay` before calling `play()` waits for
            # ever otherwise, and that is a common shape.
            self.ready_state = HAVE_ENOUGH_DATA
            self._emit("loadedmetadata")
            self._emit("canplay")
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

    # -- events -------------------------------------------------------------

    def _emit(self, name):
        """Queue one `HTMLMediaElement` event. Nothing here dispatches: the
        player has no idea a DOM exists, and the element that does owns the
        decision about when a handler may run."""
        self.events.append(name)

    def drain_events(self):
        """Take every queued event name, oldest first, and clear the queue."""
        out = list(self.events)
        self.events.clear()
        return out

    # -- audio, or rather the shape of the hole where audio goes -------------

    def gain(self):
        """The linear gain an output device should apply: 0 while muted.

        `muted` deliberately does not zero `volume`. The specification keeps
        them apart because unmuting has to restore the level the user chose,
        and a player that folded one into the other would have nothing to
        restore.
        """
        return 0.0 if self.muted else max(0.0, min(1.0, self.volume))

    def attach_audio(self, track):
        """Hand this player an audio track, and the seam this module ends at.

        There is no audio decoder and no output device in this tree, so
        `track` is always None today and this method is never called. It is
        written down because the state above it (`volume`, `muted`,
        `playback_rate`, and the playhead itself) is exactly the state an
        audio backend needs, and leaving that state without a stated consumer
        is how it ends up wrong by the time one arrives.

        What a track is expected to be, so that the backend and this file do
        not have to be written by the same person:

          * `start(position)`, `stop()` and `seek(position)`, taking media
            seconds, called from the transport methods below. None of them may
            block: they are reached from the UI thread.
          * `set_gain(gain)`, taking the 0..1 value `gain()` returns, called
            whenever `volume` or `muted` moves.
          * `set_rate(rate)`, taking media seconds per clock second. A backend
            that cannot resample may ignore anything but 1.0, but it has to
            say so by returning False so the caller does not think the pitch
            is being corrected.
          * `position()`, returning media seconds actually delivered to the
            device, or None while it has none. This is the one that matters
            most and is the reason `Clock` is injected rather than hardcoded:
            once a device is playing, *it* owns the clock and the video
            follows it, because a sound card's crystal is what the listener
            can hear drifting. Wiring that up means giving `Scheduler` a clock
            backed by `track.position()`, not adding a correction term here.
        """
        self.audio = track
        if track is not None:
            track.set_gain(self.gain())
            track.set_rate(self.scheduler.rate)
        return track is not None

    def _tell_audio(self, method, *args):
        """Forward one transport change to the audio track, if there is one.
        Every call site below goes through here so that the day a track
        exists there is no transport path that forgot to tell it."""
        track = self.audio
        if track is None:
            return False
        getattr(track, method)(*args)
        return True

    # -- transport ----------------------------------------------------------

    @property
    def playing(self):
        return self.scheduler.playing

    @property
    def ended(self):
        return self.scheduler.ended

    @property
    def paused(self):
        """`HTMLMediaElement.paused`, which is not `not playing`: a player
        that has run off the end of the file has stopped playing *and* is
        paused, and one that has never been started is paused as well."""
        return not self.scheduler.playing

    @property
    def duration(self):
        """Seconds, or NaN when there is no media. NaN rather than 0 because
        the specification says so and because a page dividing by it to size a
        progress bar wants an answer that poisons the arithmetic visibly
        instead of one that puts the knob at the far end."""
        if self.track is None:
            return float("nan")
        return float(self.scheduler.duration)

    def position(self):
        return self.scheduler.position()

    def play(self):
        """Start playback, firing what the specification's `play()` fires.

        The rewind is step two of that algorithm and is the reason a page can
        put `<button onclick="v.play()">` next to a clip that has finished and
        have it start again rather than sit at the end doing nothing.
        """
        if self.track is None:
            return False
        was_paused = not self.scheduler.playing
        if self.scheduler.ended:
            self.seek(0.0)
        self.scheduler.play()
        self._start_worker()
        self._tell_audio("start", self.scheduler.position())
        if was_paused:
            self._emit("play")
            # `playing` follows immediately only because readyState is always
            # HAVE_ENOUGH_DATA here. A streaming player would emit `waiting`
            # instead and hold `playing` back until it had frames.
            if self.ready_state >= HAVE_FUTURE_DATA:
                self._emit("playing")
        return True

    def pause(self):
        was_playing = self.scheduler.playing
        self.scheduler.pause()
        if was_playing:
            self._tell_audio("stop")
            self._emit("pause")
        return True

    def load(self):
        """`HTMLMediaElement.load()`: throw the playback state away and begin
        again from the top.

        The bytes are not re-fetched. That is a real divergence from the
        specification, which reruns resource selection and may pick a
        different `<source>`, and it is bounded by what the fetch layer offers
        -- there is no way to ask for a URL again from here without reaching
        back into the Tab. What a page uses `load()` for in practice, which is
        resetting an element it has finished with, does work.
        """
        self.pause()
        self.scheduler.seek(0.0)
        self.scheduler.ended = False
        self._last_timeupdate = None
        self._last_position = 0.0
        with self._lock:
            self._resync_to = 0
        if self.track is None:
            self.ready_state = HAVE_NOTHING
            return False
        self.ready_state = HAVE_ENOUGH_DATA
        self._present_index(0)
        self._emit("loadedmetadata")
        self._emit("canplay")
        return True

    def set_volume(self, value):
        """Returns True when the value moved, which is what decides whether a
        `volumechange` is owed."""
        value = max(0.0, min(1.0, float(value)))
        if value == self.volume:
            return False
        self.volume = value
        self._tell_audio("set_gain", self.gain())
        self._emit("volumechange")
        return True

    def set_muted(self, value):
        value = bool(value)
        if value == self.muted:
            return False
        self.muted = value
        self._tell_audio("set_gain", self.gain())
        self._emit("volumechange")
        return True

    def set_loop(self, value):
        """Two copies of the flag, because the decoder reads one (to know
        whether to wrap back to frame zero) and the scheduler reads the other
        (to know whether reaching the duration is the end or a wrap), and a
        script setting `video.loop` has to move both or the clip wraps its
        pictures without wrapping its clock."""
        self.loop = bool(value)
        self.scheduler.loop = self.loop
        return self.loop

    def set_playback_rate(self, value):
        value = float(value)
        if value == self.playback_rate:
            return False
        self.playback_rate = value
        self.scheduler.set_rate(value)
        self._tell_audio("set_rate", self.scheduler.rate)
        self._emit("ratechange")
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
        """Move the playhead, firing the specification's seek sequence.

        `seeking`, then `timeupdate`, then `seeked`, and the `timeupdate` is
        not subject to the four-a-second throttle because it is a separate
        step of the seek algorithm rather than the ordinary "time marches on"
        one. A page driving a scrubber from `timeupdate` needs the position it
        just asked for to come back, not the position 240 ms of throttle later.

        The whole sequence is synchronous here because the seek genuinely is:
        the file is in memory and `_present_index` decodes the landing frame
        before returning, so there is no interval during which `seeking` is
        true and a real browser would have work outstanding.
        """
        if self.track is None:
            return False
        self.seeking = True
        self._emit("seeking")
        self.scheduler.seek(seconds)
        self._tell_audio("seek", self.scheduler.position())
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
        self.seeking = False
        self._last_position = self.scheduler.position()
        self._last_timeupdate = self.scheduler.clock.now()
        self._emit("timeupdate")
        self._emit("seeked")
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
        """Stop the worker. Idempotent; safe from any thread."""
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
        # Frames are consumed `playbackRate` times faster than they are due,
        # so the sleep between decodes has to shrink by the same factor or a
        # clip at 2x starves on a decoder that was keeping up perfectly at 1x.
        rate = self.scheduler.frame_rate * max(0.01, self.scheduler.rate)
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
        was_playing = self.scheduler.playing
        frame = self.scheduler.tick()
        self._media_events(was_playing)
        # Checked on every tick, not only on a starved one: presenting a
        # frame that is already several frames late is exactly the state that
        # needs correcting, and it is the state in which `tick()` succeeds.
        self._maybe_resync()
        if frame is None:
            return False
        self._present(frame)
        self._wake.set()
        return True

    def _media_events(self, was_playing):
        """Everything the clock owes the document, decided once per tick.

        Three things can have happened inside `Scheduler.tick()` and each has
        its own sequence in the specification:

        A looping clip may have wrapped, which is a seek to the start and
        fires what a seek fires. Playback may have run off the end, which
        fires `timeupdate`, then `pause` -- ending playback really does pause
        the element, which is the part of this people are surprised by -- and
        then `ended`. Or time simply passed, which is the "time marches on"
        step, and that one is throttled.
        """
        scheduler = self.scheduler
        if scheduler.loops != self._loops_seen:
            self._loops_seen = scheduler.loops
            self._last_position = scheduler.position()
            self._last_timeupdate = scheduler.clock.now()
            self._emit("seeking")
            self._emit("timeupdate")
            self._emit("seeked")
            return
        if was_playing and scheduler.ended:
            self._last_position = scheduler.position()
            self._last_timeupdate = scheduler.clock.now()
            self._emit("timeupdate")
            self._emit("pause")
            self._emit("ended")
            self._tell_audio("stop")
            return
        if not scheduler.playing:
            return
        now = scheduler.position()
        if now == self._last_position:
            return
        clock = scheduler.clock.now()
        if self._last_timeupdate is not None \
                and clock - self._last_timeupdate < TIMEUPDATE_INTERVAL:
            return
        self._last_position = now
        self._last_timeupdate = clock
        self._emit("timeupdate")

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
