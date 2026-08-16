"""Tests for the browser's side of the audio seam.

The audio stack itself -- the ring, the filter design, the resampler, the
mixer, the clock, the three platform backends and the live half that plays a
tone through a real speaker -- lives in ``feetplayer`` now, and is tested
there. What is left here is the part that is about a browser: a `<video>`
element that has a soundtrack, and the pictures that have to follow it.

So these are integration tests of a seam, and they are deliberately on this
side of it. `media.Scheduler` decides which picture is due, and it decides it
against a clock that is `feetplayer.arch.AudioPlayer`'s idea of where the
sound is; `browser.Tab` is what builds the two and wires them together out of
one downloaded file. Neither repo can test that alone. A known audio timeline
goes in, and the question asked is which picture is on screen -- not whether
the two objects can be connected.

One of them decodes real AAC out of a real MP4 with the Fortran decoder, which
is the case that fails if the arch reports the device's timeline instead of
the stream's, or the demuxer hands the audio track the video track's chunk
offsets. Every other test in either tree would still pass.

A null device that consumes from the ring exactly the way a real one does
carries all of it, so none of this needs a sound card.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import media_fixtures
from feetbrowser import browser, media
from feetplayer import aac, arch, heel, mediacodec


def eq(a, b, msg=""):
    assert a == b, "%s: %r != %r" % (msg, a, b)


def close(a, b, tolerance, msg=""):
    assert abs(a - b) <= tolerance, "%s: %r != %r (+-%g)" % (msg, a, b,
                                                             tolerance)


# -- the clock -------------------------------------------------------------

def test_the_clock_is_what_a_media_scheduler_wants():
    """Duck-compatible with media.Clock on purpose, so that A/V sync can be
    handed one with no adapter at all."""
    clock = heel.AudioClock(48000)
    scheduler = media.Scheduler(10.0, 25.0, clock=clock)
    assert scheduler.clock is clock, "media took a clock and kept another"
    scheduler.play()
    clock.frames += 48000
    close(scheduler.position(), 1.0, 1e-9,
          "a second of audio should be a second of video")


# -- sources, end to end ---------------------------------------------------

class Capture(heel.NullDevice):
    """A null device that keeps what it consumed instead of dropping it.

    Implementing the whole device contract in twenty lines is the point: it
    is what lets the end-to-end tests measure the bytes a driver would have
    been handed, on a machine with no driver.
    """

    name = "capture"

    def __init__(self, rate=48000, channels=2, fmt=heel.FLOAT32):
        super().__init__(rate, channels, fmt, paced=False)
        self.taken = bytearray()

    def pump(self, frames):
        data = self._ring.read(frames * self.frame_bytes)
        self.taken.extend(data)
        got = len(data) // self.frame_bytes
        self._clock.frames += frames
        if got < frames:
            self.taken.extend(b"\0" * ((frames - got) * self.frame_bytes))
            self._clock.silent_frames += frames - got
            self._clock.underruns += 1
        return got

    def floats(self):
        return heel.floats_from_float32(bytes(self.taken))


def _drive(output, frames, block=512):
    """Mix and consume ``frames``, the way the two threads would have."""
    done = 0
    while done < frames:
        step = min(block, frames - done)
        output.pump()
        output.device.pump(step)
        done += step


# -- restart(): a seek is a new timeline through the same speaker -----------

def test_a_source_nobody_restarts_reports_exactly_what_it_always_did():
    """The identity case. `restart()` added two fields to every source in the
    process, and a source that never seeks must not notice them."""
    device = Capture(48000, 2)
    output = heel.Output(device, ring_frames=4800, threaded=False)
    source = output.add_source(48000, channels=1)
    eq(source._origin_pulled, 0, "a fresh source starts at the origin")
    eq(source._origin_at, 0.0, "a fresh source starts at zero seconds")
    source.write([0.1] * 48000, fmt="float")
    output.start()
    device.pump(2400)
    close(source.position(), 0.05, 1e-9, "the untouched timeline moved")
    output.close()


# -- the arch: playing a decoded stream, and the pictures that follow it ----
#
# No codec anywhere in here. `FakeAudioTrack` answers exactly the questions
# `mediacodec.AudioTrack` answers and puts the frame's own index into its
# samples, so "which part of the file came out of the speaker" is a question
# the bytes the device was handed answer by themselves. The AAC decoder has
# its own suite; what these are about is the wire between it and the heel,
# and a wire is best tested with a signal you chose.

class FakeAudioTrack:
    """An `AudioTrack` over a constant, without a codec near it.

    Frame `i` is `(i + 1) / 1000.0` in every sample, so a captured buffer
    says which frames were played, in what order, and where the silence was.
    `channels_at` makes one frame come back with a channel count that
    disagrees with the stream's, which is a real thing a broken file does and
    the one thing that must never reach the interleaver.
    """

    def __init__(self, count=200, sample_rate=48000, channels=1,
                 per_frame=1024, channels_at=None):
        self.sample_rate = sample_rate
        self.channels = channels
        self.per_frame = per_frame
        self.sample_count = count
        self.duration = count * per_frame / float(sample_rate)
        self.channels_at = dict(channels_at or {})
        self.container = "fake"
        self.codec_name = "pcm"
        self.asc = b""
        self.reads = []
        self.info = mediacodec.AudioInfo("fake", "pcm", sample_rate, channels,
                                         self.duration, count, True)

    def frame_time(self, index):
        return index * self.per_frame / float(self.sample_rate)

    def frame_duration(self, index):
        return self.per_frame / float(self.sample_rate)

    def index_at(self, seconds):
        index = int(seconds * self.sample_rate) // self.per_frame
        return max(0, min(index, self.sample_count - 1))

    def packet(self, index):
        return b""

    def frame(self, index):
        if not 0 <= index < self.sample_count:
            raise mediacodec.MediaError("audio frame %d out of range" % index)
        self.reads.append(index)
        channels = self.channels_at.get(index, self.channels)
        value = (index + 1) / 1000.0
        return mediacodec.AudioFrame(
            index, self.frame_time(index), self.frame_duration(index),
            self.sample_rate, channels,
            heel.pack([value] * (self.per_frame * channels), heel.FLOAT32))

    def reset(self):
        pass


def _player(output, track=None, **kwargs):
    return arch.AudioPlayer(track=track or FakeAudioTrack(), output=output,
                            threaded=False, **kwargs)


def _audible(device, ring_frames=4800):
    """An unthreaded output over `device` that claims to be real hardware.

    `Capture` is a `NullDevice` subclass -- that is how it gets the whole
    device contract in twenty lines -- so `Output` calls it silent, and the
    sync path declines to follow a device nobody can hear. In these tests it
    stands in for a sound card, and this is where it says so.
    """
    output = heel.Output(device, ring_frames=ring_frames, threaded=False)
    output.silent = False
    return output


# -- A/V sync: the pictures follow the sound --------------------------------

def _clip(count=100, width=8, height=6, fps=10.0):
    """An uncompressed AVI whose frame `i` is the flat colour (i, 0, 0), so
    that which picture is on screen is a question the screen answers."""
    frames = [media_fixtures.rgb24_frame(width, height,
                                         lambda x, y, i=i: (i, 0, 0))
              for i in range(count)]
    return media_fixtures.avi(frames, width, height, fps=fps)


def test_the_pictures_are_scheduled_against_the_sound():
    """The load-bearing one. A known audio timeline in, and the question is
    which picture is due -- not whether the two objects can be connected.

    The last third is the control. The same assertions are made again with a
    second of offset put into the sound's timeline by hand, and they have to
    go the other way; assertions that pass against a deliberately broken
    clock are not assertions about the clock.
    """
    device = Capture(48000, 2)
    output = _audible(device)
    audio = _player(output, track=FakeAudioTrack(count=400))
    video = media.VideoPlayer(data=_clip(count=100, fps=10.0),
                              threaded=False, decode_budget=8)
    assert video.attach_audio(audio), "the sound should be driving"
    assert isinstance(video.scheduler.clock, media._AudioClock)
    # Half a frame in, so that every measurement below lands in the middle of
    # a picture rather than on the seam between two of them.
    video.seek(0.05)
    video.play()
    audio.pump(200)
    device.pump(output.ring.backlog)        # the silence start() primed with
    close(video.position(), 0.05, 1e-6, "the picture is not where the sound is")

    shown = []
    for step in range(1, 21):
        _drive(output, 4800, block=480)     # exactly 100 ms of sound
        close(audio.position(), 0.05 + step * 0.1, 1e-6, "the sound drifted")
        video.tick()
        audio.pump(200)
        current = video.scheduler.current
        assert current is not None, "nothing on screen at step %d" % step
        eq(current.index, video.scheduler.due_index(),
           "step %d: the picture is not the one the sound is due" % step)
        shown.append(current.index)
    eq(shown, list(range(1, 21)), "the pictures did not follow the sound")
    eq(video.stats()["starved"], 0, "the pictures could not keep up")

    # The control. One second of offset, which is ten pictures and more than
    # the decode queue can hide.
    pos, media_at, rate = audio._segments[0]
    audio._segments[0] = (pos, media_at + 1.0, rate)
    _drive(output, 4800, block=480)
    video.tick()
    assert video.scheduler.current.index != video.scheduler.due_index(), \
        ("a second of offset in the sound went unnoticed, so the assertion "
         "above proves nothing")
    video.close()
    audio.close()
    output.close()


AAC_FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "fixtures", "aac")


def _real_aac_packets(name="lowrate", repeats=6):
    """The coded frames of a committed AAC vector, and its config.

    Taken apart with `aac.adts_frames()` rather than with our MP4 demuxer, so
    that a fixture built out of them is not built by the code it is about to
    be used to test. Repeating the run is what makes the clip long enough to
    measure a drift over; the samples that come back are still real decoded
    AAC, which is the part that matters here.
    """
    with open(os.path.join(AAC_FIXTURES, name + ".aac"), "rb") as handle:
        blob = handle.read()
    packets = []
    rest = blob
    for head, length in aac.adts_frames(blob):
        packets.append(rest[head:length])
        rest = rest[length:]
    return packets * repeats, aac.asc_from_adts(blob)


def _av_clip(seconds=1.9, fps=25.0, width=16, height=12):
    """One MP4 with both tracks real: Motion JPEG pictures whose red channel
    counts the frames, and AAC the Fortran decoder will actually decode."""
    packets, asc = _real_aac_packets()
    per_frame = 1024 / 44100.0
    packets = packets[:max(1, int(round(seconds / per_frame)))]
    count = max(1, int(round(len(packets) * per_frame * fps)))
    frames = [media_fixtures.jpeg(width, height,
                                  (lambda i: lambda x, y: (i * 5 % 256, 60,
                                                           120))(i))
              for i in range(count)]
    return media_fixtures.mp4_av(frames, width, height, packets, asc=asc,
                                 fps=fps)


def test_one_file_with_both_codecs_in_it_plays_in_sync():
    """The end-to-end case the suite could not make before: a single file,
    both halves decoded by our own code, and the pictures scheduled against
    the samples that came out of the sound decoder.

    Everything else in this section proves a half. `_clip()` is pictures with
    a `FakeAudioTrack` beside it, which is the clock without the codec; the
    AAC tests over in feetplayer are the codec without the clock. This is the
    one that fails if the two are wired together wrongly -- if the arch
    reports the device timeline instead of the stream's, or the demuxer hands
    the audio track the video track's chunk offsets, both repositories would
    still pass every other test they have.
    """
    if not aac.available():
        print("  skipping: %s" % aac.unavailable_reason())
        return
    data = _av_clip()
    # One file, and both probes have to find their own track in it.
    picture = mediacodec.probe(data)
    sound = mediacodec.probe_audio(data)
    assert picture.supported and sound.supported, (picture, sound)
    eq((sound.sample_rate, sound.channels), (44100, 2))

    device = Capture(44100, 2)
    output = _audible(device)
    audio = arch.AudioPlayer(data=data, output=output, threaded=False)
    assert not audio.error, audio.error
    assert not audio.silent, "a file with an AAC track came back silent"
    video = media.VideoPlayer(data=data, threaded=False, decode_budget=8)
    assert not video.error, video.error
    assert video.attach_audio(audio), "the sound should be driving"
    assert isinstance(video.scheduler.clock, media._AudioClock)

    video.seek(0.02)                    # inside a picture, not on its seam
    video.play()
    audio.pump(200)
    device.pump(output.ring.backlog)    # the silence start() primed with

    shown = []
    for step in range(1, 16):
        _drive(output, 4410, block=441)          # exactly 100 ms of sound
        close(audio.position(), 0.02 + step * 0.1, 1e-3,
              "the decoded sound drifted from its own timeline")
        video.tick()
        audio.pump(200)
        current = video.scheduler.current
        assert current is not None, "nothing on screen at step %d" % step
        eq(current.index, video.scheduler.due_index(),
           "step %d: the picture is not the one the sound is due" % step)
        shown.append(current.index)
    eq(shown, sorted(shown), "the pictures went backwards")
    assert shown[-1] > shown[0], "the pictures never advanced"
    eq(video.stats()["starved"], 0, "the pictures could not keep up")
    eq(video.stats()["decode_errors"], 0)
    eq(audio.decode_errors, 0, "a committed AAC vector failed to decode")
    assert audio.decoded > 0, "nothing was decoded, so nothing was proved"

    # The bytes the device was handed are the decoder's, not silence: a file
    # that demuxed and then played nothing would satisfy every timing
    # assertion above.
    assert device.taken.count(0) < len(device.taken), \
        "the device was handed nothing but silence"

    # The control, the same one the fake-clock test uses. Offset the sound's
    # timeline and the picture must stop being the one that is due.
    pos, media_at, rate = audio._segments[0]
    audio._segments[0] = (pos, media_at + 1.0, rate)
    _drive(output, 4410, block=441)
    video.tick()
    assert video.scheduler.current.index != video.scheduler.due_index(), \
        ("a second of offset in the sound went unnoticed, so the assertions "
         "above prove nothing")
    video.close()
    audio.close()
    output.close()


def test_a_video_with_no_sound_is_exactly_the_video_it_was():
    """Nothing in the sync path is allowed to reach a file with no audio in
    it, or a machine with no device."""
    clock = media.ManualClock()
    video = media.VideoPlayer(data=_clip(count=20, fps=10.0), clock=clock,
                              threaded=False, decode_budget=8)
    assert video.audio is None
    assert video.scheduler.clock is clock, "something replaced the clock"
    video.play()
    shown = []
    for step in range(20):
        clock.set(step * 0.1)
        if video.tick():
            shown.append(video.scheduler.current.index)
    eq(shown, list(range(20)))
    eq(video.stats()["dropped"], 0)
    close(video.position(), 1.9, 1e-9, "the clock is the position")
    video.close()


def test_a_device_nobody_can_hear_is_not_allowed_to_drive_the_pictures():
    """The paced null device keeps perfect time, which is exactly why it is
    tempting. It is also what a machine gets when its sound card has just
    been unplugged, and a video whose clock is a device nobody can hear is a
    video with a new way to stop. So the offer is declined and the clock the
    player was made with keeps the picture moving, as it always did."""
    clock = media.ManualClock()
    output = heel.open_output(backend="null", threaded=False)
    audio = _player(output)
    assert audio.silent, "the null backend should say it is silent"
    video = media.VideoPlayer(data=_clip(count=20, fps=10.0), clock=clock,
                              threaded=False, decode_budget=8)
    eq(video.attach_audio(audio), False, "a silent device took the clock")
    assert video.audio is None
    assert video.scheduler.clock is clock, "the clock was replaced anyway"
    video.play()
    clock.set(0.55)
    eq(video.scheduler.due_index(), 5, "the manual clock is not driving")
    for _ in range(3):
        video.tick()
    eq(video.scheduler.current.index, 5, "the pictures stopped moving")
    video.close()
    audio.close()
    output.close()


def test_detaching_the_sound_hands_the_pictures_back_to_their_own_clock():
    device = Capture(48000, 2)
    output = _audible(device)
    audio = _player(output)
    clock = media.ManualClock()
    video = media.VideoPlayer(data=_clip(count=100, fps=10.0), clock=clock,
                              threaded=False, decode_budget=8)
    assert video.attach_audio(audio)
    video.play()
    audio.pump(200)
    device.pump(output.ring.backlog)
    _drive(output, 4800, block=480)
    close(video.position(), 0.1, 1e-6, "the sound is not driving")
    eq(video.detach_audio(), False, "detach_audio should answer False")
    assert video.audio is None
    assert video.scheduler.clock is clock, "it kept a clock we did not give it"
    close(video.position(), 0.1, 1e-6, "the playhead jumped on detaching")
    clock.advance(0.5)
    close(video.position(), 0.6, 1e-9, "the manual clock is not driving")
    assert not audio.playing, "detaching left the sound running"
    video.close()
    audio.close()
    output.close()


def test_a_seek_moves_the_sound_before_it_re_origins_the_pictures():
    """`Scheduler.seek()` re-origins itself against the clock, and the clock
    is the sound. Seeking the sound afterwards leaves every picture scheduled
    against where the sound used to be -- by exactly the size of the seek."""
    device = Capture(48000, 2)
    output = _audible(device)
    audio = _player(output, track=FakeAudioTrack(count=400))
    video = media.VideoPlayer(data=_clip(count=100, fps=10.0),
                              threaded=False, decode_budget=8)
    assert video.attach_audio(audio)
    video.play()
    audio.pump(200)
    device.pump(output.ring.backlog)
    video.seek(4.05)
    close(video.position(), 4.05, 1e-6, "the seek did not take the pictures")
    close(audio.position(), 4.05, 1e-6, "the seek did not take the sound")
    audio.pump(200)
    _drive(output, 4800, block=480)
    close(video.position(), 4.15, 1e-6, "the pictures did not resume")
    video.tick()
    eq(video.scheduler.current.index, video.scheduler.due_index(),
       "the picture after a seek is not the one the sound is due")
    eq(video.scheduler.current.index, 41)
    video.close()
    audio.close()
    output.close()


# -- the browser: a <video> element asking for its own sound ----------------

KEY = "http://example.invalid/clip.avi"


class _TabStub:
    """The parts of a `Tab` that building a player out of bytes touches.

    A whole `Tab` wants a window, a network stack and a laid-out page. The
    real `Tab._finish_video`, `Tab._build_players` and
    `Tab._attach_video_audio` are then called unbound against this, so what
    is under test is the shipping code and not a paraphrase of it.
    """

    def __init__(self):
        self.audio_players = []
        self.video_players = []
        self.errors = []
        self.browser = None
        self._video_queue = []
        self._video_nodes = {}

    _attach_video_audio = browser.Tab._attach_video_audio
    _build_players = browser.Tab._build_players

    def _add_error(self, text):
        self.errors.append(text)

    def finish(self, data, node):
        """Take delivery of a downloaded file, as the download thread's
        drain does. Returns the `VideoPlayer` built for it."""
        self._video_nodes[KEY] = [node]
        self._video_queue.append(KEY)
        browser.Tab._finish_video(self, KEY, data)
        return self.video_players[-1] if self.video_players else None


class _ElementStub:
    """A `<video>` node, as far as building a player cares: its attributes."""

    def __init__(self, **attributes):
        self.attributes = dict(attributes)


def test_a_video_element_with_no_sound_is_left_exactly_as_it_was():
    """The common case, and the one that must not regress: an AVI with no
    audio stream. No device is opened, nothing is said, and the pictures are
    still driven by the clock they were built with."""
    tab, node = _TabStub(), _ElementStub()
    video = tab.finish(_clip(count=20, fps=10.0), node)
    assert video is not None, "the pictures did not survive the attempt"
    eq(tab.errors, [], "silence is not an error")
    eq(tab.audio_players, [], "a soundless video kept an audio player")
    assert video.audio is None, "a soundless video was given a soundtrack"
    assert video.scheduler.clock is video._own_clock, \
        "a soundless video had its clock taken away"
    assert video.first_frame() or video.scheduler.current is not None, \
        "the first picture never appeared"
    video.close()


def test_a_soundtrack_we_cannot_decode_is_said_out_loud():
    """An AVI that names an MP3 stream we have no decoder for. The pictures
    still play; the page says why they are silent."""
    tab, node = _TabStub(), _ElementStub()
    frames = [media_fixtures.rgb24_frame(8, 6, lambda x, y, i=i: (i, 0, 0))
              for i in range(20)]
    data = media_fixtures.avi(frames, 8, 6, fps=10.0,
                              audio={"format_tag": 0x0055, "channels": 2,
                                     "sample_rate": 44100, "length": 441000})
    video = tab.finish(data, node)
    assert video is not None, "an undecodable soundtrack took the pictures too"
    eq(tab.audio_players, [], "we cannot decode MP3")
    eq(len(tab.errors), 1, "a track we could name and not play went unsaid")
    assert tab.errors[0].startswith("AUDIO "), tab.errors[0]
    assert video.audio is None
    assert video.scheduler.clock is video._own_clock
    video.close()


def test_the_browser_hands_a_video_the_sound_that_came_with_it():
    """The wire itself. What `arch.AudioPlayer` decodes is tested over in
    feetplayer; what is tested here is that the element's own attributes reach
    it, that the tab keeps hold of it, and that the pictures end up on its
    clock."""
    device = Capture(48000, 2)
    output = _audible(device)
    made = []
    # Bound before the patch goes in, or the stand-in calls itself.
    real = arch.AudioPlayer

    def fake_player(data=None, loop=False):
        player = real(track=FakeAudioTrack(count=400), output=output,
                      threaded=False, loop=loop)
        made.append(player)
        return player

    tab = _TabStub()
    node = _ElementStub(loop="", muted="")
    arch.AudioPlayer = fake_player
    try:
        video = tab.finish(_clip(count=20, fps=10.0), node)
    finally:
        arch.AudioPlayer = real
    eq(len(made), 1, "the element did not ask for a player")
    audio = made[0]
    eq(tab.audio_players, [audio], "the player built is not the one attached")
    assert audio.loop, "the element's loop did not reach the sound"
    assert audio.muted, "the element's muted did not reach the sound"
    eq(tab.audio_players, [audio], "the tab did not keep hold of the sound")
    assert node.audio_player is audio, "the element cannot find its own sound"
    assert video.audio is audio, "the video was not told about the sound"
    assert isinstance(video.scheduler.clock, media._AudioClock), \
        "the pictures are still on the wall clock"
    eq(tab.errors, [], "a soundtrack that worked was complained about")
    video.close()
    audio.close()
    output.close()


def test_a_tab_that_goes_away_lets_go_of_its_sound():
    """`stop_videos()` is what a navigation calls. A daemon decode thread
    still filling a ring for a page nobody is on is a leak that makes a
    noise."""
    tab = _TabStub()
    device = Capture(48000, 2)
    output = _audible(device)
    audio = _player(output, track=FakeAudioTrack(count=400))
    audio.play()
    tab.audio_players.append(audio)
    tab.video_players = []
    tab._video_queue = []
    tab._video_nodes = {}
    browser.Tab.stop_videos(tab)
    eq(tab.audio_players, [], "the tab is still holding its audio players")
    assert not audio.playing, "the sound is still playing after the page went"
    assert audio.source is None, "the source outlived the page"
    output.close()


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    only = [a for a in sys.argv[1:] if not a.startswith("-")]
    for test in tests:
        if only and test.__name__ not in only:
            continue
        try:
            test()
            print("  ok  %s" % test.__name__, flush=True)
        except Exception as exc:
            failed += 1
            import traceback
            traceback.print_exc()
            print(" FAIL %s: %s" % (test.__name__, exc), flush=True)
    heel.close_all()
    if failed:
        print("\n%d FAILED" % failed)
        sys.exit(1)
    print("\nALL %d AUDIO TESTS PASSED" % len(tests))


if __name__ == "__main__":
    main()
