"""HTMLMediaElement: the player's half of it, and the DOM bridge's half.

The interesting thing about this interface is that almost none of it is a
value -- it is an order. A page that shows a spinner until `canplay` and hides
it on `playing` is correct only if those two arrive in that order, and a page
that resets its scrubber on `ended` is correct only if `pause` got there
first. So most of what is checked below is a sequence of event names rather
than a number, and the sequences are the ones written down in the HTML
specification rather than the ones this implementation happens to produce.

Two layers, tested separately because they fail separately. `media.VideoPlayer`
owes the document a list of event names and knows nothing about a DOM; the
bridge in `jsdom.py` turns that list into handler calls. A test that only went
through JavaScript could not tell a throttle that fires at the wrong rate from
a dispatcher that swallows events.

Every clip here is built in memory by `media_fixtures`, and every clock is a
`ManualClock`, so nothing in this file depends on wall time or on a codec
being installed.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import media_fixtures

from feetbrowser import media
from feetbrowser.browser import Tab, tree_to_list
from feetbrowser.htmlparser import Element
from feetbrowser.jsdom import adopt_media_state
from feetbrowser.net import URL
from feetbrowser.window import Tk


def eq(a, b, msg=""):
    assert a == b, f"{msg}: {a!r} != {b!r}"


def _clip(count=10, width=8, height=6, fps=10.0):
    """An uncompressed AVI of `count` flat-coloured frames.

    The pictures do not matter to anything in this file -- what matters is
    that the container reports a real duration and frame rate, because that is
    what `duration`, `ended` and the whole timeupdate schedule are computed
    from.
    """
    def painter(i):
        return lambda x, y: (i * 7 % 256, 0, 0)
    frames = [media_fixtures.rgb24_frame(width, height, painter(i))
              for i in range(count)]
    return media_fixtures.avi(frames, width, height, fps=fps)


def _player(count=10, fps=10.0, loop=False, **kwargs):
    clock = media.ManualClock()
    player = media.VideoPlayer(data=_clip(count=count, fps=fps), clock=clock,
                               threaded=False, loop=loop, decode_budget=8,
                               **kwargs)
    assert player.track is not None, player.error
    return player, clock


def _events(player):
    return " ".join(player.drain_events())


# -- the player: what it owes the document ----------------------------------

def test_opening_a_file_owes_loadedmetadata_then_canplay():
    """Both, in that order, even though the file was already in memory when
    the player was constructed. A page that waits for `canplay` before
    enabling its play button has to be given one."""
    player, _clock = _player()
    eq(_events(player), "loadedmetadata canplay",
       "the load sequence, in the specification's order")
    eq(player.ready_state, media.HAVE_ENOUGH_DATA,
       "a file wholly in memory is not partly buffered")
    eq(player.paused, True, "loading does not start playback")
    eq(player.ended, False, "nor finish it")


def test_play_fires_play_then_playing_and_only_when_it_was_paused():
    player, _clock = _player()
    _events(player)
    player.play()
    eq(_events(player), "play playing",
       "play() promises playback, then reports it has begun")
    player.play()
    eq(_events(player), "", "playing an already-playing element is a no-op")


def test_pause_fires_pause_only_when_something_was_playing():
    player, _clock = _player()
    _events(player)
    player.pause()
    eq(_events(player), "", "pausing a paused element changes nothing")
    player.play()
    _events(player)
    player.pause()
    eq(_events(player), "pause", "and pausing a playing one fires once")


def test_timeupdate_is_throttled_to_about_four_a_second():
    """Twenty-five ticks across one second of media. The clip runs at 10 fps
    so the position moves on most of them, and firing per tick would give
    twenty-five events; the specification caps the rate and every shipping
    browser lands on four."""
    player, clock = _player(count=40, fps=10.0)
    player.play()
    _events(player)
    fired = []
    for step in range(1, 26):
        clock.set(step * 0.04)
        player.tick()
        fired.extend(player.drain_events())
    eq(fired, ["timeupdate"] * 4,
       "one second of playback is four timeupdates and nothing else")


def test_timeupdate_does_not_fire_while_the_position_is_still():
    player, clock = _player(count=40)
    player.play()
    _events(player)
    clock.set(1.0)
    player.tick()
    eq(_events(player), "timeupdate", "time passed once")
    for _ in range(10):
        player.tick()
    eq(_events(player), "",
       "ten more ticks at the same instant are not ten more updates")


def test_running_off_the_end_fires_timeupdate_pause_then_ended():
    """The order people get wrong. Reaching the end of the media really does
    pause the element, and the `pause` event is part of the sequence -- a page
    that toggles a button on `pause` and again on `ended` has to see both."""
    player, clock = _player(count=10, fps=10.0)
    player.play()
    _events(player)
    clock.set(5.0)
    player.tick()
    eq(_events(player), "timeupdate pause ended",
       "the end-of-media sequence")
    eq(player.ended, True, "and the element is ended")
    eq(player.paused, True, "and paused, which is the surprising half")
    player.tick()
    eq(_events(player), "", "the end happens once, not on every later tick")


def test_playing_again_after_the_end_rewinds_first():
    """Step two of the play() algorithm, and the reason a play button next to
    a finished clip starts it over instead of sitting there."""
    player, clock = _player(count=10, fps=10.0)
    player.play()
    clock.set(5.0)
    player.tick()
    _events(player)
    player.play()
    eq(_events(player), "seeking timeupdate seeked play playing",
       "the rewind is a seek, and it happens before play")
    eq(player.position(), 0.0, "back at the start")
    eq(player.ended, False, "and no longer ended")


def test_a_looping_clip_wraps_as_a_seek_and_never_ends():
    player, clock = _player(count=10, fps=10.0, loop=True)
    player.play()
    _events(player)
    clock.set(1.5)
    player.tick()
    eq(_events(player), "seeking timeupdate seeked",
       "a wrap is a seek to the start, not an end")
    eq(player.ended, False, "a looping clip does not end")
    assert player.position() < 1.0, player.position()


def test_seeking_fires_seeking_timeupdate_seeked_unthrottled():
    """The seek algorithm's `timeupdate` is a step of that algorithm, not the
    "time marches on" one, so the four-a-second throttle must not swallow it:
    a scrubber that asks for 0.4s and is told 0.0s for another 240 ms jumps
    back under the user's finger."""
    player, clock = _player(count=40, fps=10.0)
    player.play()
    _events(player)
    for seconds in (0.4, 0.8, 1.2):
        player.seek(seconds)
        eq(_events(player), "seeking timeupdate seeked",
           "every seek reports itself, however close together")
    assert abs(player.position() - 1.2) < 1e-9, player.position()
    eq(player.seeking, False,
       "the file is in memory, so no seek is ever outstanding")


def test_playback_rate_moves_the_playhead_and_fires_ratechange():
    player, clock = _player(count=100, fps=10.0)
    player.play()
    clock.set(1.0)
    player.tick()
    _events(player)
    player.set_playback_rate(2.0)
    eq(_events(player), "ratechange", "the rate moved")
    clock.set(2.0)
    player.tick()
    assert abs(player.position() - 3.0) < 1e-9, \
        "a second of clock at double rate is two seconds of media: %r" \
        % player.position()
    _events(player)
    player.set_playback_rate(2.0)
    eq(_events(player), "", "assigning the rate it already has is not a change")


def test_a_rate_of_zero_stops_the_playhead_without_pausing():
    player, clock = _player(count=100, fps=10.0)
    player.play()
    player.set_playback_rate(0.0)
    clock.set(2.0)
    player.tick()
    eq(player.position(), 0.0, "no media time passed")
    eq(player.paused, False,
       "but the element is not paused: `paused` is about play(), not motion")


def test_volume_and_muted_stay_separate_and_gain_folds_them():
    """Unmuting has to restore the level the user chose, so `muted` cannot be
    implemented by writing zero into `volume`."""
    player, _clock = _player()
    _events(player)
    player.set_volume(0.25)
    eq(_events(player), "volumechange", "the level moved")
    eq(player.gain(), 0.25, "gain is the volume while unmuted")
    player.set_muted(True)
    eq(_events(player), "volumechange", "muting is a volume change too")
    eq(player.volume, 0.25, "and does not disturb the level")
    eq(player.gain(), 0.0, "though nothing should be audible")
    player.set_muted(True)
    eq(_events(player), "", "muting a muted element is not a change")
    player.set_muted(False)
    eq(player.gain(), 0.25, "unmuting restores what was chosen")


def test_load_returns_to_the_start_and_owes_the_load_events_again():
    player, clock = _player(count=40, fps=10.0)
    player.play()
    clock.set(1.0)
    player.tick()
    _events(player)
    player.load()
    eq(_events(player), "pause loadedmetadata canplay",
       "load() stops playback and begins the element again")
    eq(player.position(), 0.0, "back at the beginning")
    eq(player.paused, True, "and not playing")


def test_can_play_type_answers_about_the_container_then_the_codec():
    """"maybe" is the truthful answer to a container name: whether a .mov
    plays depends on what is inside it, and the only way to find out is to
    open the file. A `codecs=` parameter we recognise turns that into
    "probably"; audio is always "" because nothing here makes a sound."""
    eq(media.can_play_type("video/x-msvideo"), "maybe", "a container we open")
    eq(media.can_play_type("video/quicktime"), "maybe", "and another")
    eq(media.can_play_type('video/quicktime; codecs="jpeg"'), "probably",
       "a codec we decode all the way to pixels")
    eq(media.can_play_type('video/mp4; codecs="avc1.42E01E"'), "",
       "a container this browser does not open")
    eq(media.can_play_type("audio/mpeg"), "",
       "no audio decoder, so no honest answer but no")
    eq(media.can_play_type(""), "", "and nothing at all is not a type")
    eq(media.can_play_type("VIDEO/X-MSVIDEO"), "maybe",
       "MIME types are case-insensitive")


class _FakeAudio:
    """Stands in for the audio track that does not exist yet, to check that
    every transport path reaches it. This is the contract `attach_audio`
    documents and nothing else in the tree implements."""

    def __init__(self):
        self.calls = []

    def start(self, position):
        self.calls.append(("start", round(position, 6)))

    def stop(self):
        self.calls.append(("stop",))

    def seek(self, position):
        self.calls.append(("seek", round(position, 6)))

    def set_gain(self, gain):
        self.calls.append(("set_gain", gain))

    def set_rate(self, rate):
        self.calls.append(("set_rate", rate))
        return True

    def position(self):
        return None


def test_the_audio_seam_is_told_about_every_transport_change():
    player, clock = _player(count=40, fps=10.0)
    track = _FakeAudio()
    player.set_volume(0.5)
    assert player.attach_audio(track), "attaching a track reports success"
    eq(track.calls, [("set_gain", 0.5), ("set_rate", 1.0)],
       "a track is handed the state that already exists")
    track.calls = []
    player.play()
    player.seek(1.0)
    player.set_muted(True)
    player.set_playback_rate(1.5)
    player.pause()
    eq(track.calls,
       [("start", 0.0), ("seek", 1.0), ("set_gain", 0.0), ("set_rate", 1.5),
        ("stop",)],
       "start, seek, gain, rate and stop all arrive")


def test_the_end_of_the_media_stops_the_audio_seam_too():
    player, clock = _player(count=10, fps=10.0)
    track = _FakeAudio()
    player.attach_audio(track)
    player.play()
    track.calls = []
    clock.set(5.0)
    player.tick()
    eq(track.calls, [("stop",)],
       "running off the end is a stop the device has to hear about")


# -- the bridge: what a script sees -----------------------------------------

_WATCH = """
window.log = "";
function watch(el) {
  var names = ["loadedmetadata", "canplay", "play", "playing", "pause",
               "timeupdate", "seeking", "seeked", "ended", "volumechange",
               "ratechange"];
  names.forEach(function (n) {
    el.addEventListener(n, function (e) { window.log += e.type + " "; });
  });
}
"""


def _tab(markup, script="", url="https://example.com/page"):
    tab = Tab(700)
    address = URL(url)
    tab.url = address
    tab._build(address, "<body>%s<script>%s</script></body>"
               % (markup, _WATCH + script), "text/html")
    return tab


def _node(tab, tag="video"):
    return next(n for n in tree_to_list(tab.nodes, [])
                if isinstance(n, Element) and n.tag == tag)


def _attach(tab, node, count=10, fps=10.0):
    """Give an element a player the way `Tab._finish_video` does, but on a
    clock the test owns."""
    clock = media.ManualClock()
    player = media.VideoPlayer(data=_clip(count=count, fps=fps), clock=clock,
                               threaded=False, decode_budget=8,
                               loop="loop" in node.attributes)
    assert player.track is not None, player.error
    player.first_frame()
    node.video_player = player
    tab.video_players.append(player)
    if node not in tab._media_nodes:
        tab._media_nodes.append(node)
    player.muted = "muted" in node.attributes
    adopt_media_state(node, player)
    return player, clock


def _run(tab, code):
    tab._js_interp.run(code)
    tab._drain_js()


def _log(tab):
    """The events fired since the last read, oldest first."""
    text = tab._js_interp.globals.get("log") or ""
    tab._js_interp.run("window.log = '';")
    return text.strip()


def _text(tab, name):
    return tab._js_interp.globals.get(name) or ""


def _value(tab, expression):
    tab._js_interp.run("window._v = (%s);" % expression)
    return tab._js_interp.globals.get("_v")


def test_a_normal_load_and_play_fires_the_specification_sequence():
    """The whole point of the exercise, end to end: markup, a script that
    subscribes, a file arriving, a play() and a run to the end."""
    tab = _tab('<video id="v" src="clip.avi"></video>',
               'watch(document.getElementById("v"));')
    node = _node(tab)
    player, clock = _attach(tab, node, count=10, fps=10.0)
    tab._drain_js()
    eq(_log(tab), "loadedmetadata canplay",
       "the file arriving is what the page hears about first")
    _run(tab, 'document.getElementById("v").play();')
    eq(_log(tab), "play playing", "then the transport")
    clock.set(0.3)
    tab.tick_videos()
    eq(_log(tab), "timeupdate", "then time passing, throttled")
    clock.set(5.0)
    tab.tick_videos()
    eq(_log(tab), "timeupdate pause ended", "and then the end")
    eq(_value(tab, 'document.getElementById("v").ended'), True,
       "which the element agrees with")
    eq(_value(tab, 'document.getElementById("v").paused'), True,
       "on both counts")


def test_the_onplay_property_and_a_listener_both_fire():
    tab = _tab('<video id="v" src="clip.avi"></video>', """
        var v = document.getElementById("v");
        watch(v);
        window.slot = 0;
        v.onplay = function (e) { window.slot += 1; window.type = e.type; };
    """)
    node = _node(tab)
    player, _clock = _attach(tab, node)
    tab._drain_js()
    _log(tab)
    _run(tab, 'document.getElementById("v").play();')
    eq(_log(tab), "play playing", "the listener ran")
    eq(tab._js_interp.globals.get("slot"), 1, "and the property slot once")
    eq(tab._js_interp.globals.get("type"), "play",
       "with an event carrying its type")


def test_an_onplay_content_attribute_runs_and_the_property_replaces_it():
    tab = _tab('<video id="v" src="clip.avi" onplay="window.hits = 1;">'
               '</video>')
    node = _node(tab)
    _attach(tab, node)
    tab._drain_js()
    _run(tab, 'document.getElementById("v").play();')
    eq(tab._js_interp.globals.get("hits"), 1, "the markup handler ran")
    _run(tab, """
        var v = document.getElementById("v");
        v.pause();
        v.onplay = function () { window.hits = 2; };
        v.play();
    """)
    eq(tab._js_interp.globals.get("hits"), 2,
       "and assigning the property replaced it rather than adding to it")


def test_media_events_do_not_bubble():
    tab = _tab('<div id="d"><video id="v" src="clip.avi"></video></div>', """
        window.bubbled = 0;
        document.getElementById("d").addEventListener(
            "play", function () { window.bubbled += 1; });
        window.bubbles = null;
        document.getElementById("v").addEventListener(
            "play", function (e) { window.bubbles = e.bubbles; });
    """)
    node = _node(tab)
    _attach(tab, node)
    tab._drain_js()
    _run(tab, 'document.getElementById("v").play();')
    eq(tab._js_interp.globals.get("bubbled"), 0,
       "the parent heard nothing, which is what the specification says")
    eq(tab._js_interp.globals.get("bubbles"), False,
       "and the event says so about itself")


def test_setting_currentTime_seeks_and_reads_back():
    tab = _tab('<video id="v" src="clip.avi"></video>',
               'watch(document.getElementById("v"));')
    node = _node(tab)
    player, _clock = _attach(tab, node, count=40, fps=10.0)
    tab._drain_js()
    _log(tab)
    _run(tab, 'document.getElementById("v").currentTime = 1.5;')
    eq(_log(tab), "seeking timeupdate seeked", "the seek sequence")
    assert abs(_value(tab, 'document.getElementById("v").currentTime') - 1.5) \
        < 1e-6, "the playhead is where the script put it"
    assert abs(player.position() - 1.5) < 1e-9, "and so is the player"


def test_volume_outside_the_range_throws_index_size_error():
    """The one part of this interface that surprises people who have only read
    the getter: an out-of-range volume is an error, not a clamp."""
    tab = _tab('<video id="v" src="clip.avi"></video>')
    _attach(tab, _node(tab))
    tab._drain_js()
    _run(tab, """
        var v = document.getElementById("v");
        window.caught = "";
        try { v.volume = 1.5; } catch (e) { window.caught = "" + e; }
        window.after = v.volume;
        window.ok = "";
        try { v.volume = -0.1; } catch (e) { window.ok = "" + e; }
    """)
    assert "IndexSizeError" in _text(tab, "caught"), \
        tab._js_interp.globals.get("caught")
    assert "IndexSizeError" in _text(tab, "ok"), \
        tab._js_interp.globals.get("ok")
    eq(tab._js_interp.globals.get("after"), 1.0,
       "and the throw left the value alone rather than half-applying it")


def test_volume_and_muted_from_script_reach_the_player():
    tab = _tab('<video id="v" src="clip.avi"></video>',
               'watch(document.getElementById("v"));')
    node = _node(tab)
    player, _clock = _attach(tab, node)
    tab._drain_js()
    _log(tab)
    _run(tab, """
        var v = document.getElementById("v");
        v.volume = 0.5;
        v.muted = true;
    """)
    eq(_log(tab), "volumechange volumechange", "one event each")
    eq(player.volume, 0.5, "the level reached the player")
    eq(player.muted, True, "and so did the mute")
    eq(player.gain(), 0.0, "which is what an output device would be told")
    eq(_value(tab, 'document.getElementById("v").volume'), 0.5,
       "and the level is still readable while muted")


def test_playbackRate_from_script_reaches_the_player():
    tab = _tab('<video id="v" src="clip.avi"></video>',
               'watch(document.getElementById("v"));')
    node = _node(tab)
    player, clock = _attach(tab, node, count=100, fps=10.0)
    tab._drain_js()
    _log(tab)
    _run(tab, """
        var v = document.getElementById("v");
        v.playbackRate = 2;
        v.play();
    """)
    eq(_log(tab), "ratechange play playing", "the rate change, then the play")
    clock.set(1.0)
    tab.tick_videos()
    assert abs(player.position() - 2.0) < 1e-9, player.position()
    eq(_value(tab, 'document.getElementById("v").playbackRate'), 2.0,
       "and it reads back")


def test_loop_reflects_the_attribute_and_reaches_the_scheduler():
    tab = _tab('<video id="v" src="clip.avi"></video>')
    node = _node(tab)
    player, _clock = _attach(tab, node)
    tab._drain_js()
    eq(_value(tab, 'document.getElementById("v").loop'), False,
       "no attribute, no loop")
    _run(tab, 'document.getElementById("v").loop = true;')
    eq(node.attributes.get("loop"), "", "the content attribute is set")
    eq(player.loop, True, "the decoder wraps")
    eq(player.scheduler.loop, True, "and so does the clock")
    _run(tab, 'document.getElementById("v").loop = false;')
    assert "loop" not in node.attributes, "and unsetting removes it"


def test_autoplay_reflects_but_does_not_start_playback():
    """Assigning `autoplay` after the file is loaded reflects the attribute
    and stops there, because there is no autoplay algorithm here to rerun."""
    tab = _tab('<video id="v" src="clip.avi"></video>')
    node = _node(tab)
    player, _clock = _attach(tab, node)
    tab._drain_js()
    _run(tab, 'document.getElementById("v").autoplay = true;')
    eq(node.attributes.get("autoplay"), "", "the attribute is there")
    eq(_value(tab, 'document.getElementById("v").autoplay'), True,
       "and reads back")
    eq(player.paused, True, "but nothing started")


def test_videoWidth_is_intrinsic_on_video_and_absent_on_audio():
    tab = _tab('<video id="v" src="clip.avi"></video>'
               '<audio id="a" src="tune.wav"></audio>')
    node = _node(tab, "video")
    _attach(tab, node)
    tab._drain_js()
    eq(_value(tab, 'document.getElementById("v").videoWidth'), 8,
       "the size comes out of the container, not the layout box")
    eq(_value(tab, 'document.getElementById("v").videoHeight'), 6,
       "likewise")
    eq(_value(tab, 'typeof document.getElementById("v").videoWidth'), "number",
       "a video has the property")
    eq(_value(tab, 'typeof document.getElementById("a").videoWidth'),
       "undefined",
       "an audio element does not, which is how a page feature-tests for one")


def test_src_reflects_the_markup_and_currentSrc_the_resource():
    tab = _tab('<video id="v"><source src="clip.avi" '
               'type="video/x-msvideo"></video>')
    node = _node(tab)
    eq(_value(tab, 'document.getElementById("v").src'), "",
       "the author put no src on the element, so neither does the browser")
    eq(_value(tab, 'document.getElementById("v").currentSrc'), "",
       "and nothing has been selected yet")
    _attach(tab, node)
    tab._drain_js()
    eq(_value(tab, 'document.getElementById("v").currentSrc'),
       "https://example.com/clip.avi",
       "once the bytes are here, currentSrc is the resolved URL that won")
    eq(_value(tab, 'document.getElementById("v").src'), "",
       "and src still reflects the markup")


def test_setting_src_writes_the_attribute():
    tab = _tab('<video id="v" src="clip.avi"></video>')
    node = _node(tab)
    _run(tab, 'document.getElementById("v").src = "other.avi";')
    eq(node.attributes.get("src"), "https://example.com/other.avi",
       "the write reaches the attribute the fetch path reads, resolved by the"
       " same pass that resolves one the parser found")
    eq(_value(tab, 'document.getElementById("v").src'),
       "https://example.com/other.avi", "and reads back absolute")


def test_canPlayType_from_script():
    tab = _tab('<video id="v" src="clip.avi"></video>'
               '<audio id="a" src="tune.wav"></audio>')
    eq(_value(tab, 'document.getElementById("v").canPlayType('
                   '"video/x-msvideo")'), "maybe", "a container we open")
    eq(_value(tab, 'document.getElementById("v").canPlayType("video/webm")'),
       "", "one we do not")
    eq(_value(tab, 'document.getElementById("a").canPlayType("audio/mpeg")'),
       "", "and nothing an audio element could be asked about")


def test_the_readyState_constants_are_on_the_element():
    tab = _tab('<video id="v" src="clip.avi"></video>')
    node = _node(tab)
    eq(_value(tab, 'document.getElementById("v").HAVE_NOTHING'), 0,
       "the constants a page compares readyState against")
    eq(_value(tab, 'document.getElementById("v").HAVE_ENOUGH_DATA'), 4,
       "and the far end of the scale")
    eq(_value(tab, 'document.getElementById("v").readyState'), 0,
       "nothing has been fetched yet")
    _attach(tab, node)
    tab._drain_js()
    eq(_value(tab, 'document.getElementById("v").readyState'), 4,
       "and a file wholly in memory is not partly buffered")


def test_an_element_with_no_media_reads_as_have_nothing():
    """The state every `<video>` is in between parse and the file arriving,
    and the state an `<audio>` is in for ever, because nothing in this browser
    decodes a sample. Pages are already written to survive it."""
    tab = _tab('<audio id="a" src="tune.wav"></audio>')
    eq(_value(tab, 'document.getElementById("a").readyState'), 0,
       "no media")
    eq(_value(tab, 'document.getElementById("a").paused'), True,
       "a media element that has never played is paused")
    eq(_value(tab, 'document.getElementById("a").ended'), False,
       "and has not ended")
    eq(_value(tab, 'document.getElementById("a").currentTime'), 0.0,
       "and sits at the start")
    eq(_value(tab, 'document.getElementById("a").volume'), 1.0,
       "at full volume")
    eq(_value(tab, 'isNaN(document.getElementById("a").duration)'), True,
       "with a duration of NaN, which is the point of NaN")


def test_state_set_before_the_file_arrives_survives_the_player():
    """A script configuring a video runs while the file is still on the wire,
    which is several hundred milliseconds before there is anything to
    configure. Losing that would be a bug that only appeared on slow
    connections."""
    tab = _tab('<video id="v" src="clip.avi"></video>',
               'watch(document.getElementById("v"));')
    node = _node(tab)
    _run(tab, """
        var v = document.getElementById("v");
        v.volume = 0.25;
        v.muted = true;
        v.playbackRate = 1.5;
        v.currentTime = 0.5;
    """)
    eq(_log(tab), "volumechange volumechange ratechange",
       "the element owed the document these before it had a player")
    player, _clock = _attach(tab, node, count=40, fps=10.0)
    eq(player.volume, 0.25, "the level was adopted")
    eq(player.muted, True, "and the mute")
    eq(player.playback_rate, 1.5, "and the rate")
    assert abs(player.position() - 0.5) < 1e-9, "and the start position"
    tab._drain_js()
    eq(_log(tab), "loadedmetadata canplay seeking timeupdate seeked",
       "and the seek to the assigned position comes after the metadata,"
       " because until there is a duration there is nowhere to seek to")


def test_muted_in_the_markup_starts_the_player_muted():
    tab = _tab('<video id="v" src="clip.avi" muted></video>')
    node = _node(tab)
    eq(_value(tab, 'document.getElementById("v").muted'), True,
       "the content attribute seeds the property")
    player, _clock = _attach(tab, node)
    eq(player.muted, True, "and the player starts that way")
    eq(player.gain(), 0.0, "with nothing audible")


def test_play_returns_a_promise_and_rejects_without_a_source():
    """`v.play().catch(...)` is how essentially every autoplaying page is
    written, so play() returning undefined would take the rest of the script
    with it."""
    tab = _tab('<video id="v" src="clip.avi"></video>'
               '<audio id="a" src="tune.wav"></audio>')
    node = _node(tab)
    _attach(tab, node)
    tab._drain_js()
    _run(tab, """
        window.resolved = 0;
        window.rejected = "";
        document.getElementById("v").play().then(function () {
            window.resolved += 1;
        });
        document.getElementById("a").play().catch(function (e) {
            window.rejected = "" + e;
        });
    """)
    tab._drain_js()
    eq(tab._js_interp.globals.get("resolved"), 1,
       "a video that can play resolves")
    assert "NotSupportedError" in _text(tab, "rejected"), \
        tab._js_interp.globals.get("rejected")


def test_load_from_script_rewinds_and_reloads():
    tab = _tab('<video id="v" src="clip.avi"></video>',
               'watch(document.getElementById("v"));')
    node = _node(tab)
    player, clock = _attach(tab, node, count=40, fps=10.0)
    tab._drain_js()
    _run(tab, 'document.getElementById("v").play();')
    clock.set(1.0)
    tab.tick_videos()
    _log(tab)
    _run(tab, 'document.getElementById("v").load();')
    eq(_log(tab), "pause loadedmetadata canplay",
       "load() ends the current playback and starts the element again")
    eq(_value(tab, 'document.getElementById("v").currentTime'), 0.0,
       "back at the beginning")


def test_a_handler_that_queues_another_event_is_answered_in_the_same_drain():
    """A `play` handler calling `pause()` is how a play/pause button is
    written. The `pause` it asks for arrives after the events already in
    flight, but in the same drain rather than a frame later."""
    tab = _tab('<video id="v" src="clip.avi"></video>', """
        var v = document.getElementById("v");
        watch(v);
        v.addEventListener("play", function () { v.pause(); });
    """)
    node = _node(tab)
    player, _clock = _attach(tab, node)
    tab._drain_js()
    _log(tab)
    _run(tab, 'document.getElementById("v").play();')
    eq(_log(tab), "play playing pause",
       "the handler's pause follows the events that were already queued")
    eq(player.paused, True, "and it really did pause")


def test_a_handler_that_queues_an_event_every_time_cannot_spin():
    """The other half of the same decision: a page that turns every `pause`
    into a `play` is a page written by mistake, and it must not be able to
    keep the UI thread. It gets a bounded number of rounds and the rest of
    what it is owed on the next drain."""
    tab = _tab('<video id="v" src="clip.avi"></video>', """
        var v = document.getElementById("v");
        window.hits = 0;
        v.addEventListener("play", function () { window.hits += 1; v.pause(); });
        v.addEventListener("pause", function () { v.play(); });
    """)
    node = _node(tab)
    _attach(tab, node)
    tab._drain_js()
    _run(tab, 'document.getElementById("v").play();')
    hits = tab._js_interp.globals.get("hits")
    assert 0 < hits <= 8, "the loop is bounded, not endless: %r" % hits


def main():
    root = Tk(); root.withdraw()
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  ok  {t.__name__}")
        except Exception as e:
            failed += 1
            import traceback; traceback.print_exc()
            print(f" FAIL {t.__name__}: {e}")
    if failed:
        print(f"\n{failed} FAILED")
        sys.exit(1)
    print(f"\nALL {len(tests)} MEDIA API TESTS PASSED")


if __name__ == "__main__":
    main()
