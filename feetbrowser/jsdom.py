"""Thin Python shims over the Rust DOM bridge (`feetbrowser_engine`).

The real DOM logic now lives in `rust/src/dom.rs`; these classes keep the
exact names/attributes the Tab and the jsengine interpreter expect and
delegate every `js_get`/`js_set` to the `dom_get`/`dom_set` pyfunctions.
Native methods are returned by Rust as callable `_DomMethod` objects, so no
`js_call` is needed here (and adding one would make these host objects look
callable to `typeof`).
"""

from feetbrowser_engine import dom_get, dom_set

_DOCUMENT = "document"
_ELEMENT = "element"
_NODELIST = "nodelist"
_CLASSLIST = "classlist"
_STYLE = "style"
_FONTS = "fonts"


class JSDocument:
    """Bridge for the document global: the root of the DOM."""

    def __init__(self, root_node, base_url=None, mark_dirty=None, interp=None):
        self.root = root_node
        self.base_url = base_url
        self.mark_dirty = mark_dirty
        self._interp = interp
        # Shared mutable flag: JS mutations set it; the Tab checks it after
        # running scripts to decide whether a restyle+rerender is needed.
        self._flag = {"dirty": False}

    def js_get(self, name):
        return dom_get(_DOCUMENT, self, name)

    def js_set(self, name, value):
        return dom_set(_DOCUMENT, self, name, value)


class JSElement:
    """Bridge for one Element node."""

    def __init__(self, node, _flag=None):
        self.node = node
        self._flag = _flag or {"dirty": False}

    def js_get(self, name):
        return dom_get(_ELEMENT, self, name)

    def js_set(self, name, value):
        return dom_set(_ELEMENT, self, name, value)


class JSNodeList:
    """Array-like view over a list of JSElements."""

    def __init__(self, items):
        self._items = items

    def js_get(self, name):
        return dom_get(_NODELIST, self, name)


class JSClassList:
    """Bridge for element.classList."""

    def __init__(self, node, _flag=None):
        self.node = node
        self._flag = _flag or {"dirty": False}

    def js_get(self, name):
        return dom_get(_CLASSLIST, self, name)


class JSFontFaceSet:
    """Minimal document.fonts: load/check/add/forEach/ready."""

    def __init__(self, _flag=None, interp=None):
        self._flag = _flag or {"dirty": False}
        self._faces = []
        self._interp = interp

    def js_get(self, name):
        return dom_get(_FONTS, self, name)


class JSElementStyle:
    """Bridge for an element's style dict with camelCase -> kebab mapping."""

    def __init__(self, node, _flag=None):
        self.node = node
        self._flag = _flag or {"dirty": False}

    def js_get(self, name):
        return dom_get(_STYLE, self, name)

    def js_set(self, name, value):
        return dom_set(_STYLE, self, name, value)