"""Audio playback: the arch, because it carries the load between the heel
and the toes.

Licence condition 3 asks every subsystem to be named after a part of a foot.
The output device is the heel, the AAC decoder is the instep, and this is the
arch: the span between them, which holds nothing up on its own and without
which nothing stands. `mediacodec.py` says what frame 37 sounds like and
`heel.py` says how samples reach a speaker; this module is the only thing in
the tree that knows both, and it exists so that neither has to know the
other.

It is `media.VideoPlayer` in the other medium, deliberately down to the shape
of the calls, because the two are driven by one `<video>` element and an
element that has to remember which half wants which spelling is an element
that will get it wrong. That includes the `threaded=False` plus `pump()`
bargain: with a worker thread this decodes ahead on its own, and without one
every decode happens on the caller's thread at a moment the caller chose, so
a test is a sequence of calls rather than a race.

Three things in here are less obvious than they look.

**The position is the source's, not the device's.** `heel.AudioClock.now()`
is the device timeline -- how much sound the hardware has consumed since it
was switched on, silence included. `heel.Source.position()` is the *stream*
timeline: the same number with the ring backlog and the device's own latency
taken off, and measured from the last seek. Only the second one answers
"where in the file is the sound that is coming out of the speaker right
now", which is the only question a picture can be scheduled against. Using
the first is a silent bug: nothing raises, the sound is right, the video is a
buffer's depth ahead of it for ever.

**A seek throws sound away and nothing else may.** `seek()` is
`Source.restart(t)`, which drops what is queued and starts a new timeline --
that is exactly right for a scrubber, and exactly wrong for a loop or a
change of playback rate, where the sound already decoded is still the sound
that should be heard next. So those two do not touch the queue at all. They
push a boundary onto `_segments` instead, in `Source.position()` coordinates,
saying "when the playhead reaches here, media time is *that* and runs at
*this* rate from then on", and the boundary is popped as the playhead crosses
it. `_pos_end` and `_media_end` are the far end of that mapping: where
everything written so far finishes, in each of the two timelines.

**A frame whose channel count disagrees with the source's is not written.**
The source interleaves at a fixed channel count and resamples on the writing
side; a stereo frame written to a source built for mono is not quiet or
wrong-eared, it is noise, and it stays noise for the rest of the file because
every later frame is then interleaved off by one. Such a frame is counted and
its *duration* is written as silence, so that everything after it is still in
the right place, and `channel_errors` says it happened.
"""

import threading
from collections import deque

from . import heel, mediacodec
from .mediacodec import MediaError

__all__ = ["AudioPlayer", "shared_output", "close_shared_output",
           "TARGET_QUEUE", "DECODE_BUDGET"]


# How much decoded sound to keep ahead of the playhead. Half a second is
# about twenty AAC frames: long enough that a garbage collection or a slow
# page layout cannot empty it, short enough that a seek does not have to
# throw much away and that a `<video>` in a background tab is not holding
# seconds of PCM per element.
TARGET_QUEUE = 0.5

# Frames one inline `pump()` will decode before returning to its caller.
DECODE_BUDGET = 8

# How far forward the decoder will walk one frame at a time to reach a frame
# it skipped. Beyond this it lets the track replay from the start instead,
# which is slower per call but does not grow without bound.
MAX_WALK = 64


# The one device the whole browser mixes into. A page with four `<video>`
# elements is four sources on one output, not four outputs: `heel.Mixer` is
# there precisely so that the second sound does not have to ask the sound
# card for a second exclusive stream and lose. It is opened the first time
# something actually has audio to play, so a session that never meets a
# soundtrack never touches the device at all.
_shared_output = None
_shared_lock = threading.Lock()


def shared_output():
    """The browser's output device, opened on first use.

    `heel.open_output()` never raises: with no backend, or a backend that
    will not open, it hands back a paced `NullDevice` that keeps time and
    makes no sound. So this always returns something a source can be added
    to, and `Output.silent` is how a caller finds out which it got.
    """
    global _shared_output
    with _shared_lock:
        output = _shared_output
        if output is None or output.closed:
            output = _shared_output = heel.open_output()
        return output


def close_shared_output():
    """Give the device back. The next player that needs one reopens it."""
    global _shared_output
    with _shared_lock:
        output, _shared_output = _shared_output, None
    if output is not None:
        output.close()


class AudioPlayer:
    """An `<audio>`'s worth of state: a track, a `heel.Source`, and a worker.

    A file we cannot decode still produces a usable player, the same bargain
    `VideoPlayer` makes: `info` carries the real sample rate, channel count
    and duration, `error` carries a sentence fit to show, and every transport
    call answers False rather than raising. A `<video>` with an AC-3 track
    should play its pictures and say why it is silent, not fail to load.
    """

    def __init__(self, data=None, track=None, output=None, gain=1.0,
                 loop=False, autoplay=False, threaded=True,
                 decode_budget=DECODE_BUDGET, target_queue=TARGET_QUEUE,
                 name="audio"):
        self.track = None
        self.info = None
        self.error = ""
        self.loop = bool(loop)
        self.threaded = bool(threaded)
        self.decode_budget = max(1, int(decode_budget))
        self.target_queue = float(target_queue)
        self.name = name
        # Counters, all of them asserted somewhere in the suite: "it played
        # silence rather than noise" is not a claim you can make by listening
        # to a file that has no bad frames in it.
        self.decoded = 0
        self.decode_errors = 0
        self.channel_errors = 0
        self.loops = 0

        self._gain = max(0.0, float(gain))
        self._muted = False
        self._rate = 1.0
        self._playing = False
        self._closed = False
        self._paused_at = 0.0
        self._exhausted = False
        self._next_index = 0
        self._trim = 0              # samples to drop off the next frame
        self._last_index = -1
        self._last_frame = None
        # The far end of everything written, in both timelines, and the map
        # between them. See the module docstring.
        self._pos_end = 0.0
        self._media_end = 0.0
        self._segments = deque([(0.0, 0.0, 1.0)])
        self._lock = threading.Lock()
        # Held across every call into the track, for the same reason
        # VideoPlayer holds one: the AAC decoder keeps the previous frame's
        # second transform half, and two threads walking it at once is not a
        # race that shows up as an exception, it is a race that shows up as a
        # noise.
        self._decode_lock = threading.Lock()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread = None

        if track is None and data is not None:
            try:
                track = mediacodec.open_audio(data)
            except MediaError as exc:
                info = getattr(exc, "info", None)
                if info is None:
                    try:
                        info = mediacodec.probe_audio(data)
                    except MediaError:
                        info = mediacodec.AudioInfo("unknown")
                self.info = info
                self.error = info.reason or str(exc)
        if track is not None:
            self.track = track
            self.info = track.info
        info = self.info or mediacodec.AudioInfo("unknown")
        self.sample_rate = int(info.sample_rate or heel.DEFAULT_RATE)
        self.channels = int(info.channels or 1)
        self.duration = float(info.duration)

        self.output = output
        self.source = None
        if self.track is not None:
            if self.output is None:
                self.output = shared_output()
            self.source = self.output.add_source(
                self.sample_rate, self.channels,
                0.0 if self._muted else self._gain, name)
            if autoplay:
                self.play()

    # -- what we are -------------------------------------------------------

    @property
    def playable(self):
        """Whether there is sound here we can actually decode."""
        return self.track is not None

    @property
    def silent(self):
        """Whether the sound is going nowhere: no track, or the null device.

        A caller synchronising pictures against us wants this. The paced null
        device keeps perfect time and is the right thing to feed a headless
        box, but it is also what a machine gets when its sound card has just
        been unplugged, and a video whose clock is a device nobody can hear
        is a video with a new way to stop.
        """
        return self.track is None or self.output is None or self.output.silent

    @property
    def playing(self):
        return self._playing

    @property
    def ended(self):
        """True once the last sample written has actually been heard."""
        if self.track is None:
            return False
        with self._lock:
            if not self._exhausted:
                return False
            end = self._pos_end
        return self.source is None or self.source.position() >= end

    @property
    def rate(self):
        return self._rate

    @property
    def gain(self):
        return self._gain

    @gain.setter
    def gain(self, value):
        value = 0.0 if value < 0.0 else float(value)
        self._gain = value
        if self.source is not None and not self._muted:
            self.source.gain = value

    @property
    def muted(self):
        return self._muted

    @muted.setter
    def muted(self, value):
        self._muted = bool(value)
        if self.source is not None:
            # Muting is a gain of zero rather than a stopped decoder, so that
            # an unmute is instant and the position never stops moving --
            # which is what the pictures are scheduled against.
            self.source.gain = 0.0 if self._muted else self._gain

    # -- where we are ------------------------------------------------------

    def position(self):
        """Media time in seconds: where the sound leaving the speaker is.

        Not what has been decoded and not what has been mixed. While paused
        it is the instant we stopped at, because nothing is coming out.
        """
        if self.source is None or not self._playing:
            return self._paused_at
        pos = self.source.position()
        with self._lock:
            segments = self._segments
            while len(segments) > 1 and pos >= segments[1][0]:
                segments.popleft()
            base_pos, base_media, rate = segments[0]
        return base_media + (pos - base_pos) * rate

    def queued_seconds(self):
        """Decoded sound waiting to be played. What the worker throttles on."""
        return 0.0 if self.source is None else self.source.queued_seconds()

    # -- transport ---------------------------------------------------------

    def play(self):
        if self.track is None or self._closed:
            return False
        if self._playing:
            return True
        self.output.start()
        # Begin a segment at the point we were paused at. Doing this on every
        # play() rather than only on a seek is what makes position() report
        # exactly the paused instant the moment play() returns, which is the
        # property the video half cancels its clock against.
        self._begin(self._paused_at)
        self._playing = True
        self._stop.clear()
        self._start_worker()
        if not self.threaded:
            self.pump()
        return True

    def pause(self):
        if self.track is None:
            return False
        if not self._playing:
            return True
        self._paused_at = self.position()
        self._playing = False
        # The sound has to actually stop, so what is queued goes. What is
        # already in the ring will still be heard -- a few tens of
        # milliseconds -- and no player anywhere avoids that.
        self.source.restart(self._paused_at)
        self._wake.set()
        return True

    def toggle(self):
        if self.track is None:
            return False
        return self.pause() if self._playing else self.play()

    def seek(self, seconds):
        """Jump. This is the one operation that throws decoded sound away."""
        if self.track is None:
            return False
        self._begin(seconds)
        self._wake.set()
        if not self.threaded and self._playing:
            self.pump()
        return True

    # The shape `media.VideoPlayer.attach_audio` drives. Same operations,
    # named the way a thing being driven is named rather than the way a thing
    # a user clicks is named.

    def start(self, position=None):
        if position is not None:
            self._paused_at = self._clamp(position)
        return self.play()

    def stop(self):
        return self.pause()

    def set_gain(self, gain):
        self.gain = gain
        return True

    def set_rate(self, rate):
        """Change the playback rate. **Not** a seek: nothing already decoded
        is thrown away, because it is still the sound that comes next.

        All that moves is the map from the source's timeline to the media's,
        and it moves from the far end of what has been written rather than
        from the playhead, so the half-second already in the queue keeps
        playing at the rate it was decoded for.
        """
        rate = float(rate)
        if rate <= 0.0:
            raise ValueError("a playback rate is positive")
        with self._lock:
            if rate == self._rate:
                return False
            self._segments.append((self._pos_end, self._media_end, rate))
            self._rate = rate
        self._wake.set()
        return True

    def _clamp(self, seconds):
        seconds = max(0.0, float(seconds))
        if self.duration:
            seconds = min(seconds, self.duration)
        return seconds

    def _begin(self, seconds):
        """Start a new timeline at `seconds`. The seek, without the guards."""
        seconds = self._clamp(seconds)
        track = self.track
        index = track.index_at(seconds)
        # AAC frames are 23 milliseconds long and a seek lands inside one, so
        # the head of that frame is sound from before where we were asked to
        # go. Dropping those samples is what makes the seek land where the
        # scrubber says rather than up to a frame early.
        trim = int(round((seconds - track.frame_time(index))
                         * (track.sample_rate or self.sample_rate)))
        self.source.restart(seconds)
        with self._lock:
            self._segments.clear()
            self._segments.append((seconds, seconds, self._rate))
            self._pos_end = seconds
            self._media_end = seconds
            self._next_index = index
            self._trim = max(0, trim)
            self._exhausted = False
        self._paused_at = seconds

    # -- decoding ----------------------------------------------------------

    def _start_worker(self):
        if not self.threaded or self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="audio-decode",
                                        daemon=True)
        self._thread.start()

    def _run(self):
        while not self._stop.is_set():
            if not self._playing:
                self._wake.wait(0.05)
                self._wake.clear()
                continue
            if self.source.queued_seconds() >= self.target_queue:
                # Full enough. Sleep for a quarter of the target rather than
                # spinning: the shortest nap that cannot empty the queue.
                self._wake.wait(self.target_queue * 0.25)
                self._wake.clear()
                continue
            try:
                more = self._decode_one()
            except Exception as exc:            # noqa: BLE001
                # A broken frame must not take the decode thread with it, and
                # must not be retried in a tight loop either.
                self.decode_errors += 1
                self.error = "the audio decoder failed: %s" % exc
                more = False
            if not more:
                self._wake.wait(0.05)
                self._wake.clear()

    def pump(self, budget=None):
        """Decode ahead inline, up to `budget` frames. The deterministic path.

        Only does anything while playing: decoding into a paused source would
        hand the mixer sound to play, which is what pausing was for.
        """
        if self.track is None or not self._playing:
            return 0
        budget = self.decode_budget if budget is None else budget
        done = 0
        while done < budget:
            if self.source.queued_seconds() >= self.target_queue:
                break
            if not self._decode_one():
                break
            done += 1
        return done

    def _frame_at(self, index):
        """Decode frame `index` without ever asking the codec to go backwards.

        AAC has no keyframes, so `AudioTrack.frame()` replays the file from
        frame zero for any request that is not the one that comes next.
        Walking forward a frame at a time keeps that from happening when a
        rate above 1.0 skips frames, and a one-frame cache keeps it from
        happening when a rate below 1.0 asks for the same frame twice.
        """
        if index == self._last_index and self._last_frame is not None:
            return self._last_frame
        with self._decode_lock:
            cursor = self._last_index
            if 0 <= cursor < index - 1 and index - cursor <= MAX_WALK:
                for i in range(cursor + 1, index):
                    self.track.frame(i)     # for its state, not its sound
            frame = self.track.frame(index)
        self._last_index = index
        self._last_frame = frame
        return frame

    def _decode_one(self):
        """Decode one frame into the source. False when there is no more."""
        track = self.track
        if track is None or self.source is None:
            return False
        with self._lock:
            index = self._next_index
            trim = self._trim
            rate = self._rate
        if index >= track.sample_count:
            if not self.loop:
                with self._lock:
                    self._exhausted = True
                return False
            index = 0
            trim = 0
            # A loop is not a seek. Everything already written is still going
            # to be heard, so nothing is dropped; the boundary just says that
            # media time goes back to zero when the playhead gets that far.
            with self._lock:
                self._segments.append((self._pos_end, 0.0, rate))
                self._media_end = 0.0
                self._next_index = 0
                self.loops += 1
        try:
            frame = self._frame_at(index)
        except MediaError as exc:
            # One bad packet is not a reason to lose the sound. Skip it and
            # keep going; the position stays honest because nothing was
            # written for it.
            self.decode_errors += 1
            self.error = str(exc)
            with self._lock:
                self._next_index = index + 1
            return True

        count = frame.sample_count
        samples = frame.samples
        if trim:
            if trim >= count:
                with self._lock:
                    self._trim = 0
                    self._next_index = index + 1
                return True
            samples = samples[trim * frame.channels * 4:]
            count -= trim
        if frame.channels != self.source.channels:
            # Not written. Interleaved at the wrong width these samples are
            # noise, and every frame after them is off by one for the rest of
            # the file. Its duration goes in as silence so that everything
            # after it is still in the right place.
            self.channel_errors += 1
            self.error = ("audio frame %d has %d channels, the stream said %d"
                          % (index, frame.channels, self.source.channels))
            samples = heel.silence(count, self.source.channels, heel.FLOAT32)
        self.source.write(samples, fmt=heel.FLOAT32)

        seconds = count / float(frame.sample_rate or self.sample_rate)
        with self._lock:
            self._pos_end += seconds
            self._media_end += seconds * rate
            self._trim = 0
            self.decoded += 1
            if rate == 1.0:
                self._next_index = index + 1
            else:
                # At a rate other than one the frame that comes next is the
                # one playing at the media time we have now written up to,
                # which skips frames above 1.0 and repeats them below it.
                nxt = track.index_at(self._media_end)
                self._next_index = nxt if nxt > index else index
        return True

    # -- shutting down -----------------------------------------------------

    def close(self):
        """Stop the worker and let the source go. Idempotent, any thread.

        The device is not closed: it is shared with every other player on
        the page, and the last `<video>` on a page being scrolled past is
        not a reason to hand the sound card back.
        """
        self._closed = True
        self._playing = False
        self._stop.set()
        self._wake.set()
        thread, self._thread = self._thread, None
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=1.0)
        source, self.source = self.source, None
        if source is not None:
            source.close()
            source.clear()
            if self.output is not None:
                self.output.remove_source(source)

    # -- description -------------------------------------------------------

    def status(self):
        """One line for the status bar, the placeholder, or a test."""
        info = self.info
        if info is None:
            return "audio: none"
        shape = "%d Hz x%d" % (info.sample_rate, info.channels)
        if self.error:
            return "%s %s %s -- %s" % (info.container, info.codec or "?",
                                       shape, self.error)
        return "%s %s %s %.1fs %s" % (
            info.container, info.codec, shape, info.duration,
            "playing" if self._playing else "paused")

    def stats(self):
        source = self.source
        return {"decoded": self.decoded,
                "decode_errors": self.decode_errors,
                "channel_errors": self.channel_errors,
                "loops": self.loops,
                "dropped": source.dropped if source else 0,
                "queued": round(self.queued_seconds(), 4)}

    def __repr__(self):
        return ("<AudioPlayer %s %.3fs/%.3fs %s>"
                % (self.info.codec if self.info else "none", self.position(),
                   self.duration, "playing" if self._playing else "paused"))
