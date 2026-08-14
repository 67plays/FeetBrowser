"""Picks the DOM bridge that goes with the chosen JavaScript engine.

The bridge is the set of host objects a script sees as `document`,
`location`, elements, node lists and so on. It cannot be shared between the
engines: the Rust one keeps the logic in `rust/src/dom.rs` and its shims call
straight into that extension, while the Zig one drives the same htmlparser
node tree from Python. Both present the same class names, so the Tab wires
either one the same way.

Two host objects do not depend on the engine at all and live here rather
than in either bridge. `_JSStaticProps` and `_JSComputedStyle` read from
plain Python dicts and from the cascaded `node.style`, never from the DOM
tree, so both engines get the same ones.
"""

from . import jsengine

_impl = None

_NAMES = ("JSDocument", "JSLocation", "JSElement", "JSNodeList",
          "JSClassList", "JSFontFaceSet", "JSElementStyle", "JSFragment")


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


class _JSStaticProps:
    """Read-only property bag for environment globals (navigator, screen,
    matchMedia results): property reads return the captured dict, methods and
    writes are inert."""

    def __init__(self, props):
        self._props = dict(props)

    def js_get(self, name):
        if name in self._props:
            return self._props[name]
        return jsengine.UNDEFINED

    def js_set(self, name, value):
        return jsengine.UNDEFINED

    def js_call(self, *args):
        return jsengine.UNDEFINED


class _JSComputedStyle:
    """Bridge for `getComputedStyle(el)`: camelCase reads and
    `getPropertyValue()` resolve against the cascaded node.style dict."""

    def __init__(self, node):
        self.node = node
        self._style = getattr(node, "style", {}) if node is not None else {}

    def _snapshot(self):
        # Re-read so restyles after mutations are reflected.
        return getattr(self.node, "style", {}) if self.node is not None else {}

    def js_get(self, name):
        if name in ("getPropertyValue", "setProperty", "getPropertyPriority"):
            return getattr(self, "_" + name)
        if name == "cssText":
            return "; ".join(
                f"{k}: {v}" for k, v in self._snapshot().items())
        value = self._snapshot().get(_kebab(name))
        if value is None:
            return ""
        return value

    def js_set(self, name, value):
        return jsengine.UNDEFINED

    def _getPropertyValue(self, prop):
        value = self._snapshot().get(str(prop))
        if value is None:
            return ""
        return value

    def _setProperty(self, *args):
        return jsengine.UNDEFINED

    def _getPropertyPriority(self, *args):
        return ""


def _kebab(name):
    out = []
    for ch in name:
        if ch.isupper():
            out.append("-")
            out.append(ch.lower())
        else:
            out.append(ch)
    return "".join(out)


def __getattr__(name):
    if name in _NAMES:
        return getattr(_resolve(), name)
    raise AttributeError("module %r has no attribute %r" % (__name__, name))
