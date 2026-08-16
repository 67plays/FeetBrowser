"""The browser in a real X11 window, driven through a real X server.

doormat owns the backend now and tests it there: scanline padding, pixel
layouts, keysyms, button numbering, clipboard ownership, the connect
retry -- and, live, that a window maps and that a frame put through
XPutImage reads back out of XGetImage with its colours intact. None of
that knows a browser exists.

What is left here is the join. A real Browser in a real window, clicked
and typed at through the server, with the result read back off it.
Pulling the window layer out of the browser could hardly break the
window -- doormat's own suite would say so -- but it can very easily
break the browser's grip on one, and that only shows up from this side.
Every test below needs a server; with none, the file says so and runs
nothing.
"""
import ctypes
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from doormat import x11
from doormat.window import STATE_CONTROL


def eq(a, b, msg=""):
    assert a == b, "%s: %r != %r" % (msg, a, b)


def _live_reason():
    if not x11.available():
        return x11.unavailable_reason() or "no X11 on this platform"
    return ""


LIVE_REASON = _live_reason()
LIVE = not LIVE_REASON

if LIVE:
    from feetbrowser import browser as browsermod

    # A few signatures the backend itself never needs, declared the same way
    # it declares its own -- a missing restype truncates an XID to 32 bits.
    def _extra(name, restype, argtypes):
        fn = getattr(x11._libs["x11"], name)
        fn.restype = restype
        fn.argtypes = argtypes
        return fn

    _string_to_keysym = _extra("XStringToKeysym", x11.KeySym,
                               [ctypes.c_char_p])
    _keysym_to_keycode = _extra("XKeysymToKeycode", ctypes.c_ubyte,
                                [x11.Display, x11.KeySym])
    _get_image = _extra("XGetImage", ctypes.POINTER(x11.XImage),
                        [x11.Display, x11.XID, ctypes.c_int, ctypes.c_int,
                         ctypes.c_uint, ctypes.c_uint, ctypes.c_ulong,
                         ctypes.c_int])
    _get_geometry = _extra("XGetGeometry", x11.Status,
                           [x11.Display, x11.XID,
                            ctypes.POINTER(x11.XID),
                            ctypes.POINTER(ctypes.c_int),
                            ctypes.POINTER(ctypes.c_int),
                            ctypes.POINTER(ctypes.c_uint),
                            ctypes.POINTER(ctypes.c_uint),
                            ctypes.POINTER(ctypes.c_uint),
                            ctypes.POINTER(ctypes.c_uint)])


def send(win, event, mask=0):
    """Deliver one event to a window the way another client would.

    XSendEvent rather than XTestFakeInput: a synthetic event goes to the
    window named regardless of where the pointer is or who has the keyboard
    focus, which is the only way to be deterministic on a bare server with no
    window manager running.
    """
    lib = x11._libs["x11"]
    assert lib.XSendEvent(win._display, win._window, False, mask,
                          ctypes.byref(event)), "XSendEvent failed"
    lib.XFlush(win._display)


def key_event(win, name, state=0, press=True):
    keysym = _string_to_keysym(name.encode())
    assert keysym, "no keysym called %r" % name
    code = _keysym_to_keycode(win._display, keysym)
    assert code, "%r is not on this keyboard layout" % name
    event = x11.XEvent()
    event.xkey.type = x11.KEY_PRESS if press else x11.KEY_RELEASE
    event.xkey.display = win._display
    event.xkey.window = win._window
    event.xkey.root = x11._state["root"]
    event.xkey.keycode = code
    event.xkey.state = state
    event.xkey.same_screen = True
    return event


def press_key(win, name, state=0):
    send(win, key_event(win, name, state), x11.KEY_PRESS_MASK)


def button_event(win, button, x, y, state=0, press=True):
    event = x11.XEvent()
    event.xbutton.type = x11.BUTTON_PRESS if press else x11.BUTTON_RELEASE
    event.xbutton.display = win._display
    event.xbutton.window = win._window
    event.xbutton.root = x11._state["root"]
    event.xbutton.button = button
    event.xbutton.state = state
    event.xbutton.x, event.xbutton.y = x, y
    event.xbutton.same_screen = True
    return event


def click(win, button, x, y, state=0):
    send(win, button_event(win, button, x, y, state, True),
         x11.BUTTON_PRESS_MASK)
    send(win, button_event(win, button, x, y, state, False),
         x11.BUTTON_RELEASE_MASK)


def motion_event(win, x, y, state=0):
    event = x11.XEvent()
    event.xmotion.type = x11.MOTION_NOTIFY
    event.xmotion.display = win._display
    event.xmotion.window = win._window
    event.xmotion.root = x11._state["root"]
    event.xmotion.x, event.xmotion.y = x, y
    event.xmotion.state = state
    event.xmotion.same_screen = True
    return event


def drag_to(win, x, y):
    """A pointer move with Button 1 held, which is X11's way of saying drag:
    one event type for moving and dragging, told apart by the state mask."""
    send(win, motion_event(win, x, y, 1 << 8), x11.POINTER_MOTION_MASK)


def pump(win, times=3):
    """Let the window process everything the server has for it.

    XSync before each pass, and this is not belt and braces: XPending never
    waits, so an event we sent a microsecond ago is usually still in flight
    and a bare poll_events() sails straight past it. XSync makes the round
    trip, which puts the reply in our queue before we look.
    """
    for _ in range(times):
        if win._closed:     # the display is gone with it; do not touch it
            return
        x11._libs["x11"].XSync(win._display, False)
        win.poll_events()


def wait_ready(win, seconds=10.0):
    """Wait until the server will let us read the window back.

    A window is not on screen the instant XMapWindow returns -- with a window
    manager in the way it takes a round trip or two, and until then XGetImage
    on it simply fails and anything we blit is thrown away by the map. Asking
    for one pixel is that question put directly to the server.
    """
    lib = x11._libs["x11"]
    deadline = time.time() + seconds
    while True:
        lib.XSync(win._display, False)
        win.poll_events()
        image = _get_image(win._display, win._window, 0, 0, 1, 1,
                           0xFFFFFFFF, x11.Z_PIXMAP)
        if image:
            image.contents.data = None
            lib.XFree(image)
            return True
        if time.time() > deadline:
            return False
        time.sleep(0.02)


def geometry(win):
    """(width, height) as the X server currently has it."""
    root = x11.XID()
    x, y = ctypes.c_int(), ctypes.c_int()
    width, height = ctypes.c_uint(), ctypes.c_uint()
    border, depth = ctypes.c_uint(), ctypes.c_uint()
    x11._libs["x11"].XSync(win._display, False)
    assert _get_geometry(win._display, win._window, ctypes.byref(root),
                         ctypes.byref(x), ctypes.byref(y),
                         ctypes.byref(width), ctypes.byref(height),
                         ctypes.byref(border), ctypes.byref(depth)), \
        "XGetGeometry failed"
    return width.value, height.value


def wait_geometry(win, size, seconds=5.0):
    """A resize is a request, not a command -- a window manager answers it in
    its own time, and on a bare server it happens at once."""
    deadline = time.time() + seconds
    while geometry(win) != size and time.time() < deadline:
        win.poll_events()
        time.sleep(0.02)
    return geometry(win)


def grab(win):
    """(pixels, bytes_per_line) read straight back out of the X server.

    This is the only check that proves the frame arrived rather than merely
    being sent: XGetImage asks the server what is actually on the window.
    """
    lib = x11._libs["x11"]
    lib.XSync(win._display, False)
    image = _get_image(win._display, win._window, 0, 0, win.width, win.height,
                       0xFFFFFFFF, x11.Z_PIXMAP)
    assert image, "XGetImage came back empty"
    try:
        line = image.contents.bytes_per_line
        raw = ctypes.string_at(image.contents.data,
                               line * image.contents.height)
    finally:
        image.contents.data = None
        lib.XFree(image)
    return raw, line


class _Session:
    """A live window, torn down however the test ends."""

    def __init__(self, width=500, height=400):
        self.size = (width, height)

    def __enter__(self):
        self.win = x11.X11Tk(width=self.size[0], height=self.size[1],
                             title="test")
        assert wait_ready(self.win), "the window never reached the screen"
        return self.win

    def __exit__(self, *_exc):
        self.win.destroy()
        return False


class _Browser(_Session):
    """A live window driving a real Browser, on about:blank only."""

    def __init__(self):
        super().__init__(1000, 700)

    def __enter__(self):
        win = super().__enter__()
        self.browser = browsermod.Browser(win)
        # Browser.__init__ calls geometry(), so the window is a different size
        # now and XGetImage will not read past what the server actually has.
        wait_geometry(win, (win.width, win.height))
        self.browser.new_tab("about:blank")
        self.browser.draw()
        win.present()
        return self.browser


def _tall_page(br):
    """Load a page far taller than the window and return its tab."""
    br.new_tab("data:text/html," + "".join("<p>line %d</p>" % i
                                           for i in range(300)))
    br.draw()
    tab = br.active_tab
    assert tab.content_height() > br.tab_height(), "the page is not tall"
    return tab


# -- the browser, driven for real ------------------------------------------

def live_clicking_the_new_tab_button_opens_a_tab():
    """A stray attribute error anywhere in the mouse path swallows every
    click in the browser with nothing else looking wrong."""
    with _Browser() as br:
        before = len(br.tabs)
        x = int(br._new_tab_x() + browsermod.NEW_TAB_W / 2)
        # The tab strip is the 40px band under whatever chrome the toes drew.
        y = int(browsermod.toes.band_height(br.chrome_bands()) + 20)
        click(br.window, 1, x, y)
        pump(br.window)
        eq(len(br.tabs), before + 1, "clicking + did not open a tab")


def live_keyboard_shortcuts_reach_the_browser():
    with _Browser() as br:
        before = len(br.tabs)
        press_key(br.window, "t", STATE_CONTROL)
        pump(br.window)
        eq(len(br.tabs), before + 1, "Ctrl-T did not open a tab")
        press_key(br.window, "l", STATE_CONTROL)
        pump(br.window)
        eq(br.focus, "address", "Ctrl-L did not focus the address bar")


def live_typing_into_the_address_bar_works():
    with _Browser() as br:
        press_key(br.window, "l", STATE_CONTROL)
        pump(br.window)
        for char in "abc":
            press_key(br.window, char)
            pump(br.window, 2)
        assert br.address_text.endswith("abc"), \
            "address bar holds %r" % br.address_text


def live_a_real_page_reaches_the_screen():
    """End to end: chrome, tabs, toolbar and page, drawn by the browser and
    read back off the X server. A window that stayed blank fails here."""
    with _Browser() as br:
        br.draw()
        br.window.present()
        raw, line = grab(br.window)
        fmt = x11._state["format"]
        size = fmt.bits_per_pixel // 8
        order = "little" if fmt.byte_order == x11.LSB_FIRST else "big"
        colours = set()
        for y in range(0, br.window.height, 17):
            for x in range(0, br.window.width, 23):
                at = y * line + x * size
                colours.add(int.from_bytes(raw[at:at + size], order))
        assert len(colours) > 2, \
            "the window is one flat colour; nothing was drawn"


def live_dragging_the_scrollbar_scrolls_the_page():
    """ButtonPress, then MotionNotify with Button1Mask, then ButtonRelease --
    the three the scrollbar is dragged with. The middle one is the event
    nothing used to be listening for on the bar."""
    with _Browser() as br:
        tab = _tall_page(br)
        # An unscrolled page puts the thumb at the very top of the track.
        thumb_top = int(br.chrome_height())
        x = br.canvas.winfo_width() - 7
        send(br.window, button_event(br.window, 1, x, thumb_top + 5),
             x11.BUTTON_PRESS_MASK)
        pump(br.window)
        eq(tab.scroll, 0, "pressing the thumb jumped the page")
        drag_to(br.window, x, thumb_top + 105)
        pump(br.window)
        assert tab.scroll > 0, "dragging the thumb did not scroll the page"
        send(br.window, button_event(br.window, 1, x, thumb_top + 105,
                                     press=False), x11.BUTTON_RELEASE_MASK)
        pump(br.window)
        settled = tab.scroll
        drag_to(br.window, x, thumb_top + 300)
        pump(br.window)
        eq(tab.scroll, settled, "the drag survived the button coming up")


def live_a_drag_that_leaves_the_window_still_scrolls():
    """The press grabs the pointer, so X keeps reporting the drag to this
    window with coordinates outside it -- and dragging past the end of the
    document has to stop exactly where the wheel stops."""
    with _Browser() as br:
        tab = _tall_page(br)
        tab.scroll_by(10 ** 9)
        bottom = tab.scroll
        tab.set_scroll(0)
        br.draw()
        thumb_top = int(br.chrome_height())
        x = br.canvas.winfo_width() - 7
        send(br.window, button_event(br.window, 1, x, thumb_top + 5),
             x11.BUTTON_PRESS_MASK)
        pump(br.window)
        drag_to(br.window, x, br.window.height + 4000)
        pump(br.window)
        eq(tab.scroll, bottom, "dragging off the bottom missed the end")
        drag_to(br.window, x, -4000)
        pump(br.window)
        eq(tab.scroll, 0, "dragging off the top missed the start")
        send(br.window, button_event(br.window, 1, x, -4000, press=False),
             x11.BUTTON_RELEASE_MASK)
        pump(br.window)


def main():
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("live_")]
    if not LIVE:
        # Nothing here degrades to an offline check, so there is nothing to
        # run and no reason to report a failure for a missing server.
        print("SKIP test_x11.py: %s" % LIVE_REASON)
        return
    failed = ran = 0
    only = [a for a in sys.argv[1:] if not a.startswith("-")]
    for test in tests:
        if only and test.__name__ not in only:
            continue
        ran += 1
        try:
            test()
            print("  ok  %s" % test.__name__, flush=True)
        except Exception as exc:
            failed += 1
            import traceback
            traceback.print_exc()
            print(" FAIL %s: %s" % (test.__name__, exc), flush=True)
    if failed:
        print("\n%d FAILED" % failed)
        sys.exit(1)
    print("\nALL %d X11 BROWSER TESTS PASSED against a live server" % ran)


if __name__ == "__main__":
    main()
