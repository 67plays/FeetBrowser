"""Where a window comes from.

The windows themselves are `doormat`: the X11, Win32 and Cocoa backends, the
input translation behind them and the event loop above them. That was
``feetbrowser/window.py``, ``x11.py``, ``win32.py`` and ``cocoa.py`` until it
was split out into a repository of its own -- the same code by the same
authors, moved because none of it knows a browser exists. It draws nothing:
a window is handed our `raster` canvas and asks it for a surface of packed
RGB, which is the whole seam between them.

What is left here is the part that is the browser's business rather than the
window's -- which backend to open, what the window is called, and the picture
on it -- and ``FEETBROWSER_DISPLAY`` is what selects the first:

    cocoa     the AppKit window, via ctypes (macOS)
    win32     the Win32 window, via ctypes
    x11       the Xlib window, via ctypes
    none      stay headless even where a window is possible

Empty -- the default -- means "use whatever this platform offers". Naming a
backend that cannot run here is an error rather than a silent fallback,
because asking for one and quietly getting a headless root is how you end up
with a blank screenshot and no idea why. It is read on every call rather than
at import, so a test can set it and a `--screenshot` run does not have to
care what the environment said.

`headless_root()` is always the headless root, which is what tests and
``--screenshot`` want. Opening a window on the screen is a separate, explicit
act: `new_window()`. Nothing gets a native window by accident.
"""
import os

import doormat
from doormat import window

#: The environment variable that names a backend. doormat has its own
#: ($DOORMAT_DISPLAY); an application with a variable of its own passes the
#: value in instead of exporting to that one, which is what we do.
DISPLAY_VAR = "FEETBROWSER_DISPLAY"

#: What the window is called before a page has loaded and named it.
TITLE = "FeetBrowser"

# The decoded icon, or None if the art is missing or unreadable, or False
# for "not looked yet". Decoding a 256x256 PNG is not free and every window
# wants the same pixels.
_ICON = False


def display():
    """The backend named in the environment, lowercased and stripped."""
    return os.environ.get(DISPLAY_VAR, "").strip().lower()


def icon():
    """The window icon as ``(width, height, rgba)``, or None.

    ``feetbrowser/icon.png`` is the same artwork the Windows and macOS
    bundles draw from, and only X11 wants it at runtime -- the other two take
    theirs from the bundle. It is decoded here rather than in doormat because
    a browser that puts a picture on its window already has a PNG decoder and
    a brand, and a windowing library has neither.

    A window without a picture on it is a far better outcome than a browser
    that will not start, so anything that goes wrong reading the art is
    simply no icon.
    """
    global _ICON
    if _ICON is False:
        from . import imagecodec
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "icon.png")
        try:
            with open(path, "rb") as f:
                _ICON = imagecodec.decode_png(f.read())
        except (OSError, imagecodec.ImageError):
            _ICON = None
    return _ICON


def Toplevel(master=None, **kwargs):
    """A secondary window of the same kind as its master."""
    kwargs.setdefault("title", TITLE)
    kwargs.setdefault("icon", icon())
    return doormat.Toplevel(master, **kwargs)


def platform_root():
    """The native root-window class for this platform, or None.

    Returns the class rather than an instance so callers can still decide not
    to open anything -- and so the import only happens when it is wanted.
    """
    return doormat.platform_root(display())


def display_problem():
    """A one-line explanation of why there is no window, or "".

    Only the reasons worth repeating survive: a backend that is simply for
    another operating system says nothing, because "Cocoa needs macOS" is
    noise on a Linux box that is missing its X server.
    """
    return doormat.display_problem()


def new_window(**kwargs):
    """A window on the screen if this platform has one, else a headless root.

    The fallback is not a failure mode: a headless root runs the whole browser
    faithfully, which is what tests and --screenshot rely on. It just has
    nowhere to put the pixels.
    """
    kwargs.setdefault("title", TITLE)
    kwargs.setdefault("icon", icon())
    return doormat.new_window(display(), **kwargs)


def headless_root(**kwargs):
    """A root that runs the browser with nowhere to put the pixels."""
    kwargs.setdefault("title", TITLE)
    return window.Tk(**kwargs)


def has_display():
    """True when new_window() would open something visible."""
    return doormat.has_display(display())
