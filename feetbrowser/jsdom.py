"""Thin Python shims over the Rust DOM bridge (`feetbrowser_engine`).

The real DOM logic now lives in `rust/src/dom.rs`; these classes keep the
exact names/attributes the Tab and the jsengine interpreter expect and
delegate every `js_get`/`js_set` to the `dom_get`/`dom_set` pyfunctions.
Native methods are returned by Rust as callable `_DomMethod` objects, so no
`js_call` is needed here (and adding one would make these host objects look
callable to `typeof`).
"""

from urllib.parse import urlsplit

from feetbrowser_engine import dom_get, dom_set, UNDEFINED

_DOCUMENT = "document"
_ELEMENT = "element"
_NODELIST = "nodelist"
_CLASSLIST = "classlist"
_STYLE = "style"
_FONTS = "fonts"


class JSLocation:
    """Bridge for `window.location` / `document.location`.

    Reads expose the parsed parts of the current page URL. Writes — assigning
    `href`, or calling `assign`/`replace`/`reload` — hand a navigation request
    to the host's `navigate(url_str, replace)` callback, which the Tab wires to
    its load pipeline. This is how JS-driven redirects navigate the browser
    (e.g. DuckDuckGo's `window.parent.location.replace(...)`).
    """

    def __init__(self, base_url=None, navigate=None):
        self.base_url = base_url
        self._navigate = navigate

    def _parts(self):
        base = str(self.base_url) if self.base_url else ""
        try:
            return urlsplit(base)
        except Exception:
            return None

    def js_get(self, name):
        if name in ("assign", "replace", "reload"):
            return getattr(self, "_" + name)
        parts = self._parts()
        if name == "href":
            return str(self.base_url) if self.base_url else ""
        if parts is None or not parts.scheme:
            return "" if name in (
                "hostname", "protocol", "pathname", "search", "hash",
                "host", "origin", "port") else UNDEFINED
        if name == "hostname":
            return parts.hostname or ""
        if name == "protocol":
            return parts.scheme + ":"
        if name == "pathname":
            return parts.path
        if name == "search":
            return "?" + parts.query if parts.query else ""
        if name == "hash":
            return parts.fragment or ""
        if name == "host":
            return parts.netloc
        if name == "origin":
            return f"{parts.scheme}://{parts.netloc}"
        if name == "port":
            return str(parts.port or "")
        return UNDEFINED

    def js_set(self, name, value):
        if name == "href":
            self._navigate_url(value, replace=False)

    def _assign(self, url=None):
        self._navigate_url(url, replace=False)
        return UNDEFINED

    def _replace(self, url=None):
        self._navigate_url(url, replace=True)
        return UNDEFINED

    def _reload(self):
        if self._navigate is not None and self.base_url is not None:
            self._navigate(str(self.base_url), replace=True)
        return UNDEFINED

    def _navigate_url(self, url, replace):
        if url is None or url is UNDEFINED:
            return
        if self._navigate is not None:
            self._navigate(str(url), replace)


class JSDocument:
    """Bridge for the document global: the root of the DOM."""

    def __init__(self, root_node, base_url=None, mark_dirty=None, interp=None,
                 location=None):
        self.root = root_node
        self.base_url = base_url
        self.mark_dirty = mark_dirty
        self._interp = interp
        self._location_obj = location
        # Shared mutable flag: JS mutations set it; the Tab checks it after
        # running scripts to decide whether a restyle+rerender is needed. It
        # also carries the live interpreter so DOM shims (NodeList.forEach,
        # getComputedStyle) can call back into JS.
        self._flag = {"dirty": False, "interp": interp}

    def js_get(self, name):
        if name == "location" and self._location_obj is not None:
            return self._location_obj
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

    def __init__(self, items, _flag=None):
        self._items = items
        self._flag = _flag or {"dirty": False}

    def js_get(self, name):
        return dom_get(_NODELIST, self, name)


class JSFragment:
    """DocumentFragment shim: an owned child list. appendChild/append
    accumulate children; appending the fragment to an element moves them
    (handled in dom.rs appendChild)."""

    def __init__(self, node=None, _flag=None):
        self._flag = _flag or {"dirty": False}
        self._items = []

    def js_get(self, name):
        if name in ("append", "appendChild"):
            return getattr(self, "_" + name)
        if name == "childElementCount":
            return sum(1 for i in self._items if hasattr(i, "node"))
        if name == "children":
            return JSNodeList(self._items, self._flag)
        if name == "textContent":
            return "".join(i.textContent for i in self._items
                           if hasattr(i, "textContent"))
        return UNDEFINED

    def js_set(self, name, value):
        return UNDEFINED

    def _appendChild(self, *children):
        for c in children:
            if hasattr(c, "js_unwrap"):
                c = c.js_unwrap()
            self._items.append(c)
        return children[-1] if children else UNDEFINED

    def _append(self, *args):
        return self._appendChild(*args)


class JSClassList:
    """Bridge for element.classList."""

    def __init__(self, node, _flag=None):
        self.node = node
        self._flag = _flag or {"dirty": False}

    def js_get(self, name):
        return dom_get(_CLASSLIST, self, name)


class _JSStaticProps:
    """Read-only property bag for environment globals (navigator, screen,
    matchMedia results): property reads return the captured dict, methods and
    writes are inert."""

    def __init__(self, props):
        self._props = dict(props)

    def js_get(self, name):
        if name in self._props:
            return self._props[name]
        return UNDEFINED

    def js_set(self, name, value):
        return UNDEFINED

    def js_call(self, name, *args):
        return UNDEFINED


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
        return UNDEFINED

    def _getPropertyValue(self, prop):
        value = self._snapshot().get(str(prop))
        if value is None:
            return ""
        return value

    def _setProperty(self, *args):
        return UNDEFINED

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