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
import importlib
import os

BACKEND = os.environ.get("FEETBROWSER_BACKEND", "raster").strip().lower()

# "cocoa", "x11", ... or "none" to stay headless even where a window is
# possible. Empty means "use whatever this platform offers".
DISPLAY = os.environ.get("FEETBROWSER_DISPLAY", "").strip().lower()

# The native window backends, tried in order when nothing was asked for by
# name: (module, label, the names that select it, the root class). Each one
# answers `available()` for itself, so a backend that cannot run here simply
# says so and the next is tried. Cocoa comes first because on the one system
# that has both, XQuartz is the deliberate choice and Cocoa is the default.
NATIVE_BACKENDS = (
    ("cocoa", "Cocoa", ("cocoa", "macos", "darwin"), "CocoaTk"),
    ("x11", "X11", ("x11", "linux", "xorg"), "X11Tk"),
)


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
    del _PROBLEMS[:]
    if DISPLAY == "none":
        return None
    for module, label, names, root in NATIVE_BACKENDS:
        asked = DISPLAY in names
        if DISPLAY and not asked:
            continue
        try:
            backend_module = importlib.import_module("." + module, __package__)
        except ImportError as exc:
            # Asking for a backend by name and silently getting a headless
            # root is the kind of thing you discover from an empty screenshot.
            if asked:
                raise RuntimeError("no %s window available here: %s"
                                   % (label, exc)) from exc
            continue
        if backend_module.available():
            return getattr(backend_module, root)
        # Backends say why they cannot run -- "DISPLAY is not set" is a very
        # different problem from "this is not Linux", and the difference is
        # the whole of what a user needs to hear.
        reason = backend_module.unavailable_reason()
        if reason:
            _PROBLEMS.append("%s: %s" % (label, reason))
        if asked:
            raise RuntimeError("no %s window available here: %s"
                               % (label, reason or "unsupported platform"))
    return None


# Why the last platform_root() found nothing, for callers that want to say so.
_PROBLEMS = []


def display_problem():
    """A one-line explanation of why there is no window, or "".

    Only the reasons worth repeating survive: a backend that is simply for
    another operating system says nothing, because "Cocoa needs macOS" is
    noise on a Linux box that is missing its X server.
    """
    return "; ".join(_PROBLEMS)


def backend():
    """The backend actually in use, resolving the choice if need be.

    Read the module's BACKEND directly only to see what was *asked* for:
    until something has been built, `auto` is still `auto`.
    """
    _resolve()
    return BACKEND


def new_window(**kwargs):
    """A window on the screen if this platform has one, else a headless root.

    The fallback is not a failure mode: a headless root runs the whole browser
    faithfully, which is what tests and --screenshot rely on. It just has
    nowhere to put the pixels.
    """
    if backend() == "raster":
        root = platform_root()
        if root is not None:
            return root(**kwargs)
    return _resolve()["Tk"](**kwargs)


def has_display():
    """True when new_window() would open something visible."""
    if backend() != "raster":
        return True  # tkinter brings its own window
    return platform_root() is not None


_impl = None

_NAMES = ("Tk", "Toplevel", "Canvas", "PhotoImage", "TclError", "Font")


def _resolve():
    """Pick the backend, once, the first time anything asks for it.

    Doing this at import time meant every `import feetbrowser.gui` -- and so
    every import of the browser, for any reason at all -- walked the system
    font directories first, and a machine with no usable fonts could not get
    as far as importing a symbol to report that.
    """
    global _impl, BACKEND
    if _impl is not None:
        return _impl
    if BACKEND == "tk":
        _impl = _use_tk()
    elif BACKEND == "auto":
        try:
            _impl = _use_raster()
        except Exception:  # noqa: BLE001 - any failure means fall back to Tk
            _impl = _use_tk()
    else:
        _impl = _use_raster()
    BACKEND = _impl["name"]
    return _impl


def __getattr__(name):
    if name in _NAMES:
        return _resolve()[name]
    raise AttributeError("module %r has no attribute %r" % (__name__, name))
