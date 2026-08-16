"""The browser itself, running inside a real Cocoa window.

doormat carries its own tests for whether an NSWindow opens, translates
NSEvents and gets a framebuffer onto the screen. It cannot carry these:
doormat has no idea a browser exists, so nothing over there notices when
a click stops reaching the tab strip, when Cmd-L stops focusing the
address bar, or when a drag on the scrollbar stops moving the page.
doormat proves a window works; this proves the browser works in one,
which is the seam between the two packages and the only thing that can
quietly come apart. So these tests open an actual NSWindow, run an
actual Browser in it, and post actual NSEvents at it. Nothing is stubbed.

Skipped with a clear message on any platform that is not macOS.
"""
import ctypes
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _skip(reason):
    print("SKIP test_cocoa.py: %s" % reason)
    sys.exit(0)


if sys.platform != "darwin":
    _skip("not macOS")

from feetbrowser import browser as browsermod  # noqa: E402
from doormat import cocoa  # noqa: E402

if not cocoa.available():
    _skip("AppKit is not loadable here")


# -- synthetic input -------------------------------------------------------

def _window_number(win):
    return cocoa.msg(win._window, "windowNumber", restype=ctypes.c_long)


def make_mouse(win, kind, x, y, flags=0):
    """A real mouse NSEvent at canvas (top-left) coordinates."""
    _width, height = win._content_size()
    location = cocoa.NSPoint(float(x), float(height - y))
    event = cocoa.msg(
        cocoa._cls("NSEvent"),
        "mouseEventWithType:location:modifierFlags:timestamp:windowNumber:"
        "context:eventNumber:clickCount:pressure:",
        kind, location, flags, 0.0, _window_number(win), None, 0, 1, 1.0,
        argtypes=(ctypes.c_ulonglong, cocoa.NSPoint, ctypes.c_ulonglong,
                  ctypes.c_double, ctypes.c_long, ctypes.c_void_p,
                  ctypes.c_long, ctypes.c_long, ctypes.c_float))
    assert event, "could not build a mouse NSEvent"
    return event


def send_mouse(win, kind, x, y, flags=0):
    """Hand a real mouse NSEvent straight to the translator.

    Mouse events do not survive the queue with their location intact: once the
    app is active, AppKit re-resolves a posted event's location against where
    the physical cursor happens to be, which is not something a test can pin
    down. Every test here asserts on *where* the click landed -- which tab
    strip button, which pixel of the scrollbar -- so the events go in at
    ``_translate``, and ``post_key`` is left to prove the queue delivers.
    """
    win._translate(make_mouse(win, kind, x, y, flags))


def post_key(win, chars, keycode, flags=0):
    """Queue a real key-down NSEvent."""
    text = cocoa.nsstring(chars)
    event = cocoa.msg(
        cocoa._cls("NSEvent"),
        "keyEventWithType:location:modifierFlags:timestamp:windowNumber:"
        "context:characters:charactersIgnoringModifiers:isARepeat:keyCode:",
        cocoa._KEY_DOWN, cocoa.NSPoint(0.0, 0.0), flags, 0.0,
        _window_number(win), None, text, text, False, keycode,
        argtypes=(ctypes.c_ulonglong, cocoa.NSPoint, ctypes.c_ulonglong,
                  ctypes.c_double, ctypes.c_long, ctypes.c_void_p,
                  ctypes.c_void_p, ctypes.c_void_p, ctypes.c_bool,
                  ctypes.c_ushort))
    assert event, "could not build a key NSEvent"
    cocoa.msg(win._app, "postEvent:atStart:", event, True,
              argtypes=(ctypes.c_void_p, ctypes.c_bool))


def pump(win, times=6):
    for _ in range(times):
        win.poll_events()


class _Session:
    """A live window, torn down however the test ends."""

    def __enter__(self):
        self.win = cocoa.CocoaTk(width=900, height=600, title="test")
        # Drain the events a window makes on its way up. Until they are gone
        # the app is still activating, and AppKit re-resolves the location of
        # the first event posted into it -- a real app has the same warm-up,
        # it just spends it in the run loop instead.
        pump(self.win, 8)
        return self.win

    def __exit__(self, *_exc):
        self.win.destroy()
        return False


class _Browser(_Session):
    """A live window driving a real Browser, on about:blank only."""

    def __enter__(self):
        win = super().__enter__()
        self.browser = browsermod.Browser(win)
        self.browser.new_tab("about:blank")
        self.browser.draw()
        win.present()
        return self.browser


# -- the browser, driven for real ------------------------------------------

def test_clicking_the_new_tab_button_opens_a_tab():
    """The regression that started this file: a stray attribute error in the
    mouse path swallowed every click, and nothing else looked wrong."""
    with _Browser() as br:
        before = len(br.tabs)
        x = br._new_tab_x() + browsermod.NEW_TAB_W / 2
        # The tab strip is the 40px band under whatever chrome the toes drew.
        y = browsermod.toes.band_height(br.chrome_bands()) + 20
        send_mouse(br.window, cocoa._LEFT_DOWN, x, y)
        send_mouse(br.window, cocoa._LEFT_UP, x, y)
        assert len(br.tabs) == before + 1, \
            "clicking + did not open a tab (%d -> %d)" % (before,
                                                          len(br.tabs))


def test_keyboard_shortcuts_reach_the_browser():
    with _Browser() as br:
        before = len(br.tabs)
        post_key(br.window, "t", 17, cocoa._MOD_COMMAND)
        pump(br.window)
        assert len(br.tabs) == before + 1, "Cmd-T did not open a tab"
        post_key(br.window, "l", 37, cocoa._MOD_COMMAND)
        pump(br.window)
        assert br.focus == "address", "Cmd-L did not focus the address bar"


def test_typing_into_the_address_bar_works():
    with _Browser() as br:
        post_key(br.window, "l", 37, cocoa._MOD_COMMAND)
        pump(br.window)
        for ch, code in (("a", 0), ("b", 11), ("c", 8)):
            post_key(br.window, ch, code)
            pump(br.window, 2)
        assert br.address_text.endswith("abc"), \
            "address bar holds %r" % br.address_text


def test_a_frame_is_presented_after_interaction():
    with _Browser() as br:
        post_key(br.window, "t", 17, cocoa._MOD_COMMAND)
        pump(br.window)
        br.draw()
        br.window.present()
        assert cocoa.msg(br.window._view, "image"), \
            "nothing was presented after a tab opened"


def _tall_page(br):
    """Load a page far taller than the window and return its tab."""
    br.new_tab("data:text/html," + "".join("<p>line %d</p>" % i
                                           for i in range(300)))
    br.draw()
    tab = br.active_tab
    assert tab.content_height() > br.tab_height(), "the page is not tall"
    return tab


def test_dragging_the_scrollbar_scrolls_the_page():
    """AppKit's own three events -- mouseDown, mouseDragged, mouseUp -- are
    what the scrollbar is dragged with, and mouseDragged is the one nothing
    used to be listening for on the bar."""
    with _Browser() as br:
        tab = _tall_page(br)
        # An unscrolled page puts the thumb at the very top of the track.
        thumb_top = br.chrome_height()
        x = br.canvas.winfo_width() - 7
        send_mouse(br.window, cocoa._LEFT_DOWN, x, thumb_top + 5)
        assert tab.scroll == 0, "pressing the thumb jumped the page"
        send_mouse(br.window, cocoa._LEFT_DRAGGED, x, thumb_top + 105)
        assert tab.scroll > 0, "mouseDragged on the thumb did not scroll"
        send_mouse(br.window, cocoa._LEFT_UP, x, thumb_top + 105)
        settled = tab.scroll
        send_mouse(br.window, cocoa._LEFT_DRAGGED, x, thumb_top + 300)
        assert tab.scroll == settled, "the drag survived mouseUp"


def test_a_drag_that_leaves_the_window_still_scrolls():
    """AppKit keeps sending the drag to the window the press went to, so the
    coordinates run off the top and bottom of the window -- and dragging the
    bar past the end of the document has to stop where the wheel stops."""
    with _Browser() as br:
        tab = _tall_page(br)
        tab.scroll_by(10 ** 9)
        bottom = tab.scroll
        tab.set_scroll(0)
        br.draw()
        thumb_top = br.chrome_height()
        x = br.canvas.winfo_width() - 7
        send_mouse(br.window, cocoa._LEFT_DOWN, x, thumb_top + 5)
        send_mouse(br.window, cocoa._LEFT_DRAGGED, x, br.window.height + 4000)
        assert tab.scroll == bottom, \
            "dragged off the bottom to %r, the wheel stops at %r" % (
                tab.scroll, bottom)
        send_mouse(br.window, cocoa._LEFT_DRAGGED, x, -4000)
        assert tab.scroll == 0, "dragged off the top to %r" % tab.scroll
        send_mouse(br.window, cocoa._LEFT_UP, x, -4000)


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  ok  {t.__name__}")
        except Exception as e:
            failed += 1
            import traceback
            traceback.print_exc()
            print(f" FAIL {t.__name__}: {e}")
    if failed:
        print(f"\n{failed} FAILED")
        sys.exit(1)
    print(f"\nALL {len(tests)} COCOA BROWSER TESTS PASSED")


if __name__ == "__main__":
    main()
