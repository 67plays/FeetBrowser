"""The DOM bridge for the JavaScript engine.

The bridge is the set of host objects a script sees as `document`,
`location`, elements, node lists and so on. It belongs to the Rust engine:
the logic lives in `rust/src/dom.rs` and the shims in `jsdom_rust.py` call
straight into that extension, so the Tab wires it in directly.

Three host objects do not depend on the engine at all and live here rather
than in the bridge. `_JSStaticProps` and `_JSComputedStyle` read from plain
Python dicts and from the cascaded `node.style`, never from the DOM tree.
`HTMLMediaElement` is the third and is the odd one: it is an *element*
interface, so it is reached through `JSElement`, but every name on it resolves
against a `media.VideoPlayer` rather than against the DOM, and none of it is
anything `dom.rs` could answer. `JSElement` below is the Rust element shim
with those names taken off the front of `js_get`/`js_set`; everything it does
not recognise goes straight through to the extension exactly as before, and
the name test is a set membership so an ordinary `el.className` pays a hash
lookup for the privilege.
"""

from . import jsengine
from .jsdom_rust import (
    JSDocument, JSLocation, JSElement as _BridgeElement, JSNodeList,
    JSClassList, JSFontFaceSet, JSElementStyle, JSFragment,
)
from .media import (
    HAVE_NOTHING, HAVE_METADATA, HAVE_CURRENT_DATA, HAVE_FUTURE_DATA,
    HAVE_ENOUGH_DATA, can_play_type,
)

__all__ = ["JSDocument", "JSLocation", "JSElement", "JSNodeList",
           "JSClassList", "JSFontFaceSet", "JSElementStyle", "JSFragment",
           "MEDIA_TAGS", "MEDIA_EVENTS", "dispatch_media_events"]

UNDEFINED = jsengine.UNDEFINED

# The two elements that get the interface. `<source>` and `<track>` are not
# media elements -- they are children of one -- and giving them `play()`
# because they live in the same part of the parser would be a lie a page can
# trip over.
MEDIA_TAGS = ("video", "audio")

# Every event this bridge knows how to fire. The set is what decides whether
# `video.onplay = f` is a handler slot or an ordinary expando, so it is also
# the honest list of what a page can rely on: `loadstart`, `durationchange`,
# `loadeddata`, `canplaythrough`, `waiting`, `stalled`, `emptied`, `progress`,
# `suspend`, `abort` and `error` are not fired by anything here, and a page
# that waits on one of them waits for ever.
MEDIA_EVENTS = (
    "loadedmetadata", "canplay", "play", "playing", "pause", "timeupdate",
    "seeking", "seeked", "ended", "volumechange", "ratechange",
)

_MEDIA_HANDLER_PROPS = frozenset("on" + name for name in MEDIA_EVENTS)

_MEDIA_METHODS = frozenset(("play", "pause", "load", "canPlayType"))

_MEDIA_PROPS = frozenset((
    "currentTime", "duration", "paused", "ended", "loop", "autoplay",
    "muted", "volume", "playbackRate", "readyState", "seeking",
    "videoWidth", "videoHeight", "src", "currentSrc",
    "HAVE_NOTHING", "HAVE_METADATA", "HAVE_CURRENT_DATA", "HAVE_FUTURE_DATA",
    "HAVE_ENOUGH_DATA",
))

# The whole of the interface, in one set, because the point of the test in
# `JSElement.js_get` is to be a single miss for every name that is not ours.
_MEDIA_NAMES = _MEDIA_METHODS | _MEDIA_PROPS | _MEDIA_HANDLER_PROPS

_READY_STATES = {
    "HAVE_NOTHING": HAVE_NOTHING, "HAVE_METADATA": HAVE_METADATA,
    "HAVE_CURRENT_DATA": HAVE_CURRENT_DATA,
    "HAVE_FUTURE_DATA": HAVE_FUTURE_DATA,
    "HAVE_ENOUGH_DATA": HAVE_ENOUGH_DATA,
}

_NAN = float("nan")


def _is_media(node):
    return getattr(node, "tag", None) in MEDIA_TAGS


class JSElement(_BridgeElement):
    """One Element node, with `HTMLMediaElement` layered over `<video>` and
    `<audio>`.

    Elements are wrapped afresh for every property access -- `dom.rs` builds a
    new one out of the node each time -- so this class holds no state of its
    own beyond the interface object it caches for the duration of one wrap.
    Anything that has to outlive a read lives on the node or on the player.
    """

    def js_get(self, name):
        if name in _MEDIA_NAMES and _is_media(self.node):
            return self._media().get(name)
        return _BridgeElement.js_get(self, name)

    def js_set(self, name, value):
        if name in _MEDIA_NAMES and _is_media(self.node):
            handled = self._media().set(name, value)
            if handled:
                return UNDEFINED
        return _BridgeElement.js_set(self, name, value)

    def _media(self):
        media = getattr(self, "_media_iface", None)
        if media is None:
            media = _HTMLMediaElement(self)
            self._media_iface = media
        return media


def media_state(node):
    """The `HTMLMediaElement` state that belongs to the element rather than
    to the player, created on first use.

    Two elements need it. An `<audio>` never gets a player at all -- nothing
    in this tree decodes a sample -- and a `<video>` does not get one until
    its file has been fetched, which is several hundred milliseconds after the
    scripts that configure it have run. Both cases have to answer `volume` and
    remember what was assigned to it, and neither has anywhere else to put the
    answer.

    `events` is the same story for the queue: a player carries its own, but
    the element has to be able to owe the document a `volumechange` before
    there is a player to owe it from.
    """
    state = getattr(node, "_media_state", None)
    if state is None:
        state = {
            "volume": 1.0,
            # The content attribute seeds the IDL attribute and then stops
            # mattering: `muted` is live state, not a reflection, which is why
            # `defaultMuted` is a separate name in the specification.
            "muted": "muted" in getattr(node, "attributes", {}),
            "playbackRate": 1.0,
            # The specification calls this the default playback start
            # position: assigning `currentTime` with no media loaded does not
            # seek, it records where the seek will land once there is
            # something to seek in.
            "currentTime": 0.0,
            "events": [],
            "handlers": {},
        }
        node._media_state = state
    return state


def adopt_media_state(node, player):
    """Hand a freshly built player whatever the element was already holding.

    Called once, by the Tab, at the moment a player is attached. Without it a
    page that sets `video.volume = 0.2` in a `<script>` -- which runs while
    the file is still on the wire -- would have that silently undone when the
    bytes landed, and the bug would only show up on a slow connection.
    """
    state = getattr(node, "_media_state", None)
    if state is None:
        return False
    player.volume = state["volume"]
    player.muted = state["muted"]
    player.playback_rate = state["playbackRate"]
    player.scheduler.set_rate(state["playbackRate"])
    if state["currentTime"]:
        player.seek(state["currentTime"])
    # The events queued before the player existed go first: they happened
    # first, and a page that sees `volumechange` after `loadedmetadata` would
    # reasonably conclude something changed the volume, which nothing did.
    player.events.extendleft(reversed(state["events"]))
    del state["events"][:]
    return True


def dispatch_media_events(node, interp):
    """Fire every event the element owes, in order, at the element itself.

    Media events do not bubble -- this is the one place in the browser where
    that matters, because `_dispatch_js_event` walks to the root and a
    `timeupdate` arriving at `document.body` four times a second would be
    both wrong and expensive.

    Handlers run outside the interpreter rather than inside it: this is
    reached from the Tab's drain, after whatever script queued the event has
    finished, which is as close to the specification's "queue a media element
    task" as a browser without a real task queue gets. The visible difference
    is that `v.play()` returns before its `play` event fires here too, but the
    handler runs at the end of the current drain rather than at the end of the
    current task.
    """
    state = media_state(node)
    pending = state["events"]
    player = getattr(node, "video_player", None)
    if player is not None:
        pending.extend(player.drain_events())
    if not pending or interp is None:
        return False
    # Taken by value: a handler is allowed to call play() and queue more, and
    # those belong to the next drain rather than to this loop, which would
    # otherwise be a page's way of never giving the UI thread back.
    firing, state["events"] = pending[:], []
    for name in firing:
        _fire_media_event(node, name, interp)
    return True


def _fire_media_event(node, name, interp):
    event = {
        "type": name,
        "target": JSElement(node),
        "currentTarget": JSElement(node),
        "bubbles": False,
        "cancelable": False,
        "defaultPrevented": False,
        "isTrusted": True,
        "preventDefault": lambda *a: UNDEFINED,
        "stopPropagation": lambda *a: UNDEFINED,
        "stopImmediatePropagation": lambda *a: UNDEFINED,
    }
    for handler in list(getattr(node, "_js_handlers", {}).get(name, [])):
        _run_handler(interp, handler, event)
    slot = media_state(node)["handlers"].get(name)
    if slot is not None:
        _run_handler(interp, slot, event)
    elif getattr(node, "attributes", {}).get("on" + name):
        # Only when the property slot is empty, because the content attribute
        # and the `onplay` property are one slot in the specification and
        # assigning the property is how a page replaces the markup's handler.
        try:
            interp.run(node.attributes["on" + name])
        except jsengine.JSException as exc:
            interp.logs.append(f"JS error: {exc}")


def _run_handler(interp, handler, event):
    try:
        interp.call(handler, event)
    except jsengine.JSException as exc:
        interp.logs.append(f"JS error: {exc}")


class _HTMLMediaElement:
    """The `HTMLMediaElement` half of a `<video>` or `<audio>` element.

    Every name resolves against the `media.VideoPlayer` attached to the node,
    and the interesting design question is what to answer when there is not
    one -- which is the normal state for `<audio>` for as long as this browser
    has no audio, and the state of every `<video>` between parse and the
    file arriving.

    The answer is: exactly what the specification says an element with
    `readyState` of `HAVE_NOTHING` answers. `duration` is NaN, `paused` is
    true, `ended` is false, `currentTime` reads back whatever was assigned,
    and a seek fires no events because the seek algorithm returns at its
    second step. That is not a graceful-degradation story invented here; it is
    the state a real browser is in for the first frames of every page with a
    video on it, and pages are already written to survive it.
    """

    def __init__(self, element):
        self.element = element
        self.node = element.node
        self._methods = {}

    @property
    def player(self):
        return getattr(self.node, "video_player", None)

    @property
    def state(self):
        return media_state(self.node)

    # -- reads --------------------------------------------------------------

    def get(self, name):
        if name in _MEDIA_METHODS:
            method = self._methods.get(name)
            if method is None:
                # Cached so `el.play === el.play` and, more usefully, so
                # `removeEventListener`-shaped code that stores a method and
                # compares it later is not defeated by a fresh bound method
                # every read. `dom.rs` caches its own native methods for the
                # same reason.
                method = getattr(self, "_" + name)
                self._methods[name] = method
            return method
        if name in _READY_STATES:
            return _READY_STATES[name]
        if name.startswith("on"):
            return self.state["handlers"].get(name[2:], UNDEFINED)
        return getattr(self, "_get_" + name)()

    def _get_currentTime(self):
        player = self.player
        if player is None:
            return self.state["currentTime"]
        return player.position()

    def _get_duration(self):
        player = self.player
        return _NAN if player is None else player.duration

    def _get_paused(self):
        player = self.player
        return True if player is None else player.paused

    def _get_ended(self):
        player = self.player
        return False if player is None else player.ended

    def _get_seeking(self):
        player = self.player
        return False if player is None else player.seeking

    def _get_readyState(self):
        player = self.player
        return HAVE_NOTHING if player is None else player.ready_state

    def _get_loop(self):
        return "loop" in self.node.attributes

    def _get_autoplay(self):
        return "autoplay" in self.node.attributes

    def _get_muted(self):
        player = self.player
        return self.state["muted"] if player is None else player.muted

    def _get_volume(self):
        player = self.player
        return self.state["volume"] if player is None else player.volume

    def _get_playbackRate(self):
        player = self.player
        return (self.state["playbackRate"] if player is None
                else player.playback_rate)

    def _get_videoWidth(self):
        return self._intrinsic(0)

    def _get_videoHeight(self):
        return self._intrinsic(1)

    def _intrinsic(self, axis):
        """`videoWidth`/`videoHeight` exist on `<video>` and nowhere else, so
        on an `<audio>` the honest answer is that the property is absent
        rather than zero -- `typeof el.videoWidth` is how a page asks whether
        the element it was handed has pictures, and zero would say yes.

        Zero until metadata is known, which the specification requires and
        which is also all we could say: the size comes out of the container.
        """
        if self.node.tag != "video":
            return UNDEFINED
        player = self.player
        if player is None or player.ready_state < HAVE_METADATA:
            return 0
        return (player.width, player.height)[axis]

    def _get_src(self):
        """The content attribute, resolved -- except when it was never there.

        `_absolutize_media_srcs` hoists the chosen `<source>` onto the element
        so the rest of the browser only has to look in one place, which is a
        good trade everywhere except here: `src` reflects markup the author
        wrote, and reporting a URL the author put on a child element would
        make `video.src` and `video.getAttribute("src")` disagree with the
        page. `currentSrc` is the property that is allowed to know.
        """
        if getattr(self.node, "media_src_hoisted", False):
            return ""
        return self.node.attributes.get("src", "")

    def _get_currentSrc(self):
        """Empty until a resource has actually been selected, which here means
        until the bytes arrived and a player was built from them. A page using
        `currentSrc` to find out which `<source>` won gets the answer only
        once there is a real answer."""
        if self.player is None:
            return ""
        return self.node.attributes.get("src", "")

    # -- writes -------------------------------------------------------------

    def set(self, name, value):
        """True when this interface took the write; False hands it back to the
        element bridge, which is how `src` still lands in the attribute
        dictionary the fetch path reads."""
        if name.startswith("on") and name[2:] in MEDIA_EVENTS:
            event = name[2:]
            if value is None or value is UNDEFINED:
                self.state["handlers"].pop(event, None)
            else:
                self.state["handlers"][event] = value
            return True
        setter = getattr(self, "_set_" + name, None)
        if setter is None:
            return False
        setter(value)
        return True

    def _set_currentTime(self, value):
        seconds = _number(value)
        if seconds is None:
            return
        self.state["currentTime"] = max(0.0, seconds)
        player = self.player
        if player is not None:
            player.seek(seconds)

    def _set_volume(self, value):
        """Out of range throws rather than clamping, which is the one piece of
        this interface that surprises people who have only read the getter.
        The specification is explicit: a volume outside 0..1 is an
        `IndexSizeError`, because a page that computes a level and gets it
        wrong wants to find out, not to have the browser quietly decide."""
        level = _number(value)
        if level is None or level != level or level < 0.0 or level > 1.0:
            raise ValueError(
                "IndexSizeError: The volume provided (%s) is outside the"
                " range [0, 1]." % (value,))
        player = self.player
        if player is None:
            if level != self.state["volume"]:
                self.state["volume"] = level
                self.state["events"].append("volumechange")
            return
        player.set_volume(level)
        self.state["volume"] = player.volume

    def _set_muted(self, value):
        wanted = _truthy(value)
        player = self.player
        if player is None:
            if wanted != self.state["muted"]:
                self.state["muted"] = wanted
                self.state["events"].append("volumechange")
            return
        player.set_muted(wanted)
        self.state["muted"] = player.muted

    def _set_playbackRate(self, value):
        rate = _number(value)
        if rate is None or rate != rate:
            return
        player = self.player
        if player is None:
            if rate != self.state["playbackRate"]:
                self.state["playbackRate"] = rate
                self.state["events"].append("ratechange")
            return
        player.set_playback_rate(rate)
        self.state["playbackRate"] = player.playback_rate

    def _set_loop(self, value):
        self._reflect_boolean("loop", value)
        player = self.player
        if player is not None:
            player.set_loop(_truthy(value))

    def _set_autoplay(self, value):
        """Reflects the attribute and stops there. Assigning `autoplay` after
        the file has loaded does not start it playing, because the
        specification starts playback from the autoplay algorithm and there is
        no autoplay algorithm here -- `load_videos()` reads the attribute once
        and that is all. A page that wants playback calls `play()`."""
        self._reflect_boolean("autoplay", value)

    def _reflect_boolean(self, attribute, value):
        if _truthy(value):
            self.node.attributes[attribute] = ""
        else:
            self.node.attributes.pop(attribute, None)
        self.element._flag["dirty"] = True

    # -- methods ------------------------------------------------------------

    def _play(self, *args):
        """Returns a promise, already settled.

        The specification has it resolve when playback actually begins and
        reject with a `NotSupportedError` when the element cannot play at all,
        and both of those are knowable here the instant `play()` is called:
        the file is either decoded and in memory or it is not. The reason to
        return a promise rather than nothing is that
        `video.play().catch(function () {})` is how essentially every page
        that autoplays is written, and a `play()` returning undefined turns
        that line into a TypeError that takes the rest of the script with it.
        """
        player = self.player
        promise = self._promise()
        if player is None or player.track is None:
            reason = "NotSupportedError: The element has no supported sources."
            if promise is not None:
                promise.reject(reason)
            return promise if promise is not None else UNDEFINED
        player.play()
        if promise is not None:
            promise.resolve(UNDEFINED)
        return promise

    def _pause(self, *args):
        player = self.player
        if player is not None:
            player.pause()
        return UNDEFINED

    def _load(self, *args):
        player = self.player
        if player is not None:
            player.load()
        else:
            # No player, so nothing to reset except the position a script may
            # have assigned; the specification's load algorithm ends with the
            # element back at the beginning either way.
            self.state["currentTime"] = 0.0
        return UNDEFINED

    def _canPlayType(self, content_type=None, *args):
        if content_type is None or content_type is UNDEFINED:
            return ""
        if self.node.tag == "audio":
            # Not a shortcut around `can_play_type`: it already answers ""
            # for `audio/*`, and this catches the page that offers an
            # `<audio type="video/quicktime">`, which we still cannot play
            # because we would have nowhere to send the sound.
            return ""
        return can_play_type(str(content_type))

    def _promise(self):
        interp = self.element._flag.get("interp")
        if interp is None:
            return None
        return interp.create_promise()


def _number(value):
    """JS `ToNumber`, near enough: `true` is 1 and a string that is not a
    number is not a number. None back means "no usable value", which every
    caller treats as "leave the property alone" rather than as zero -- the
    difference matters for `currentTime`, where zero is a seek to the start
    and a page assigning it an undefined variable did not mean to rewind."""
    if value is None or value is UNDEFINED:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _truthy(value):
    if value is None or value is UNDEFINED or value is False:
        return False
    if value == 0 or value == "":
        return False
    return True


class _JSStaticProps:
    """Read-only property bag for environment globals (navigator, screen,
    matchMedia results): property reads return the captured dict, methods and
    writes are inert."""

    def __init__(self, props):
        self._props = dict(props)

    def js_get(self, name):
        if name in self._props:
            return self._props[name]
        return jsengine.UNDEFINED

    def js_set(self, name, value):
        return jsengine.UNDEFINED

    def js_call(self, *args):
        return jsengine.UNDEFINED


class _JSComputedStyle:
    """Bridge for `getComputedStyle(el)`: camelCase reads and
    `getPropertyValue()` resolve against the cascaded node.style dict."""

    def __init__(self, node):
        self.node = node
        self._style = getattr(node, "style", {}) if node is not None else {}

    def _snapshot(self):
        # Re-read so restyles after mutations are reflected.
        return getattr(self.node, "style", {}) if self.node is not None else {}

    def js_get(self, name):
        if name in ("getPropertyValue", "setProperty", "getPropertyPriority"):
            return getattr(self, "_" + name)
        if name == "cssText":
            return "; ".join(
                f"{k}: {v}" for k, v in self._snapshot().items())
        value = self._snapshot().get(_kebab(name))
        if value is None:
            return ""
        return value

    def js_set(self, name, value):
        return jsengine.UNDEFINED

    def _getPropertyValue(self, prop):
        value = self._snapshot().get(str(prop))
        if value is None:
            return ""
        return value

    def _setProperty(self, *args):
        return jsengine.UNDEFINED

    def _getPropertyPriority(self, *args):
        return ""


def _kebab(name):
    out = []
    for ch in name:
        if ch.isupper():
            out.append("-")
            out.append(ch.lower())
        else:
            out.append(ch)
    return "".join(out)
