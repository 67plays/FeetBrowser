"""Picks the GUI backend and re-exports it under one set of names.

Everything above this module -- the layout engine, the browser chrome, the
toe plugins -- talks to `Tk`, `Toplevel`, `Canvas`, `PhotoImage`, `Font` and
`TclError` without caring where they come from. That indirection is the whole
point: the raster backend draws every pixel itself, and Tk remains available
as a fallback for anyone who wants to compare the two.

Selection is by the ``FEETBROWSER_BACKEND`` environment variable:

    raster    our own font engine, rasteriser and event loop (the default)
    tk        the original tkinter widgets
    auto      raster, falling back to tk if the font engine finds no fonts

``Tk`` is always the headless root, which is what tests and ``--screenshot``
want. Opening a window on the screen is a separate, explicit act:
``new_window()``. Nothing gets a native window by accident.
"""
import os

BACKEND = os.environ.get("FEETBROWSER_BACKEND", "raster").strip().lower()

# "cocoa", "x11", ... or "none" to stay headless even where a window is
# possible. Empty means "use whatever this platform offers".
DISPLAY = os.environ.get("FEETBROWSER_DISPLAY", "").strip().lower()


def _use_tk():
    import tkinter
    import tkinter.font
    return {
        "Tk": tkinter.Tk,
        "Toplevel": tkinter.Toplevel,
        "Canvas": tkinter.Canvas,
        "PhotoImage": tkinter.PhotoImage,
        "TclError": tkinter.TclError,
        "Font": tkinter.font.Font,
        "name": "tk",
    }


def _use_raster():
    from . import canvas as canvasmod
    from . import fontengine, window
    if not fontengine.index():
        raise RuntimeError("no usable fonts found on this system")
    return {
        "Tk": window.Tk,
        "Toplevel": _toplevel,
        "Canvas": canvasmod.Canvas,
        "PhotoImage": canvasmod.PhotoImage,
        "TclError": canvasmod.TclError,
        "Font": canvasmod.Font,
        "name": "raster",
    }


def _toplevel(master=None, **kwargs):
    """A secondary window of the same kind as its master."""
    from . import window
    factory = getattr(master, "toplevel_class", None)
    if factory is not None:
        return factory(master, **kwargs)
    return window.Toplevel(master, **kwargs)


def platform_root():
    """The native root-window class for this platform, or None.

    Returns the class rather than an instance so callers can still decide not
    to open anything -- and so the import only happens when it is wanted.
    """
    if DISPLAY == "none":
        return None
    if DISPLAY in ("", "cocoa"):
        try:
            from . import cocoa
        except ImportError:
            return None
        if cocoa.available():
            return cocoa.CocoaTk
        if DISPLAY == "cocoa":
            raise RuntimeError("no Cocoa window available here")
    return None


def new_window(**kwargs):
    """A window on the screen if this platform has one, else a headless root.

    The fallback is not a failure mode: a headless root runs the whole browser
    faithfully, which is what tests and --screenshot rely on. It just has
    nowhere to put the pixels.
    """
    if BACKEND == "raster":
        root = platform_root()
        if root is not None:
            return root(**kwargs)
    return Tk(**kwargs)


def has_display():
    """True when new_window() would open something visible."""
    if BACKEND != "raster":
        return True  # tkinter brings its own window
    return platform_root() is not None


if BACKEND == "tk":
    _impl = _use_tk()
elif BACKEND == "auto":
    try:
        _impl = _use_raster()
    except Exception:  # noqa: BLE001 - any failure means fall back to Tk
        _impl = _use_tk()
else:
    _impl = _use_raster()

Tk = _impl["Tk"]
Toplevel = _impl["Toplevel"]
Canvas = _impl["Canvas"]
PhotoImage = _impl["PhotoImage"]
TclError = _impl["TclError"]
Font = _impl["Font"]
BACKEND = _impl["name"]
