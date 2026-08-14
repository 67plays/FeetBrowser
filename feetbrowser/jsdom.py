"""Picks the DOM bridge that goes with the chosen JavaScript engine.

The bridge is the set of host objects a script sees as `document`,
`location`, elements, node lists and so on. It cannot be shared between the
engines: the Rust one keeps the logic in `rust/src/dom.rs` and its shims call
straight into that extension, while the Zig one drives the same htmlparser
node tree from Python. Both present the same class names, so the Tab wires
either one the same way.
"""

from . import jsengine

_impl = None

_NAMES = ("JSDocument", "JSLocation", "JSElement", "JSNodeList",
          "JSClassList", "JSFontFaceSet", "JSElementStyle")


def _resolve():
    global _impl
    if _impl is not None:
        return _impl
    if jsengine.engine() == "rust":
        from . import jsdom_rust as impl
    else:
        from . import jsdom_py as impl
    _impl = impl
    return impl


def __getattr__(name):
    if name in _NAMES:
        return getattr(_resolve(), name)
    raise AttributeError("module %r has no attribute %r" % (__name__, name))
