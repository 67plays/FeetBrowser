"""Picks the JavaScript engine and re-exports it under one set of names.

The browser talks to `Interpreter`, `JSException` and `UNDEFINED` and does
not care which engine is behind them. There are two:

    zig     our own engine, a dynamic library loaded with ctypes (the default)
    rust    the `feetbrowser_engine` extension module

Selection is by the ``FEETBROWSER_JS`` environment variable. Both are real
choices, not a primary and a fallback: they run the same test suite, and
having two implementations of the same contract is how a bug in either one
gets found.

Resolution is deferred to first use, the way `gui.py` defers picking a
rendering backend, so that merely importing this module never builds or
loads anything.
"""

import os

ENGINE = os.environ.get("FEETBROWSER_JS", "zig").strip().lower()

def _use_zig():
    from . import jszig
    return {
        "Interpreter": jszig.Interpreter,
        "JSException": jszig.JSException,
        "UNDEFINED": jszig.UNDEFINED,
        "name": "zig",
    }


def _use_rust():
    from feetbrowser_engine import Interpreter, JSException, UNDEFINED
    return {
        "Interpreter": Interpreter,
        "JSException": JSException,
        "UNDEFINED": UNDEFINED,
        "name": "rust",
    }


_impl = None

_NAMES = ("Interpreter", "JSException", "UNDEFINED")


def _resolve():
    global _impl, ENGINE
    if _impl is not None:
        return _impl
    if ENGINE == "rust":
        _impl = _use_rust()
    else:
        _impl = _use_zig()
    ENGINE = _impl["name"]
    return _impl


def engine():
    """The engine actually in use, resolving the choice if need be."""
    _resolve()
    return ENGINE


def __getattr__(name):
    if name in _NAMES:
        return _resolve()[name]
    raise AttributeError("module %r has no attribute %r" % (__name__, name))
