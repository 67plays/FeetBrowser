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
"""
import os

BACKEND = os.environ.get("FEETBROWSER_BACKEND", "raster").strip().lower()


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
        "Toplevel": window.Toplevel,
        "Canvas": canvasmod.Canvas,
        "PhotoImage": canvasmod.PhotoImage,
        "TclError": canvasmod.TclError,
        "Font": canvasmod.Font,
        "name": "raster",
    }


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
