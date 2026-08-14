"""A small JavaScript DOM bridge over the raw htmlparser DOM tree.

Every wrapper here is a "host object" for the jsengine interpreter: it
implements js_get/js_set so member reads, writes and native method calls
resolve against the underlying htmlparser nodes. Nothing in this module may
import from browser (that would be a circular import); the Tab wires the
bridge into the interpreter in browser.py.
"""

import re
from urllib.parse import urlsplit

from .htmlparser import HTMLParser, Text, Element
from .jsengine import UNDEFINED

# Tags serialized as self-closing voids by the innerHTML serializer.
VOID_TAGS = {"br", "img", "hr", "input", "meta", "link", "base"}

# A DocumentFragment is a parentless element with a tag no parser can produce,
# so nothing else in the tree can collide with it.
FRAGMENT_TAG = "#fragment"


def _clone(node, deep):
    """Copy an element, and its subtree when asked."""
    if isinstance(node, Text):
        return Text(node.text, None)
    copy = Element(node.tag, dict(node.attributes), None)
    if deep:
        copy.children = [_clone(c, True) for c in node.children]
        for child in copy.children:
            child.parent = copy
    return copy


def _walk(node):
    """Yield `node` then its descendants in document order."""
    stack = [node]
    while stack:
        n = stack.pop()
        yield n
        for child in reversed(n.children):
            stack.append(child)


def _iter_elements(node):
    """Yield the Element nodes under `node` in document order."""
    for n in _walk(node):
        if isinstance(n, Element):
            yield n


def _escape_text(text):
    return (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def _escape_attr(value):
    return _escape_text(value).replace('"', "&quot;")


def serialize_element(el):
    attrs = "".join(f' {name}="{_escape_attr(value)}"'
                    for name, value in el.attributes.items())
    if el.tag in VOID_TAGS:
        return f"<{el.tag}{attrs}>"
    return f"<{el.tag}{attrs}>{serialize(el.children)}</{el.tag}>"


def serialize(children):
    """Serialize a list of raw nodes to an HTML string."""
    return "".join(_escape_text(c.text) if isinstance(c, Text)
                   else serialize_element(c) for c in children
                   if isinstance(c, (Text, Element)))


def _camel_to_kebab(name):
    return re.sub(r"(?<!^)([A-Z])", r"-\1", name).lower()


def _parse_selector(sel):
    """Parse a simple selector: tag, #id, .class, or combinations.

    Returns (tag_or_None, {classes}, {ids}) or None if unsupported.
    """
    parts = re.split(r"(?=[#.])", sel.strip())
    tag = None
    classes = set()
    ids = set()
    for part in parts:
        if part.startswith("#"):
            ids.add(part[1:])
        elif part.startswith("."):
            classes.add(part[1:])
        elif part:
            if not re.fullmatch(r"[a-zA-Z][\w-]*", part):
                return None
            tag = part.lower()
    return tag, classes, ids


def _selector_hits(parsed, node):
    """Does one element satisfy a parsed selector?"""
    tag, classes, ids = parsed
    if tag and node.tag != tag:
        return False
    if ids and node.attributes.get("id") not in ids:
        return False
    if classes and not classes.issubset(
            set(node.attributes.get("class", "").split())):
        return False
    return True


def _int_index(name):
    try:
        return int(name)
    except (TypeError, ValueError):
        return None


class JSLocation:
    """Bridge for `window.location` / `document.location`.

    Reads expose the parsed parts of the current page URL. Writes -- assigning
    `href`, or calling `assign`/`replace`/`reload` -- hand a navigation request
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
        except Exception:  # noqa: BLE001 - a malformed URL has no parts
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
        # running scripts to decide whether a restyle+rerender is needed.
        self._flag = {"dirty": False}
        self._methods = {
            "getElementById": self._get_element_by_id,
            "querySelector": self._query_selector,
            "querySelectorAll": self._query_selector_all,
            "getElementsByTagName": self._get_elements_by_tag_name,
            "getElementsByClassName": self._get_elements_by_class_name,
            "createElement": self._create_element,
            "createTextNode": self._create_text_node,
            "createDocumentFragment": self._create_document_fragment,
            "addEventListener": self._add_event_listener,
            "removeEventListener": self._remove_event_listener,
        }

    def js_get(self, name):
        if name in self._methods:
            return self._methods[name]
        if name == "body":
            return self._find(lambda n: n.tag == "body")
        if name == "head":
            return self._find(lambda n: n.tag == "head")
        if name == "title":
            return self._get_title()
        if name == "documentElement":
            return JSElement(self.root, self._flag)
        if name == "readyState":
            return "complete"
        if name == "cookie":
            return ""
        if name == "referrer":
            return ""
        if name == "domain":
            return self._host() or ""
        if name == "URL":
            return str(self.base_url) if self.base_url else ""
        if name == "location":
            if self._location_obj is not None:
                return self._location_obj
            return self._location()
        if name == "visibilityState":
            return "visible"
        if name == "hidden":
            return False
        if name == "characterSet":
            return "UTF-8"
        if name == "contentType":
            return "text/html"
        if name == "fonts":
            return JSFontFaceSet(self._flag, self._interp)
        if name == "defaultView":
            return UNDEFINED
        if name == "all":
            return JSNodeList([JSElement(n, self._flag)
                               for n in _iter_elements(self.root)])
        if name == "scripts":
            return JSNodeList([JSElement(n, self._flag)
                               for n in _iter_elements(self.root)
                               if n.tag == "script"])
        if name == "images":
            return JSNodeList([JSElement(n, self._flag)
                               for n in _iter_elements(self.root)
                               if n.tag == "img"])
        return UNDEFINED

    def js_set(self, name, value):
        if name == "title":
            self._set_title(value)
            self._flag["dirty"] = True
        elif name == "cookie":
            self._flag["dirty"] = True

    def _find(self, pred):
        for n in _iter_elements(self.root):
            if pred(n):
                return JSElement(n, self._flag)
        return UNDEFINED

    def _get_element_by_id(self, element_id):
        return self._find(lambda n: n.attributes.get("id") == element_id)

    def _query_selector(self, sel):
        parsed = _parse_selector(sel)
        if parsed is None:
            return UNDEFINED
        return self._find(lambda n: _selector_hits(parsed, n))

    def _get_elements_by_tag_name(self, tag):
        tag = str(tag).lower()
        return JSNodeList([JSElement(n, self._flag)
                           for n in _iter_elements(self.root)
                           if tag == "*" or n.tag == tag])

    def _get_elements_by_class_name(self, cls):
        cls = str(cls)
        return JSNodeList([JSElement(n, self._flag)
                           for n in _iter_elements(self.root)
                           if cls in set(n.attributes.get("class", "").split())])

    def _query_selector_all(self, sel):
        parsed = _parse_selector(sel)
        if parsed is None:
            return JSNodeList([])
        tag, classes, ids = parsed
        return JSNodeList([JSElement(n, self._flag)
                           for n in _iter_elements(self.root)
                           if isinstance(n, Element)
                           and (not tag or n.tag == tag)
                           and (not ids or n.attributes.get("id") in ids)
                           and (not classes or classes.issubset(
                               set(n.attributes.get("class", "").split())))])

    def _host(self):
        try:
            from urllib.parse import urlparse
            return urlparse(str(self.base_url)).hostname
        except Exception:
            return ""

    def _location(self):
        base = str(self.base_url) if self.base_url else ""
        from urllib.parse import urlsplit
        try:
            parts = urlsplit(base)
        except Exception:
            parts = None
        if parts is None or not parts.scheme:
            return {"href": base, "hostname": "", "protocol": "",
                    "pathname": "", "search": "", "hash": "",
                    "host": "", "origin": "", "port": "",
                    "reload": lambda: None, "assign": lambda u=None: None,
                    "replace": lambda u=None: None}
        return {
            "href": base,
            "hostname": parts.hostname or "",
            "protocol": parts.scheme + ":",
            "pathname": parts.path,
            "search": "?" + parts.query if parts.query else "",
            "hash": "",
            "host": parts.netloc,
            "origin": f"{parts.scheme}://{parts.netloc}",
            "port": str(parts.port or ""),
            "reload": lambda: None,
            "assign": lambda u=None: None,
            "replace": lambda u=None: None,
        }

    def _create_element(self, tag):
        return JSElement(Element(tag, {}, None), self._flag)

    def _create_text_node(self, text):
        if text is None or text is UNDEFINED:
            text = ""
        return JSText(Text(str(text), None), self._flag)

    def _add_event_listener(self, event_type, fn, *_options):
        # Document-level listeners hang off the root node, which is where the
        # Tab looks when it dispatches.
        return JSElement(self.root, self._flag)._add_event_listener(
            event_type, fn)

    def _remove_event_listener(self, event_type, fn, *_options):
        return JSElement(self.root, self._flag)._remove_event_listener(
            event_type, fn)

    def _create_document_fragment(self):
        return JSFragment(_flag=self._flag)

    def _get_title(self):
        for n in _iter_elements(self.root):
            if n.tag == "title":
                text = "".join(c.text for c in n.children
                               if isinstance(c, Text)).strip()
                if text:
                    return text
        return ""

    def _set_title(self, value):
        if value is None or value is UNDEFINED:
            value = ""
        for n in _iter_elements(self.root):
            if n.tag == "title":
                n.children = [Text(str(value), n)]
                return
        head = next((c for c in self.root.children
                     if isinstance(c, Element) and c.tag == "head"), None)
        title = Element("title", {}, head if head is not None else self.root)
        title.children = [Text(str(value), title)]
        if head is not None:
            head.children.append(title)
        else:
            self.root.children.insert(0, title)


class JSText:
    """Bridge for one Text node, as `document.createTextNode` returns.

    It is not a JSElement: a text node has no tag, no attributes and no
    children, and letting one pretend otherwise puts a bogus element in the
    tree. Only what a script can usefully read or write is here.
    """

    def __init__(self, node, _flag=None):
        self.node = node
        self._flag = _flag or {"dirty": False}

    def js_get(self, name):
        if name in ("textContent", "nodeValue", "data", "wholeText"):
            return self.node.text
        if name == "nodeType":
            return 3
        if name == "nodeName":
            return "#text"
        if name == "length":
            return len(self.node.text)
        if name == "parentNode":
            parent = self.node.parent
            return JSElement(parent, self._flag) if parent else UNDEFINED
        return UNDEFINED

    def js_set(self, name, value):
        if name in ("textContent", "nodeValue", "data"):
            self.node.text = "" if value is None or value is UNDEFINED \
                else str(value)
            self._flag["dirty"] = True


class JSElement:
    """Bridge for one Element node."""

    def __init__(self, node, _flag=None):
        self.node = node
        self._flag = _flag or {"dirty": False}
        self._methods = {
            "setAttribute": self._set_attribute,
            "getAttribute": self._get_attribute,
            "removeAttribute": self._remove_attribute,
            "hasAttribute": self._has_attribute,
            "appendChild": self._append_child,
            "removeChild": self._remove_child,
            "addEventListener": self._add_event_listener,
            "removeEventListener": self._remove_event_listener,
            "querySelector": self._query_selector,
            "querySelectorAll": self._query_selector_all,
            "getElementsByClassName": self._get_elements_by_class_name,
            "getElementsByTagName": self._get_elements_by_tag_name,
            "insertBefore": self._insert_before,
            "cloneNode": self._clone_node,
            "contains": self._contains,
            "remove": self._remove,
            "matches": self._matches,
            "closest": self._closest,
        }

    def js_get(self, name):
        if name in self._methods:
            return self._methods[name]
        if name == "textContent":
            return "".join(n.text for n in _walk(self.node)
                           if isinstance(n, Text))
        if name == "innerHTML":
            return serialize(self.node.children)
        if name == "outerHTML":
            return serialize_element(self.node)
        if name == "tagName":
            return self.node.tag.upper()
        if name == "tag":
            return self.node.tag
        if name == "nodeType":
            return 1
        if name == "nodeName":
            return self.node.tag.upper()
        # Text nodes have no wrapper, so the sibling and child walks below
        # skip them. Scripts that count children see only elements.
        if name in ("firstChild", "firstElementChild"):
            return self._first_element()
        if name in ("lastChild", "lastElementChild"):
            return self._last_element()
        if name in ("nextSibling", "nextElementSibling"):
            return self._sibling(1)
        if name in ("previousSibling", "previousElementSibling"):
            return self._sibling(-1)
        if name == "childNodes":
            return self.js_get("children")
        if name == "children":
            return JSNodeList([JSElement(c, self._flag) for c in self.node.children
                               if isinstance(c, Element)])
        if name == "childElementCount":
            return len(self._element_children())
        if name == "parentNode":
            return JSElement(self.node.parent, self._flag) if self.node.parent else UNDEFINED
        if name == "classList":
            return JSClassList(self.node, self._flag)
        if name == "dataset":
            return self._dataset()
        if name == "id":
            return self.node.attributes.get("id", "")
        if name == "className":
            return self.node.attributes.get("class", "")
        if name == "style":
            return JSElementStyle(self.node, self._flag)
        if isinstance(name, str) and name in self.node.attributes:
            return self.node.attributes[name]
        return UNDEFINED

    def js_set(self, name, value):
        if name == "textContent":
            self._set_text_content(value)
        elif name == "innerHTML":
            self._set_inner_html(value)
        # style writes are no-ops; mutations go through JSElementStyle.

    # -- native methods -------------------------------------------------

    def _set_attribute(self, name, value):
        self.node.attributes[str(name)] = str(value)
        self._flag["dirty"] = True
        return UNDEFINED

    def _get_attribute(self, name):
        return self.node.attributes.get(str(name))

    def _remove_attribute(self, name):
        self.node.attributes.pop(str(name), None)
        self._flag["dirty"] = True
        return UNDEFINED

    def _has_attribute(self, name):
        return str(name) in self.node.attributes

    def _query_selector(self, sel):
        parsed = _parse_selector(sel)
        if parsed is None:
            return UNDEFINED
        for n in _iter_elements(self.node):
            if n is not self.node and _selector_hits(parsed, n):
                return JSElement(n, self._flag)
        return UNDEFINED

    def _query_selector_all(self, sel):
        parsed = _parse_selector(sel)
        if parsed is None:
            return JSNodeList([])
        return JSNodeList([JSElement(n, self._flag)
                           for n in _iter_elements(self.node)
                           if n is not self.node
                           and _selector_hits(parsed, n)])

    def _get_elements_by_class_name(self, cls):
        cls = str(cls)
        return JSNodeList([JSElement(n, self._flag)
                           for n in _iter_elements(self.node)
                           if n is not self.node and cls in
                           set(n.attributes.get("class", "").split())])

    def _get_elements_by_tag_name(self, tag):
        tag = str(tag).lower()
        return JSNodeList([JSElement(n, self._flag)
                           for n in _iter_elements(self.node)
                           if n is not self.node
                           and (tag == "*" or n.tag == tag)])

    def _dataset(self):
        data = {}
        for k, v in self.node.attributes.items():
            if k.startswith("data-"):
                key = k[5:]
                parts = key.split("-")
                data[parts[0] + "".join(p.title() for p in parts[1:])] = v
        return data

    def _append_child(self, child):
        if not isinstance(child, (JSElement, JSText)):
            return UNDEFINED
        raw = child.node
        if isinstance(raw, Element) and raw.tag == FRAGMENT_TAG:
            # A fragment is not itself inserted: appending it moves its
            # children and leaves it empty, which is the whole point of
            # building a subtree off to one side and grafting it on at once.
            moved = list(raw.children)
            raw.children = []
            for node in moved:
                node.parent = self.node
                self.node.children.append(node)
            self._flag["dirty"] = True
            return child
        raw.parent = self.node
        self.node.children.append(raw)
        self._flag["dirty"] = True
        return child

    def _remove(self):
        parent = self.node.parent
        if parent is None:
            return UNDEFINED
        try:
            parent.children.remove(self.node)
        except ValueError:
            return UNDEFINED
        self.node.parent = None
        self._flag["dirty"] = True
        return UNDEFINED

    def _element_children(self):
        return [c for c in self.node.children if isinstance(c, Element)]

    def _first_element(self):
        kids = self._element_children()
        return JSElement(kids[0], self._flag) if kids else UNDEFINED

    def _last_element(self):
        kids = self._element_children()
        return JSElement(kids[-1], self._flag) if kids else UNDEFINED

    def _sibling(self, step):
        parent = self.node.parent
        if parent is None:
            return UNDEFINED
        kids = [c for c in parent.children if isinstance(c, Element)]
        try:
            i = kids.index(self.node) + step
        except ValueError:
            return UNDEFINED
        if 0 <= i < len(kids):
            return JSElement(kids[i], self._flag)
        return UNDEFINED

    def _insert_before(self, child, ref=UNDEFINED):
        if not isinstance(child, (JSElement, JSText)):
            return UNDEFINED
        raw = child.node
        raw.parent = self.node
        if isinstance(ref, (JSElement, JSText)) and ref.node in self.node.children:
            self.node.children.insert(self.node.children.index(ref.node), raw)
        else:
            self.node.children.append(raw)
        self._flag["dirty"] = True
        return child

    def _clone_node(self, deep=False):
        return JSElement(_clone(self.node, bool(deep)), self._flag)

    def _contains(self, other):
        if not isinstance(other, JSElement):
            return False
        return any(n is other.node for n in _walk(self.node))

    def _matches(self, sel):
        parsed = _parse_selector(sel)
        return parsed is not None and _selector_hits(parsed, self.node)

    def _closest(self, sel):
        parsed = _parse_selector(sel)
        if parsed is None:
            return UNDEFINED
        node = self.node
        while node is not None:
            if isinstance(node, Element) and _selector_hits(parsed, node):
                return JSElement(node, self._flag)
            node = node.parent
        return UNDEFINED

    def _remove_child(self, child):
        if not isinstance(child, (JSElement, JSText)):
            return UNDEFINED
        raw = child.node
        try:
            self.node.children.remove(raw)
        except ValueError:
            return UNDEFINED
        raw.parent = None
        self._flag["dirty"] = True
        return child

    def _add_event_listener(self, event_type, fn, *_options):
        handlers = getattr(self.node, "_js_handlers", None)
        if handlers is None:
            handlers = {}
            self.node._js_handlers = handlers
        handlers.setdefault(str(event_type), []).append(fn)
        return UNDEFINED

    def _remove_event_listener(self, event_type, fn, *_options):
        handlers = getattr(self.node, "_js_handlers", None) or {}
        for i, h in enumerate(handlers.get(str(event_type), [])):
            if h is fn:
                del handlers[str(event_type)][i]
                break
        return UNDEFINED

    # -- property setters / getters ------------------------------------

    def _set_text_content(self, value):
        if value is None or value is UNDEFINED:
            value = ""
        self.node.children = [Text(str(value), self.node)]
        self._flag["dirty"] = True

    def _set_inner_html(self, value):
        value = "" if value is None or value is UNDEFINED else str(value)
        root = HTMLParser(value).parse()
        source = next((n.children for n in _iter_elements(root)
                       if n.tag == "body"), root.children)
        for child in source:
            child.parent = self.node
        self.node.children = list(source)
        self._flag["dirty"] = True


class JSNodeList:
    """Array-like view over a list of JSElements."""

    def __init__(self, items):
        self._items = items

    def js_get(self, name):
        if name == "length":
            return len(self._items)
        if name == "item":
            return self._item
        if name == "forEach":
            return self._for_each
        # Property names cross the bridge as strings, so an index arrives
        # looking like one.
        index = name if isinstance(name, int) else _int_index(name)
        if index is not None and 0 <= index < len(self._items):
            return self._items[index]
        return UNDEFINED

    def _item(self, index):
        try:
            i = int(index)
        except (TypeError, ValueError):
            return UNDEFINED
        return self._items[i] if 0 <= i < len(self._items) else UNDEFINED

    def _for_each(self, fn, *_rest):
        for i, item in enumerate(self._items):
            fn(item, i, self)
        return UNDEFINED


class JSFragment(JSElement):
    """DocumentFragment: a parentless element whose tag no parser can
    produce, so it can hold children before they are grafted on and never
    collide with a real element. `appendChild` on the destination is what
    moves them across.
    """

    def __init__(self, node=None, _flag=None):
        super().__init__(Element(FRAGMENT_TAG, {}, None), _flag)


class JSClassList:
    """Bridge for element.classList."""

    def __init__(self, node, _flag=None):
        self.node = node
        self._flag = _flag or {"dirty": False}

    def _classes(self):
        return set(self.node.attributes.get("class", "").split())

    def _save(self, classes):
        self.node.attributes["class"] = " ".join(sorted(classes))
        self._flag["dirty"] = True

    def js_get(self, name):
        if name == "length":
            return len(self._classes())
        if name == "add":
            return self._add
        if name == "remove":
            return self._remove
        if name == "contains":
            return lambda cls: str(cls) in self._classes()
        if name == "toggle":
            return self._toggle
        index = _int_index(name)
        if index is not None:
            items = sorted(self._classes())
            if 0 <= index < len(items):
                return items[index]
        return UNDEFINED

    def _add(self, *classes):
        cs = self._classes()
        cs.update(str(c) for c in classes)
        self._save(cs)
        return UNDEFINED

    def _remove(self, *classes):
        cs = self._classes()
        cs.difference_update(str(c) for c in classes)
        self._save(cs)
        return UNDEFINED

    def _toggle(self, cls, force=UNDEFINED):
        cs = self._classes()
        name = str(cls)
        if force is True or (force is UNDEFINED and name not in cs):
            cs.add(name)
            self._save(cs)
            return True
        cs.discard(name)
        self._save(cs)
        return False


class JSFontFaceSet:
    """Minimal document.fonts: load/check/add/forEach/ready."""

    def __init__(self, _flag=None, interp=None):
        self._flag = _flag or {"dirty": False}
        self._faces = []
        self._interp = interp

    def js_get(self, name):
        if name == "add":
            return lambda f: self._faces.append(f)
        if name == "load":
            return lambda *args: UNDEFINED
        if name == "check":
            return lambda *args: True
        if name == "forEach":
            return lambda cb: None
        if name == "ready":
            return self._interp.create_promise() if self._interp else UNDEFINED
        return UNDEFINED


class JSElementStyle:
    """Bridge for an element's style dict with camelCase -> kebab mapping.

    `style()` reassigns node.style on every restyle, so JS-driven writes are
    also kept on the node in `_js_style_overrides` and re-applied by the Tab's
    `_js_mutated` after it re-cascades the stylesheet. Overrides always win
    (inline JS beats author rules).
    """

    def __init__(self, node, _flag=None):
        self.node = node
        self._flag = _flag or {"dirty": False}

    def _overrides(self):
        overrides = getattr(self.node, "_js_style_overrides", None)
        if overrides is None:
            overrides = {}
            self.node._js_style_overrides = overrides
        return overrides

    def js_get(self, name):
        if name == "getPropertyValue":
            return self._get_property_value
        if name == "setProperty":
            return self._set_property
        kebab = _camel_to_kebab(name)
        return self._overrides().get(kebab, self.node.style.get(kebab, ""))

    def js_set(self, name, value):
        self._write(_camel_to_kebab(name), value)

    def _write(self, name, value):
        self._overrides()[name] = str(value)
        self.node.style[name] = str(value)
        self._flag["dirty"] = True

    def _get_property_value(self, name):
        name = str(name)
        return self._overrides().get(name, self.node.style.get(name, ""))

    def _set_property(self, name, value):
        self._write(str(name), value)
        return UNDEFINED
