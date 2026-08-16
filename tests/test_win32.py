"""The browser itself, running in a real Win32 window.

Every other test in this suite runs headless, so the one thing none of them
can see is the browser wired to a window that Windows actually created: a
click that arrives as WM_LBUTTONDOWN and has to come back out as an opened
tab, a Ctrl-L that has to reach the address bar, a frame that has to end up
on the screen. So these tests open a window, drive it with real messages
through the real window procedure, and blit through real GDI.

doormat has its own suite for whether a window works. This one is for
whether the browser works *in* one, which is the seam the two packages meet
at and the only thing that can be broken while both halves pass their own
tests. Skipped with a clear message off Windows.
"""
import ctypes
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _skip(reason):
    print("SKIP test_win32.py: %s" % reason)
    sys.exit(0)


if sys.platform != "win32":
    _skip("not Windows")

from feetbrowser import browser as browsermod  # noqa: E402
from doormat import win32  # noqa: E402

if not win32.available():
    _skip("no Win32 window station here")


# -- synthetic input -------------------------------------------------------
#
# A few signatures the backend itself never needs, declared the same way it
# declares its own: a missing restype truncates a handle to 32 bits.

def _extra(lib, name, restype, argtypes):
    fn = getattr(win32._libs[lib], name)
    fn.restype = restype
    fn.argtypes = argtypes
    return fn


HANDLE, UINT = win32.HANDLE, win32.UINT
_send = _extra("user32", "SendMessageW", win32.LRESULT,
               [HANDLE, UINT, win32.WPARAM, win32.LPARAM])
_get_keyboard_state = _extra("user32", "GetKeyboardState", win32.BOOL,
                             [ctypes.c_void_p])
_set_keyboard_state = _extra("user32", "SetKeyboardState", win32.BOOL,
                             [ctypes.c_void_p])
_create_dc = _extra("gdi32", "CreateCompatibleDC", HANDLE, [HANDLE])
_create_bitmap = _extra("gdi32", "CreateCompatibleBitmap", HANDLE,
                        [HANDLE, ctypes.c_int, ctypes.c_int])
_select = _extra("gdi32", "SelectObject", HANDLE, [HANDLE, HANDLE])
_delete_object = _extra("gdi32", "DeleteObject", win32.BOOL, [HANDLE])
_delete_dc = _extra("gdi32", "DeleteDC", win32.BOOL, [HANDLE])
_get_pixel = _extra("gdi32", "GetPixel", win32.DWORD,
                    [HANDLE, ctypes.c_int, ctypes.c_int])


def pack(x, y):
    """Two coordinates in one lParam, the way Windows packs them."""
    return (int(y) & 0xFFFF) << 16 | (int(x) & 0xFFFF)


def send(win, message, wparam=0, lparam=0):
    """Deliver a message straight to the window procedure.

    This is what SendMessageW does for a window on the calling thread, so the
    real procedure and the real translation run; only the queue is skipped.
    """
    return _send(win._hwnd, message, wparam, lparam)


def pump(win, times=4):
    for _ in range(times):
        win.poll_events()


class holding:
    """Hold modifier keys down for real, as far as GetKeyState can tell.

    The backend reads modifiers from the keyboard rather than from the
    message, which is the right thing to do -- it survives the window losing
    focus with a key held -- but it means a synthetic Ctrl-L needs Control
    genuinely down. SetKeyboardState writes the calling thread's own key
    state table, which is the table GetKeyState reads, and touches no other
    process.
    """

    def __init__(self, *vks):
        self.vks = vks

    def __enter__(self):
        self.saved = (ctypes.c_ubyte * 256)()
        _get_keyboard_state(ctypes.byref(self.saved))
        state = (ctypes.c_ubyte * 256)()
        ctypes.memmove(state, self.saved, 256)
        for vk in self.vks:
            state[vk] = 0x80
        _set_keyboard_state(ctypes.byref(state))
        return self

    def __exit__(self, *_exc):
        _set_keyboard_state(ctypes.byref(self.saved))
        return False


class _Session:
    """A live window, torn down however the test ends."""

    def __enter__(self):
        self.win = win32.Win32Tk(width=900, height=600, title="test")
        # Drain what a window makes on its way up: ShowWindow delivers
        # WM_SIZE and WM_PAINT before it returns, and the activation
        # messages follow through the queue.
        pump(self.win, 6)
        return self.win

    def __exit__(self, *_exc):
        self.win.destroy()
        pump(self.win, 2)
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


class _MemoryTarget:
    """A bitmap in memory to blit into.

    Reading pixels back from the window itself would depend on the window
    being visible and unoccluded, which on a build agent it is not. A memory
    DC goes through the same StretchDIBits with the same BITMAPINFO, so it
    proves the same things and proves them the same way every time.
    """

    def __init__(self, win, width, height):
        self.win = win
        self.width, self.height = width, height

    def __enter__(self):
        user32 = win32._libs["user32"]
        self.screen = user32.GetDC(self.win._hwnd)
        self.hdc = _create_dc(self.screen)
        self.bitmap = _create_bitmap(self.screen, self.width, self.height)
        assert self.hdc and self.bitmap, "no memory bitmap"
        self.old = _select(self.hdc, self.bitmap)
        return self

    def pixel(self, x, y):
        """(r, g, b) at a point. GetPixel hands back 0x00bbggrr."""
        value = _get_pixel(self.hdc, x, y)
        assert value != 0xFFFFFFFF, "no pixel at (%d, %d)" % (x, y)
        return value & 0xFF, (value >> 8) & 0xFF, (value >> 16) & 0xFF

    def __exit__(self, *_exc):
        _select(self.hdc, self.old)
        _delete_object(self.bitmap)
        _delete_dc(self.hdc)
        win32._libs["user32"].ReleaseDC(self.win._hwnd, self.screen)
        return False


# -- the browser, driven for real ------------------------------------------

def test_clicking_the_new_tab_button_opens_a_tab():
    """A stray attribute error anywhere in the mouse path swallows every
    click, and nothing else looks wrong."""
    with _Browser() as br:
        before = len(br.tabs)
        x = br._new_tab_x() + browsermod.NEW_TAB_W / 2
        # The tab strip is the 40px band under whatever chrome the toes drew.
        y = browsermod.toes.band_height(br.chrome_bands()) + 20
        send(br.window, win32.WM_LBUTTONDOWN, win32.MK_LBUTTON, pack(x, y))
        send(br.window, win32.WM_LBUTTONUP, 0, pack(x, y))
        assert len(br.tabs) == before + 1, \
            "clicking + did not open a tab (%d -> %d)" % (before, len(br.tabs))


def test_keyboard_shortcuts_reach_the_browser():
    with _Browser() as br:
        before = len(br.tabs)
        with holding(win32.VK_CONTROL):
            send(br.window, win32.WM_KEYDOWN, 0x54, 0)      # Ctrl-T
            assert len(br.tabs) == before + 1, "Ctrl-T did not open a tab"
            send(br.window, win32.WM_KEYDOWN, 0x4C, 0)      # Ctrl-L
        assert br.focus == "address", "Ctrl-L did not focus the address bar"


def test_typing_into_the_address_bar_works():
    with _Browser() as br:
        with holding(win32.VK_CONTROL):
            send(br.window, win32.WM_KEYDOWN, 0x4C, 0)
        for char in "abc":
            send(br.window, win32.WM_CHAR, ord(char), 0)
        assert br.address_text.endswith("abc"), \
            "address bar holds %r" % br.address_text


def test_a_frame_is_presented_after_interaction():
    with _Browser() as br:
        with holding(win32.VK_CONTROL):
            send(br.window, win32.WM_KEYDOWN, 0x54, 0)
        br.draw()
        br.window.present()
        assert br.window._frame, "nothing was presented after a tab opened"
        with _MemoryTarget(br.window, 900, 600) as target:
            br.window._blit(target.hdc)
            # The chrome is drawn at the top of every frame, so a blank
            # window here means the browser never reached the screen.
            assert target.pixel(450, 10) != (0, 0, 0), \
                "the top of the window is black"


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
    print(f"\nALL {len(tests)} BROWSER-IN-WIN32 TESTS PASSED")


if __name__ == "__main__":
    main()
