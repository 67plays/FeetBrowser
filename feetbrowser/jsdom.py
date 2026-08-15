"""The DOM bridge for the JavaScript engine.

The bridge is the set of host objects a script sees as `document`,
`location`, elements, node lists and so on. It belongs to the Rust engine:
the logic lives in `rust/src/dom.rs` and the shims in `jsdom_rust.py` call
straight into that extension, so the Tab wires it in directly.

Two host objects do not depend on the engine at all and live here rather
than in the bridge. `_JSStaticProps` and `_JSComputedStyle` read from plain
Python dicts and from the cascaded `node.style`, never from the DOM tree.
"""

from . import jsengine
from .jsdom_rust import (
    JSDocument, JSLocation, JSElement, JSNodeList,
    JSClassList, JSFontFaceSet, JSElementStyle, JSFragment,
)

__all__ = ["JSDocument", "JSLocation", "JSElement", "JSNodeList",
           "JSClassList", "JSFontFaceSet", "JSElementStyle", "JSFragment"]


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
