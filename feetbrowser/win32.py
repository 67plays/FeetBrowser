"""A real Windows window, with no toolkit and no bindings package.

Win32 is a C API, so ctypes is all it takes to drive it: load ``user32``,
``gdi32`` and ``kernel32``, declare the signatures, call the functions. No
pywin32, no compiled shim -- the same rule ``cocoa.py`` follows, and for the
same reason.

What this adds to ``window.Window`` is the two things a headless window
cannot have: real input (``poll_events``) and somewhere to put the pixels
(``present``). Everything above it still sees Tk's API, because translation
happens here -- ``WM_LBUTTONDOWN`` and virtual key codes become
``<Button-1>``, ``<Control-l>``, ``<MouseWheel>``.

Presenting costs one conversion. A device-independent bitmap is BGR, not RGB,
and its rows run bottom-up unless the height is negative, so the framebuffer
cannot go to GDI untouched the way it goes to CoreGraphics. We convert to
32-bit BGRX rather than 24-bit BGR: DIB rows are padded to a four-byte
boundary, so 24bpp needs per-row padding whenever the width is not a multiple
of four, while at 32bpp the stride is always ``width * 4`` and the whole
frame is one buffer with no row loop at all. The conversion itself is three
strided slice assignments, which run in C.

Everything here that is arithmetic or a lookup table lives in a module-level
function, so the part that can only be exercised on Windows is as small as it
can be made -- see ``tests/test_units.py`` for the parts that are not.
"""
import ctypes
import sys

from .window import (STATE_ALT, STATE_CONTROL, STATE_SHIFT, Event, Window,
                     key_sequences)

# -- types -----------------------------------------------------------------
#
# Spelled out rather than taken from ctypes.wintypes, because wintypes derives
# LONG from c_long -- which is 8 bytes on a 64-bit Unix and 4 on Windows. The
# structures below are wire formats handed to GDI, so their fields are fixed
# widths and BITMAPINFOHEADER is 40 bytes wherever this module is imported.
DWORD = ctypes.c_uint32
LONG = ctypes.c_int32
WORD = ctypes.c_uint16
UINT = ctypes.c_uint32
BOOL = ctypes.c_int32
HANDLE = ctypes.c_void_p
# These three are pointer-sized on both ABIs, so they take the pointer-sized
# C types rather than a fixed width.
WPARAM = ctypes.c_size_t
LPARAM = ctypes.c_ssize_t
LRESULT = ctypes.c_ssize_t

# WINFUNCTYPE is the stdcall/x64 calling convention and only exists on
# Windows. Falling back keeps this module importable everywhere, which the
# linter, the test collector and gui.py all depend on.
_FUNCTYPE = getattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE)
WNDPROC = _FUNCTYPE(LRESULT, HANDLE, UINT, WPARAM, LPARAM)


class POINT(ctypes.Structure):
    _fields_ = [("x", LONG), ("y", LONG)]


class RECT(ctypes.Structure):
    _fields_ = [("left", LONG), ("top", LONG),
                ("right", LONG), ("bottom", LONG)]


class MSG(ctypes.Structure):
    _fields_ = [("hwnd", HANDLE), ("message", UINT), ("wParam", WPARAM),
                ("lParam", LPARAM), ("time", DWORD), ("pt", POINT)]


class WNDCLASSEXW(ctypes.Structure):
    _fields_ = [("cbSize", UINT), ("style", UINT), ("lpfnWndProc", WNDPROC),
                ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                ("hInstance", HANDLE), ("hIcon", HANDLE),
                ("hCursor", HANDLE), ("hbrBackground", HANDLE),
                ("lpszMenuName", ctypes.c_wchar_p),
                ("lpszClassName", ctypes.c_wchar_p), ("hIconSm", HANDLE)]


class PAINTSTRUCT(ctypes.Structure):
    _fields_ = [("hdc", HANDLE), ("fErase", BOOL), ("rcPaint", RECT),
                ("fRestore", BOOL), ("fIncUpdate", BOOL),
                ("rgbReserved", ctypes.c_byte * 32)]


class MINMAXINFO(ctypes.Structure):
    _fields_ = [("ptReserved", POINT), ("ptMaxSize", POINT),
                ("ptMaxPosition", POINT), ("ptMinTrackSize", POINT),
                ("ptMaxTrackSize", POINT)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", DWORD), ("biWidth", LONG), ("biHeight", LONG),
                ("biPlanes", WORD), ("biBitCount", WORD),
                ("biCompression", DWORD), ("biSizeImage", DWORD),
                ("biXPelsPerMeter", LONG), ("biYPelsPerMeter", LONG),
                ("biClrUsed", DWORD), ("biClrImportant", DWORD)]


class BITMAPINFO(ctypes.Structure):
    """A header with no palette: BI_RGB at 32bpp needs none."""

    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", DWORD * 3)]


# -- constants -------------------------------------------------------------
#
# Named here rather than inline so the calls below read like the Win32 they
# are.

# Window and class styles.
CS_VREDRAW, CS_HREDRAW = 0x0001, 0x0002
WS_OVERLAPPEDWINDOW = 0x00CF0000
WS_VISIBLE = 0x10000000
CW_USEDEFAULT = -2147483648        # 0x80000000 as a signed int
SW_HIDE, SW_SHOW, SW_RESTORE = 0, 5, 9
SWP_NOMOVE, SWP_NOZORDER, SWP_NOACTIVATE = 0x0002, 0x0004, 0x0010
HWND_BOTTOM, HWND_TOP = 1, 0
PM_REMOVE = 0x0001

# Messages.
WM_DESTROY = 0x0002
WM_SIZE = 0x0005
WM_PAINT = 0x000F
WM_CLOSE = 0x0010
WM_ERASEBKGND = 0x0014
WM_GETMINMAXINFO = 0x0024
WM_SETCURSOR = 0x0020
WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_CHAR = 0x0102
WM_SYSKEYDOWN = 0x0104
WM_SYSCHAR = 0x0106
WM_MOUSEMOVE = 0x0200
WM_LBUTTONDOWN, WM_LBUTTONUP = 0x0201, 0x0202
WM_RBUTTONDOWN, WM_RBUTTONUP = 0x0204, 0x0205
WM_MBUTTONDOWN, WM_MBUTTONUP = 0x0207, 0x0208
WM_MOUSEWHEEL = 0x020A

# WM_MOUSEMOVE's wParam.
MK_LBUTTON, MK_RBUTTON, MK_MBUTTON = 0x0001, 0x0002, 0x0010

# WM_SETCURSOR's low lParam word; only the client area is ours to set.
HTCLIENT = 1

# Virtual keys, for GetKeyState.
VK_SHIFT, VK_CONTROL, VK_MENU = 0x10, 0x11, 0x12

# GDI.
DIB_RGB_COLORS = 0
BI_RGB = 0
SRCCOPY = 0x00CC0020
COLORONCOLOR = 3
WHEEL_DELTA = 120

# Clipboard.
CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002

# The per-monitor-v2 DPI context. Without it Windows renders the window at 96
# DPI and has the compositor scale the result, which is exactly the blurry
# browser this backend exists to avoid.
DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = ctypes.c_void_p(-4)

_CLASS_NAME = "FeetBrowserWindow"

# Virtual key codes -> Tk keysyms, for the keys that carry no character.
# Everything printable is left to WM_CHAR, which is the only thing that knows
# the user's keyboard layout.
VK_KEYSYMS = {
    0x08: "BackSpace", 0x09: "Tab", 0x0D: "Return", 0x1B: "Escape",
    0x21: "Prior", 0x22: "Next", 0x23: "End", 0x24: "Home",
    0x25: "Left", 0x26: "Up", 0x27: "Right", 0x28: "Down",
    0x2D: "Insert", 0x2E: "Delete",
    0x70: "F1", 0x71: "F2", 0x72: "F3", 0x73: "F4", 0x74: "F5",
    0x75: "F6", 0x76: "F7", 0x77: "F8", 0x78: "F9", 0x79: "F10",
    0x7A: "F11", 0x7B: "F12",
}

# Tk cursor names -> the IDC_* standard cursors, which LoadCursorW takes as
# integers cast to a string pointer.
CURSORS = {
    "": 32512,          # IDC_ARROW
    "arrow": 32512,
    "hand2": 32649,     # IDC_HAND
    "hand1": 32649,
    "xterm": 32513,     # IDC_IBEAM
    "watch": 32514,     # IDC_WAIT
}

# The keys that produce a character but whose WM_CHAR is unusable, because
# Control turns it into a control code (Ctrl-L arrives as 0x0C, not "l") and
# Alt suppresses it. For those the character is recovered from the virtual
# key, which is ASCII for exactly this range.
_VK_ASCII_LOW, _VK_ASCII_HIGH = 0x30, 0x5A   # '0'..'9', 'A'..'Z'


class Win32Unavailable(RuntimeError):
    """Raised when this platform cannot supply a Win32 window."""


# HWND -> Win32Window. There is one message queue per *thread*, not per
# window, so whoever pumps drains events for everybody and the shared window
# procedure routes each one to the window it belongs to. That is what lets the
# root's main loop feed a popup Toplevel, which is how the browser has always
# worked.
_WINDOWS = {}

_libs = {}
_state = {}     # the registered class atom and the WNDPROC keeping it alive


# -- pure helpers ----------------------------------------------------------
#
# No ctypes below this line until the window itself. These are the parts a
# test can reach from any operating system.

def dib_stride(width, bit_count=32):
    """Bytes per row of a DIB, which is padded to a four-byte boundary.

    The rounding is why this backend presents 32bpp: at 24bpp a 999-pixel row
    is 2997 bytes of pixels in a 3000-byte row, and every row after the first
    is offset by the difference -- the classic diagonal-smear bug. At 32bpp
    the padding is always zero.
    """
    return ((width * bit_count + 31) // 32) * 4


def bgra_from_rgb(pixels, width, height, stride=None):
    """Convert a raster.Surface framebuffer into a top-down 32bpp DIB.

    Three things change at once: RGB becomes BGR, a fourth (ignored) byte is
    added per pixel so rows need no padding, and rows are left top-down --
    which the caller then declares with a negative biHeight.

    The three assignments are strided copies inside CPython, so a frame costs
    six passes over the buffer rather than a Python loop over a million
    pixels. A Surface is contiguous (its stride is always ``width * 3``), so
    the row loop is only there for a caller that hands us a padded one.
    """
    stride = width * 3 if stride is None else stride
    if stride == width * 3:
        rgb = pixels
    else:
        rgb = bytearray(width * height * 3)
        for row in range(height):
            rgb[row * width * 3:(row + 1) * width * 3] = \
                pixels[row * stride:row * stride + width * 3]
    out = bytearray(width * height * 4)
    out[0::4] = rgb[2::3]       # blue
    out[1::4] = rgb[1::3]       # green
    out[2::4] = rgb[0::3]       # red
    # The fourth byte stays zero: BI_RGB at 32bpp ignores it.
    return out


def signed_word(value):
    """The low 16 bits of `value` read as a signed number.

    Win32 packs coordinates two to a parameter, and a negative one -- a drag
    that left the window on the left or top edge -- is 0xFFFF-relative.
    """
    value &= 0xFFFF
    return value - 0x10000 if value & 0x8000 else value


def lparam_point(lparam):
    """The (x, y) a mouse message packs into its lParam."""
    return signed_word(lparam), signed_word(lparam >> 16)


def wheel_delta(raw):
    """A WM_MOUSEWHEEL delta as the number of pixels to scroll.

    Windows reports multiples of WHEEL_DELTA (120) per notch. browser.py
    treats ``|delta| < 30`` as a pixel count and anything larger as line
    units, so a notch has to land well under 30 -- 20 pixels, matching what
    the Cocoa backend sends for one line of scroll.
    """
    if not raw:
        return 0
    delta = int(raw * 20 / WHEEL_DELTA)
    if not delta:
        delta = 1 if raw > 0 else -1
    return max(-29, min(29, delta))


def modifier_state(shift, control, alt):
    """Tk's event.state bits from three held-down flags."""
    state = 0
    if shift:
        state |= STATE_SHIFT
    if control:
        state |= STATE_CONTROL
    if alt:
        state |= STATE_ALT
    return state


def keysym_for_vk(vk, state):
    """The Tk keysym for a WM_KEYDOWN, or None to wait for WM_CHAR.

    Named keys are resolved here because they never produce a usable
    character. Printable keys are normally left to WM_CHAR, which is the only
    thing that has been through the user's keyboard layout -- except under
    Control or Alt, where WM_CHAR either carries a control code or never
    arrives, so the letter is recovered from the virtual key.
    """
    keysym = VK_KEYSYMS.get(vk)
    if keysym is not None:
        if keysym == "Tab" and state & STATE_SHIFT:
            # X11's name for a shifted Tab, and what browser.py binds for
            # previous-tab.
            return "ISO_Left_Tab"
        return keysym
    if not state & (STATE_CONTROL | STATE_ALT):
        return None
    if not _VK_ASCII_LOW <= vk <= _VK_ASCII_HIGH:
        return None
    char = chr(vk)
    if char.isdigit():
        return char
    # Tk names a shifted letter by its shifted character, so <Control-Shift-s>
    # and <Control-S> are the same keypress. window.key_sequences offers both.
    return char if state & STATE_SHIFT else char.lower()


def keysym_for_char(text, state):
    """(keysym, char) for a WM_CHAR, or None when it carries nothing.

    Control codes are dropped: Return, Tab, Escape and Backspace all arrive
    twice, once as a named virtual key and once as a WM_CHAR below 0x20, and
    only the first of those is the event.
    """
    if not text:
        return None
    char = text[0]
    if char == " ":
        return "space", " "
    if not char.isprintable():
        return None
    if state & (STATE_CONTROL | STATE_ALT):
        return None     # already delivered from the virtual key
    return char, char


# -- loading ---------------------------------------------------------------

def _load():
    """Open the DLLs and declare every signature. Idempotent.

    Declaring signatures is not optional. ctypes defaults a return type to
    ``c_int``, which truncates a 64-bit HWND to a wild handle -- the same
    class of bug that shipped once in the Cocoa backend as a segfault on the
    first frame.
    """
    if _libs:
        return
    if sys.platform != "win32":
        raise Win32Unavailable("Win32 windows need Windows")
    windll = getattr(ctypes, "WinDLL", None)
    if windll is None:      # pragma: no cover - unreachable on Windows
        raise Win32Unavailable("this Python has no stdcall support")
    try:
        for name in ("user32", "gdi32", "kernel32"):
            _libs[name] = windll(name + ".dll", use_last_error=True)
    except OSError as exc:
        _libs.clear()
        raise Win32Unavailable("cannot load %s: %s" % (name, exc)) from exc
    _declare()
    _set_dpi_awareness()


def _declare():
    user32, gdi32, kernel32 = _libs["user32"], _libs["gdi32"], \
        _libs["kernel32"]
    hwnd = HANDLE
    signatures = [
        (user32, "RegisterClassExW", ctypes.c_uint16,
         [ctypes.POINTER(WNDCLASSEXW)]),
        (user32, "CreateWindowExW", hwnd,
         [DWORD, ctypes.c_wchar_p, ctypes.c_wchar_p, DWORD, ctypes.c_int,
          ctypes.c_int, ctypes.c_int, ctypes.c_int, hwnd, HANDLE, HANDLE,
          ctypes.c_void_p]),
        (user32, "DefWindowProcW", LRESULT, [hwnd, UINT, WPARAM, LPARAM]),
        (user32, "DestroyWindow", BOOL, [hwnd]),
        (user32, "PeekMessageW", BOOL,
         [ctypes.POINTER(MSG), hwnd, UINT, UINT, UINT]),
        (user32, "TranslateMessage", BOOL, [ctypes.POINTER(MSG)]),
        (user32, "DispatchMessageW", LRESULT, [ctypes.POINTER(MSG)]),
        (user32, "GetDC", HANDLE, [hwnd]),
        (user32, "ReleaseDC", ctypes.c_int, [hwnd, HANDLE]),
        (user32, "BeginPaint", HANDLE, [hwnd, ctypes.POINTER(PAINTSTRUCT)]),
        (user32, "EndPaint", BOOL, [hwnd, ctypes.POINTER(PAINTSTRUCT)]),
        (user32, "InvalidateRect", BOOL, [hwnd, ctypes.c_void_p, BOOL]),
        (user32, "GetClientRect", BOOL, [hwnd, ctypes.POINTER(RECT)]),
        (user32, "AdjustWindowRectEx", BOOL,
         [ctypes.POINTER(RECT), DWORD, BOOL, DWORD]),
        (user32, "SetWindowPos", BOOL,
         [hwnd, hwnd, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
          UINT]),
        (user32, "ShowWindow", BOOL, [hwnd, ctypes.c_int]),
        (user32, "SetWindowTextW", BOOL, [hwnd, ctypes.c_wchar_p]),
        (user32, "GetWindowTextW", ctypes.c_int,
         [hwnd, ctypes.c_wchar_p, ctypes.c_int]),
        (user32, "SetForegroundWindow", BOOL, [hwnd]),
        (user32, "SetFocus", HANDLE, [hwnd]),
        (user32, "LoadCursorW", HANDLE, [HANDLE, ctypes.c_void_p]),
        (user32, "SetCursor", HANDLE, [HANDLE]),
        (user32, "SetCapture", HANDLE, [hwnd]),
        (user32, "ReleaseCapture", BOOL, []),
        (user32, "ScreenToClient", BOOL, [hwnd, ctypes.POINTER(POINT)]),
        (user32, "GetKeyState", ctypes.c_short, [ctypes.c_int]),
        (user32, "IsWindow", BOOL, [hwnd]),
        (user32, "OpenClipboard", BOOL, [hwnd]),
        (user32, "CloseClipboard", BOOL, []),
        (user32, "EmptyClipboard", BOOL, []),
        (user32, "GetClipboardData", HANDLE, [UINT]),
        (user32, "SetClipboardData", HANDLE, [UINT, HANDLE]),
        (user32, "IsClipboardFormatAvailable", BOOL, [UINT]),
        (gdi32, "StretchDIBits", ctypes.c_int,
         [HANDLE, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
          ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
          ctypes.c_void_p, ctypes.POINTER(BITMAPINFO), UINT, DWORD]),
        (gdi32, "SetStretchBltMode", ctypes.c_int, [HANDLE, ctypes.c_int]),
        (kernel32, "GetModuleHandleW", HANDLE, [ctypes.c_wchar_p]),
        (kernel32, "GlobalAlloc", HANDLE, [UINT, ctypes.c_size_t]),
        (kernel32, "GlobalLock", ctypes.c_void_p, [HANDLE]),
        (kernel32, "GlobalUnlock", BOOL, [HANDLE]),
        (kernel32, "GlobalSize", ctypes.c_size_t, [HANDLE]),
        (kernel32, "GlobalFree", HANDLE, [HANDLE]),
    ]
    for lib, name, restype, argtypes in signatures:
        fn = getattr(lib, name)
        fn.restype = restype
        fn.argtypes = argtypes


def _set_dpi_awareness():
    """Ask for per-monitor v2 DPI, falling back down the three generations.

    Without this the framebuffer is drawn at 96 DPI and stretched by the
    compositor, which looks exactly as bad as it sounds on a laptop panel.
    Every one of these is best-effort: the call fails harmlessly if awareness
    was already set (by a manifest, or by a second window opening).
    """
    user32 = _libs["user32"]
    setter = getattr(user32, "SetProcessDpiAwarenessContext", None)
    if setter is not None:
        setter.restype = BOOL
        setter.argtypes = [ctypes.c_void_p]
        if setter(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2):
            return
    try:
        shcore = ctypes.WinDLL("shcore.dll")
    except OSError:
        shcore = None
    if shcore is not None:
        awareness = getattr(shcore, "SetProcessDpiAwareness", None)
        if awareness is not None and awareness(2) == 0:   # PER_MONITOR_AWARE
            return
    legacy = getattr(user32, "SetProcessDPIAware", None)
    if legacy is not None:
        legacy()


def _register_class():
    """Register the window class once per process.

    The WNDPROC is stored on the module, not on a window: a ctypes callback
    that gets collected leaves Windows calling into freed memory, and the
    class outlives every window made from it anyway.
    """
    if "atom" in _state:
        return _state["atom"]
    user32, kernel32 = _libs["user32"], _libs["kernel32"]
    proc = WNDPROC(_window_proc)
    cls = WNDCLASSEXW()
    cls.cbSize = ctypes.sizeof(WNDCLASSEXW)
    cls.style = CS_HREDRAW | CS_VREDRAW
    cls.lpfnWndProc = proc
    cls.cbClsExtra = 0
    cls.cbWndExtra = 0
    cls.hInstance = kernel32.GetModuleHandleW(None)
    cls.hIcon = None
    cls.hCursor = user32.LoadCursorW(None, ctypes.c_void_p(CURSORS["arrow"]))
    cls.hbrBackground = None    # we paint every pixel; see WM_ERASEBKGND
    cls.lpszMenuName = None
    cls.lpszClassName = _CLASS_NAME
    cls.hIconSm = None
    atom = user32.RegisterClassExW(ctypes.byref(cls))
    if not atom:
        raise Win32Unavailable("could not register the window class: %d"
                               % ctypes.get_last_error())
    _state["atom"] = atom
    _state["wndproc"] = proc    # the reference that keeps the callback alive
    _state["hinstance"] = cls.hInstance
    return atom


def _window_proc(hwnd, message, wparam, lparam):
    """The one window procedure, shared by every window in the process."""
    window = _WINDOWS.get(_handle_key(hwnd))
    if window is not None:
        try:
            result = window._handle(message, wparam, lparam)
        except Exception as exc:    # noqa: BLE001 - never lose the loop
            window.on_callback_error("event", exc)
            result = None
        if result is not None:
            return result
    return _libs["user32"].DefWindowProcW(hwnd, message, wparam, lparam)


def _handle_key(hwnd):
    """A dict key for an HWND, which ctypes hands back as an int or None."""
    return int(hwnd) if hwnd else 0


# -- the window ------------------------------------------------------------

class Win32Window(Window):
    """A titled, resizable Windows window presenting a raster surface."""

    def __init__(self, width=1000, height=720, title="FeetBrowser"):
        _load()
        super().__init__(width, height, title)
        user32 = _libs["user32"]
        _register_class()
        style = WS_OVERLAPPEDWINDOW
        outer = self._frame_size(self.width, self.height, style)
        self._closed = False
        self._frame = None          # the last converted frame, for WM_PAINT
        self._frame_dims = (0, 0)
        self._repaint = True
        self._surrogate = 0
        self._bitmap = BITMAPINFO()
        self._hwnd = user32.CreateWindowExW(
            0, _CLASS_NAME, title, style, CW_USEDEFAULT, CW_USEDEFAULT,
            outer[0], outer[1], None, None, _state["hinstance"], None)
        if not self._hwnd:
            raise Win32Unavailable("could not create a window: %d"
                                   % ctypes.get_last_error())
        # Registered before the window is shown, because ShowWindow delivers
        # WM_SIZE and WM_PAINT synchronously and the procedure has to find us.
        _WINDOWS[_handle_key(self._hwnd)] = self
        user32.ShowWindow(self._hwnd, SW_SHOW)
        user32.SetForegroundWindow(self._hwnd)
        user32.SetFocus(self._hwnd)

    # -- geometry ----------------------------------------------------------

    @staticmethod
    def _frame_size(width, height, style):
        """The outer size whose *client* area is width x height."""
        rect = RECT(0, 0, int(width), int(height))
        _libs["user32"].AdjustWindowRectEx(ctypes.byref(rect), style, False, 0)
        return rect.right - rect.left, rect.bottom - rect.top

    def _content_size(self):
        rect = RECT()
        _libs["user32"].GetClientRect(self._hwnd, ctypes.byref(rect))
        return rect.right - rect.left, rect.bottom - rect.top

    def resize(self, width, height):
        """Resize from our side, e.g. a geometry() call before the first
        frame. A resize the *user* made arrives as WM_SIZE instead."""
        super().resize(width, height)
        if self._closed:
            return
        outer = self._frame_size(self.width, self.height, WS_OVERLAPPEDWINDOW)
        _libs["user32"].SetWindowPos(
            self._hwnd, None, 0, 0, outer[0], outer[1],
            SWP_NOMOVE | SWP_NOZORDER | SWP_NOACTIVATE)

    def minsize(self, width, height):
        super().minsize(width, height)
        # Enforced from WM_GETMINMAXINFO; nothing to do to the window itself.

    # -- presenting --------------------------------------------------------

    def present(self):
        canvas = self.canvas
        if canvas is None or self._closed:
            return
        if not canvas.dirty and not self._repaint and self._frame is not None:
            return
        if canvas.dirty or self._frame is None:
            surface = canvas.render()
            self._frame = bgra_from_rgb(surface.pixels, surface.width,
                                        surface.height, surface.stride)
            self._frame_dims = (surface.width, surface.height)
        self._repaint = False
        user32 = _libs["user32"]
        hdc = user32.GetDC(self._hwnd)
        if not hdc:
            return
        try:
            self._blit(hdc)
        finally:
            user32.ReleaseDC(self._hwnd, hdc)

    def _blit(self, hdc):
        """Push the converted frame at whatever size the client area is now.

        Stretching rather than a one-to-one SetDIBitsToDevice covers the frame
        or two between the user dragging the window edge and the canvas
        catching up, where the surface and the client area disagree. When they
        agree -- which is almost always -- StretchDIBits takes the same
        straight copy path.
        """
        width, height = self._frame_dims
        if not width or not height or self._frame is None:
            return
        header = self._bitmap.bmiHeader
        header.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        header.biWidth = width
        # Negative: a DIB is bottom-up by default, and our rows are top-down.
        header.biHeight = -height
        header.biPlanes = 1
        header.biBitCount = 32
        header.biCompression = BI_RGB
        header.biSizeImage = 0
        client_w, client_h = self._content_size()
        if not client_w or not client_h:
            return
        gdi32 = _libs["gdi32"]
        gdi32.SetStretchBltMode(hdc, COLORONCOLOR)
        buffer = (ctypes.c_char * len(self._frame)).from_buffer(self._frame)
        gdi32.StretchDIBits(hdc, 0, 0, client_w, client_h, 0, 0, width,
                            height, buffer, ctypes.byref(self._bitmap),
                            DIB_RGB_COLORS, SRCCOPY)

    # -- input -------------------------------------------------------------

    def poll_events(self):
        """Drain the thread's message queue without blocking.

        ``PeekMessageW`` rather than ``GetMessageW``: the base main loop calls
        this every iteration and does its own waiting, so a pump that blocked
        would stop timers and animation dead.
        """
        if self._closed:
            return False
        user32 = _libs["user32"]
        message = MSG()
        delivered = False
        while user32.PeekMessageW(ctypes.byref(message), None, 0, 0,
                                  PM_REMOVE):
            delivered = True
            # TranslateMessage is what turns a WM_KEYDOWN into the WM_CHAR
            # that carries the character for the user's keyboard layout.
            user32.TranslateMessage(ctypes.byref(message))
            user32.DispatchMessageW(ctypes.byref(message))
            if self._closed:
                break
        return delivered

    def _handle(self, message, wparam, lparam):
        """Translate one message. None means "let Windows have it"."""
        if message == WM_PAINT:
            return self._on_paint()
        if message == WM_ERASEBKGND:
            return 1    # every pixel is ours; erasing it first only flickers
        if message == WM_SIZE:
            width, height = signed_word(lparam), signed_word(lparam >> 16)
            if width and height and (width, height) != (self.width,
                                                        self.height):
                # The base implementation, not ours: the window is already
                # this size, and asking Windows to resize it again mid-drag
                # fights the user.
                Window.resize(self, width, height)
            self._repaint = True
            return 0
        if message == WM_GETMINMAXINFO:
            return self._on_minmax(lparam)
        if message == WM_SETCURSOR:
            return self._on_setcursor(lparam)
        if message == WM_CLOSE:
            self.destroy()
            return 0
        if message == WM_DESTROY:
            self._closed = True
            _WINDOWS.pop(_handle_key(self._hwnd), None)
            return 0
        if message in (WM_KEYDOWN, WM_SYSKEYDOWN):
            self._on_key(wparam)
            # Alt combinations stay with Windows too, or Alt-F4 stops working.
            return 0 if message == WM_KEYDOWN else None
        if message in (WM_CHAR, WM_SYSCHAR):
            self._on_char(wparam)
            return 0 if message == WM_CHAR else None
        if message == WM_MOUSEWHEEL:
            self._on_wheel(wparam, lparam)
            return 0
        if WM_MOUSEMOVE <= message <= WM_MOUSEWHEEL:
            return self._on_mouse(message, wparam, lparam)
        return None

    def _on_paint(self):
        user32 = _libs["user32"]
        paint = PAINTSTRUCT()
        hdc = user32.BeginPaint(self._hwnd, ctypes.byref(paint))
        if hdc:
            # Repaint from the last frame rather than re-rendering: WM_PAINT
            # arrives during a live resize drag, where the loop is not running.
            self._blit(hdc)
            user32.EndPaint(self._hwnd, ctypes.byref(paint))
        if self._frame is None:
            # Nothing has been presented yet, so ask for another WM_PAINT once
            # there is something to show rather than leaving the window blank.
            self._repaint = True
        return 0

    def _on_minmax(self, lparam):
        if not (self.min_width or self.min_height):
            return None
        info = ctypes.cast(ctypes.c_void_p(lparam),
                           ctypes.POINTER(MINMAXINFO)).contents
        outer = self._frame_size(self.min_width, self.min_height,
                                 WS_OVERLAPPEDWINDOW)
        info.ptMinTrackSize.x, info.ptMinTrackSize.y = outer
        return 0

    def _on_setcursor(self, lparam):
        """Set the pointer for the client area, and only for it.

        Windows asks on every mouse move, which is the hook: the canvas
        publishes a cursor name and this is where it is honoured. The frame
        and the resize borders are not ours, so those fall through.
        """
        if signed_word(lparam) != HTCLIENT:
            return None
        wanted = getattr(self.canvas, "cursor", "") if self.canvas else ""
        user32 = _libs["user32"]
        handle = user32.LoadCursorW(
            None, ctypes.c_void_p(CURSORS.get(wanted, CURSORS["arrow"])))
        user32.SetCursor(handle)
        return 1

    def _state(self):
        """Tk's event.state, read from the keyboard rather than the message.

        GetKeyState's high bit is "held down now". Reading it here rather than
        tracking key-up and key-down keeps the state right after the window
        loses and regains focus with a modifier held.
        """
        held = _libs["user32"].GetKeyState
        return modifier_state(held(VK_SHIFT) & 0x8000,
                              held(VK_CONTROL) & 0x8000,
                              held(VK_MENU) & 0x8000)

    def _on_mouse(self, message, wparam, lparam):
        x, y = lparam_point(lparam)
        state = self._state()
        user32 = _libs["user32"]
        if message == WM_MOUSEMOVE:
            for mask, num in ((MK_LBUTTON, 1), (MK_MBUTTON, 2),
                              (MK_RBUTTON, 3)):
                if wparam & mask:
                    name = "<B%d-Motion>" % num
                    self.dispatch(name, Event(x=x, y=y, num=num, state=state,
                                              type=name))
                    return 0
            self.dispatch("<Motion>", Event(x=x, y=y, state=state,
                                            type="<Motion>"))
            return 0
        buttons = {
            WM_LBUTTONDOWN: (1, "<Button-1>", True),
            WM_LBUTTONUP: (1, "<ButtonRelease-1>", False),
            WM_MBUTTONDOWN: (2, "<Button-2>", True),
            WM_MBUTTONUP: (2, "<ButtonRelease-2>", False),
            WM_RBUTTONDOWN: (3, "<Button-3>", True),
            WM_RBUTTONUP: (3, "<ButtonRelease-3>", False),
        }
        if message not in buttons:
            return None
        num, name, pressed = buttons[message]
        # Capture the mouse for the length of a drag, so a selection that
        # leaves the window still reports where it went and still ends.
        if pressed:
            user32.SetCapture(self._hwnd)
            user32.SetFocus(self._hwnd)
        else:
            user32.ReleaseCapture()
        self.dispatch(name, Event(x=x, y=y, num=num, state=state, type=name))
        return 0

    def _on_wheel(self, wparam, lparam):
        delta = wheel_delta(signed_word(wparam >> 16))
        if not delta:
            return
        # A wheel message carries *screen* coordinates, unlike every other
        # mouse message. Forgetting that puts the scroll in the wrong frame.
        point = POINT(signed_word(lparam), signed_word(lparam >> 16))
        _libs["user32"].ScreenToClient(self._hwnd, ctypes.byref(point))
        self.dispatch("<MouseWheel>",
                      Event(x=point.x, y=point.y, delta=delta,
                            state=self._state(), type="<MouseWheel>"))

    def _on_key(self, vk):
        state = self._state()
        keysym = keysym_for_vk(vk, state)
        if keysym is None:
            return      # printable and unmodified: WM_CHAR has the character
        char = "\r" if keysym == "Return" else ""
        self._deliver(keysym, char, state)

    def _on_char(self, code):
        state = self._state()
        text = self._decode(code)
        resolved = keysym_for_char(text, state)
        if resolved is None:
            return
        keysym, char = resolved
        self._deliver(keysym, char, state)

    def _decode(self, code):
        """A WM_CHAR wParam is one UTF-16 code unit, so an astral character
        arrives as two messages and the first half means nothing on its own."""
        pending = self._surrogate
        self._surrogate = 0
        if 0xD800 <= code <= 0xDBFF:
            self._surrogate = code
            return ""
        if 0xDC00 <= code <= 0xDFFF:
            if not pending:
                return ""
            code = 0x10000 + ((pending - 0xD800) << 10) + (code - 0xDC00)
        return chr(code)

    def _deliver(self, keysym, char, state):
        event = Event(keysym=keysym, char=char, state=state, type="<Key>")
        for sequence in key_sequences(keysym, state):
            if self.dispatch(sequence, event):
                return

    # -- window chrome -----------------------------------------------------

    def on_title_changed(self, title):
        if not self._closed:
            _libs["user32"].SetWindowTextW(self._hwnd, title)

    def on_destroy(self):
        if self._closed:
            return
        hwnd = self._hwnd
        self._closed = True
        _WINDOWS.pop(_handle_key(hwnd), None)
        self._frame = None
        if hwnd:
            _libs["user32"].DestroyWindow(hwnd)

    def withdraw(self):
        super().withdraw()
        _libs["user32"].ShowWindow(self._hwnd, SW_HIDE)

    def deiconify(self):
        super().deiconify()
        _libs["user32"].ShowWindow(self._hwnd, SW_SHOW)

    def lift(self, *_args):
        user32 = _libs["user32"]
        user32.SetWindowPos(self._hwnd, HWND_TOP, 0, 0, 0, 0,
                            SWP_NOMOVE | 0x0001)   # | SWP_NOSIZE
        user32.SetForegroundWindow(self._hwnd)

    def lower(self, *_args):
        _libs["user32"].SetWindowPos(self._hwnd, HWND_BOTTOM, 0, 0, 0, 0,
                                     SWP_NOMOVE | 0x0001 | SWP_NOACTIVATE)

    # -- clipboard ---------------------------------------------------------

    def on_clipboard_set(self, text):
        user32, kernel32 = _libs["user32"], _libs["kernel32"]
        if not user32.OpenClipboard(self._hwnd):
            return
        try:
            user32.EmptyClipboard()
            data = ctypes.create_unicode_buffer(text)
            size = ctypes.sizeof(data)
            handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, size)
            if not handle:
                return
            target = kernel32.GlobalLock(handle)
            if not target:
                kernel32.GlobalFree(handle)
                return
            ctypes.memmove(target, ctypes.byref(data), size)
            kernel32.GlobalUnlock(handle)
            # Ownership passes to the clipboard on success, and only then --
            # a failed SetClipboardData leaves the block ours to free.
            if not user32.SetClipboardData(CF_UNICODETEXT, handle):
                kernel32.GlobalFree(handle)
        finally:
            user32.CloseClipboard()

    def on_clipboard_get(self):
        user32, kernel32 = _libs["user32"], _libs["kernel32"]
        if not user32.IsClipboardFormatAvailable(CF_UNICODETEXT):
            return ""
        if not user32.OpenClipboard(self._hwnd):
            return ""
        try:
            handle = user32.GetClipboardData(CF_UNICODETEXT)
            if not handle:
                return ""
            pointer = kernel32.GlobalLock(handle)
            if not pointer:
                return ""
            try:
                return ctypes.wstring_at(pointer)
            finally:
                kernel32.GlobalUnlock(handle)
        finally:
            user32.CloseClipboard()


class Win32Toplevel(Win32Window):
    """A secondary window, used for the browser's link previews."""

    def __init__(self, master=None, **kwargs):
        super().__init__(**kwargs)
        self.master = master
        if master is not None:
            master.children.append(self)

    def destroy(self):
        if self.master is not None and self in self.master.children:
            self.master.children.remove(self)
        super().destroy()


class Win32Tk(Win32Window):
    """The root window, matching ``window.Tk``."""

    def __init__(self, *_args, **kwargs):
        super().__init__(**kwargs)
        self.tk = None  # layout's batched-measure path checks for this


# gui.Toplevel reads this off the master, so a popup opened from a real window
# is a real window and one opened from a headless root stays headless.
Win32Window.toplevel_class = Win32Toplevel


def available():
    """True when a Win32 window can actually be created here."""
    if sys.platform != "win32":
        return False
    try:
        _load()
    except Win32Unavailable:
        return False
    return True
