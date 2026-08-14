"""The FeetBrowser GUI and the load pipeline that ties every stage together.

Pipeline per navigation:
    URL.request -> HTMLParser -> collect stylesheets -> CSSParser + cascade
    -> DocumentLayout -> display list -> paint on a Tk canvas.

Chrome (tabs, address bar, back/forward, scrollbar) is drawn by hand on a
second canvas so the whole browser really is "from scratch".
"""

import os
import re
import sys
import json
import html
import threading
import time
import tkinter
import urllib.parse
import uuid
from collections import deque

from .net import URL
from .htmlparser import HTMLParser, Text, Element
from .cssparser import CSSParser, style, parse_inline, set_viewport, \
    media_matches, get_viewport
from .layout import DocumentLayout, paint_tree, get_font, DrawText, _measure
from .jsdom import JSDocument
from .jsengine import Interpreter, JSException, UNDEFINED
from . import toes as toes

WIDTH, HEIGHT = 1000, 720
SCROLL_STEP = 80
CHROME_HEIGHT = 80  # tabs + address bar
LOG_HEIGHT = 16  # slim strip under the toolbar reporting load errors
BOOKMARKS_FILE = os.path.expanduser("~/.feetbrowser_bookmarks.json")
MAX_CACHED_IMAGES = 300
# Cap the number of concurrent image fetches across the whole browser.
# Without a bound, a photo-heavy page spawns hundreds of threads and sockets
# at once; a small pool keeps memory and file-descriptor use flat while
# still fetching far faster than the layout can paint.
MAX_CONCURRENT_IMAGE_FETCHES = 6
_image_fetch_sem = threading.Semaphore(MAX_CONCURRENT_IMAGE_FETCHES)

# Deeply nested documents walk DOM/layout trees recursively; give Python a
# comfortable margin so pathological pages degrade gracefully instead of
# crashing with RecursionError.
sys.setrecursionlimit(20000)

HERE = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(HERE, "ua.css")) as f:
    DEFAULT_STYLE_SHEET = CSSParser(f.read()).parse()


def tree_to_list(tree, out):
    stack = [tree]
    while stack:
        node = stack.pop()
        out.append(node)
        for child in reversed(node.children):
            stack.append(child)
    return out


def find_links(node, out):
    """Collect <link rel=stylesheet href=...> hrefs."""
    stack = [node]
    while stack:
        n = stack.pop()
        if isinstance(n, Element) and n.tag == "link" \
                and n.attributes.get("rel", "").lower() == "stylesheet" \
                and "href" in n.attributes:
            out.append(n.attributes["href"])
        for child in reversed(n.children):
            stack.append(child)
    return out


def inline_styles(node, out):
    stack = [node]
    while stack:
        n = stack.pop()
        if isinstance(n, Element) and n.tag == "style":
            out.append("".join(c.text for c in n.children if isinstance(c, Text)))
        for child in reversed(n.children):
            stack.append(child)
    return out


def find_base_href(node):
    """Return the href of the document's first <base> element, if any."""
    stack = [node]
    while stack:
        n = stack.pop()
        if isinstance(n, Element) and n.tag == "base" and "href" in n.attributes:
            return n.attributes["href"]
        for child in n.children:
            stack.append(child)
    return None


def get_title(node):
    stack = [node]
    while stack:
        n = stack.pop()
        if isinstance(n, Element) and n.tag == "title":
            text = "".join(c.text for c in n.children
                           if isinstance(c, Text)).strip()
            if text:
                return text
        for child in reversed(n.children):
            stack.append(child)
    return None


# @import url("..."); / @import "..." [media]; — matched at a statement
# boundary so a bare "@import" mention inside a rule can't be grabbed.
_IMPORT_RE = re.compile(
    r"(?P<lead>(?:^|[\s{};]))@import\s+"
    r"(?:url\(\s*)?(?P<url>[^'\";\s()]+|'[^']*'|\"[^\"]*\")"
    r"(?:\s*\))?\s*(?P<media>[^;]*);",
    re.IGNORECASE)


def _expand_imports(css, base_url, depth=0, seen=None, log=None):
    """Inline `@import`ed stylesheets so pages that ship their CSS via
    @import don't come out unstyled.

    Imported sheets are fetched relative to `base_url`, nested imports are
    expanded (bounded depth), and the import's own media query is honored
    against the current viewport. A `seen` set guards against cycles.
    """
    if depth > 4 or "@import" not in css:
        return css
    if seen is None:
        seen = set()
    out = []
    last = 0
    width, height = get_viewport()
    for m in _IMPORT_RE.finditer(css):
        out.append(css[last:m.start()])
        out.append(m.group("lead"))
        last = m.end()
        url = (m.group("url") or "").strip().strip("\"'")
        media = (m.group("media") or "").strip()
        if not url or url in seen:
            continue
        if media and not media_matches(media, width, height):
            continue
        seen.add(url)
        imp_url = None
        try:
            imp_url = base_url.resolve(url)
            _h, imported, _c = imp_url.request()
        except Exception as e:  # noqa: BLE001 - a broken import shouldn't stop the page
            if log is not None:
                log(f"CSS {imp_url or url} ({type(e).__name__})")
            continue
        out.append(_expand_imports(imported, imp_url, depth + 1, seen, log))
    out.append(css[last:])
    return "".join(out)


class FormAction:
    """Returned by Tab.click() when a <form> is submitted: load url+payload."""

    __slots__ = ("url", "payload")

    def __init__(self, url, payload):
        self.url = url
        self.payload = payload


class Tab:
    """One document: its DOM, layout, scroll position and history."""

    def __init__(self, tab_height, browser=None):
        self.history = []
        self.future = []
        self.url = None
        self.scroll = 0
        self.tab_height = tab_height
        self.browser = browser
        self.display_list = []
        self.document = None
        self.nodes = None
        self.title = "New Tab"
        self.status = ""
        self.base_url = None
        self.focused_input = None
        self.form_values = {}
        # Absolute URL -> decoded tkinter.PhotoImage, shared with the layout
        # so <img> elements render their actual pixels.
        self.image_cache = {}
        self._image_queue = []
        self._image_results = deque()
        self._image_root = None
        self._image_done = None
        # Console output accumulated from JS (errors + console.log lines).
        self.js_logs = []
        # Network/load failures worth surfacing in the browser's log strip
        # (CSS/script/image fetches that failed or were dropped).
        self.net_errors = []
        # Image URLs that failed to download, filled by background threads
        # and drained into net_errors on the UI thread.
        self._image_failures = deque()
        # How much of the interpreter's append-only log has already been
        # scanned for JS errors, so _capture_js_errors never double-counts.
        self._js_log_cursor = 0
        # Stylesheet rules for the current document, kept so JS-driven DOM
        # mutations can be re-styled, and the live interpreter reused across
        # script runs and click-handler dispatch.
        self._last_rules = None
        self._js_interp = None
        self._js_doc = None
        # Background-thread results for JS `fetch()`/`XMLHttpRequest`,
        # drained on the UI thread by `_drain_js`.
        self._js_fetch_results = deque()
        self._js_xhr_results = deque()
        # Async page loading (GUI mode): a generation counter discards stale
        # fetches if the user navigates again mid-load, and the result queue
        # hands bytes from the fetch thread back to the UI thread.
        self.loading = False
        self._load_gen = 0
        self._load_queue = deque()
        self._load_meta = None
        # Page text selection: (ax, ay, ex, ey) in document coordinates, or
        # None when nothing is selected. Used by drag-selection + Ctrl+C.
        self.selection = None

    # -- navigation ------------------------------------------------------

    def load(self, url, payload=None, push=True, refresh=False,
             pending_scroll=0):
        if isinstance(url, str):
            base = self.url
            url = base.resolve(url) if (base and "://" not in url
                                        and not url.startswith(("data:", "file:",
                                                                "view-source:"))) \
                else URL(url)
        self.status = f"Loading {url}..."
        if url.view_source:
            self.status = "Loading source..."
        self.focused_input = None
        self.form_values = {}
        self.selection = None

        # Toe-handled (internal) URLs are cheap and stay synchronous. The
        # built-in ToeHub handles toehub:// and framework toe:// pages before
        # any installed toe gets a say.
        handled = None
        if self.browser and isinstance(url, URL):
            from . import toehub
            handled = toehub.handle(url, self)
            if handled is None:
                handled = toes.first(self.browser.toe_contexts, "handle",
                                     url, self)
        if handled is not None:
            _headers, body, ctype = handled
            self._complete_load(url, payload, push, pending_scroll, body, ctype)
            return
        # In the GUI, fetch http(s) off the UI thread so the loading spinner
        # can animate while the network is slow.
        if self._gui_mode() and isinstance(url, URL) \
                and url.scheme in ("http", "https"):
            self._start_async_load(url, payload, push, refresh, pending_scroll)
            return
        try:
            _headers, body, ctype = url.request(payload=payload,
                                                refresh=refresh)
            doc_error = None
        except TypeError:
            # Internal URL objects (about:blank, bookmarks, history) expose a
            # simpler request(); retry without the refresh flag.
            _headers, body, ctype = url.request(payload=payload)
            doc_error = None
        except Exception as e:  # noqa: BLE001 - surface any network error in-page
            body = f"<h1>Could not load page</h1><pre>{type(e).__name__}: {e}</pre>"
            doc_error = f"DOC {url} ({type(e).__name__})"
            ctype = "text/html"
        self._complete_load(url, payload, push, pending_scroll, body, ctype,
                            doc_error=doc_error)

    def _gui_mode(self):
        return self.browser is not None \
            and getattr(self.browser, "window", None) is not None

    def _start_async_load(self, url, payload, push, refresh, pending_scroll):
        """Fetch the page body on a background thread; the UI thread applies
        it in `_poll_async` so Tk/DOM work never leaves the main loop."""
        self.loading = True
        self._load_gen += 1
        gen = self._load_gen
        self._load_meta = {"gen": gen, "url": url, "payload": payload,
                           "push": push, "pending_scroll": pending_scroll}

        def worker():
            try:
                _headers, body, ctype = url.request(payload=payload,
                                                    refresh=refresh)
                exc = None
            except Exception as e:  # noqa: BLE001 - surfaced as an error page
                body, ctype, exc = None, None, e
            self._load_queue.append((gen, body, ctype, exc))

        threading.Thread(target=worker, daemon=True).start()
        # Self-schedule the drain on the UI thread so the load completes even
        # for tabs not owned by the main window (e.g. popups).
        self.browser.window.after(60, self._poll_async)

    def _poll_async(self):
        """UI thread: apply a finished background fetch, keep polling while
        the load is still in flight."""
        self._drain_async_load()
        if self.loading and self.browser is not None \
                and getattr(self.browser, "window", None) is not None:
            self.browser.window.after(60, self._poll_async)

    def _drain_async_load(self):
        """UI thread: apply a finished background fetch, if it's still the
        current load. Stale results (a newer navigation started meanwhile)
        are discarded."""
        if not self._load_queue:
            return
        gen, body, ctype, exc = self._load_queue.popleft()
        meta = self._load_meta
        if meta is None or gen != meta["gen"]:
            return  # stale load from before the latest navigation
        self._load_meta = None
        if exc is not None:
            body = (f"<h1>Could not load page</h1>"
                    f"<pre>{type(exc).__name__}: {exc}</pre>")
            doc_error = f"DOC {meta['url']} ({type(exc).__name__})"
            ctype = "text/html"
        else:
            doc_error = None
        self._complete_load(meta["url"], meta["payload"], meta["push"],
                            meta["pending_scroll"], body, ctype,
                            doc_error=doc_error)

    def _complete_load(self, url, payload, push, pending_scroll, body, ctype,
                       doc_error=None):
        """Shared tail of load(): apply a fetched body to the tab."""
        if push and self.url is not None:
            self.history.append((self.url, self.scroll))
            self.future.clear()
        self.url = url
        self.scroll = pending_scroll or 0

        if url.view_source or ctype.startswith("text/plain"):
            escaped = (body.replace("&", "&amp;")
                       .replace("<", "&lt;").replace(">", "&gt;"))
            body = f"<pre>{escaped}</pre>"
            ctype = "text/html"

        try:
            self._build(url, body, ctype)
        except Exception as e:  # noqa: BLE001 - never leave the tab half-rendered
            err = (f"<h1>Rendering error</h1>"
                   f"<pre>{type(e).__name__}: {e}</pre>")
            self.title = "Error"
            self._build(url, err, "text/html")

        if doc_error is not None:
            self._add_error(doc_error)

        self.loading = False
        self.status = str(url)
        if getattr(url, "fragment", ""):
            self.scroll_to_fragment(url.fragment)
        self._clamp_scroll()
        # With the DOM ready, start image loading and repaint.
        if self._gui_mode():
            self.load_images(self.browser.window, done=self.browser.draw)

    def _build(self, url, body, ctype="text/html"):
        """Parse, collect stylesheets, cascade, and lay out `body`."""
        # Fresh document: drop any previous form focus/values and JS state.
        self.focused_input = None
        self.form_values = {}
        self.js_logs = []
        self.net_errors = []
        self._image_failures = deque()
        self._js_log_cursor = 0
        self._js_interp = None
        self._js_doc = None
        self._last_js_render = 0.0
        self._js_fetch_results.clear()
        self._js_xhr_results.clear()

        if ctype.startswith("image/"):
            # We can't decode images yet; render a labelled placeholder instead
            # of trying to parse binary data as HTML.
            body = (f"<h1>Image</h1><p>[img: {ctype}]</p>"
                    f"<p><code>{body[:80]}</code></p>")

        if self.browser:
            body = toes.rewrite(self.browser.toe_contexts, url, body)

        self.nodes = HTMLParser(body).parse()
        self.title = get_title(self.nodes) or str(url)

        # <base href> (if any) overrides where relative URLs resolve from.
        base_href = find_base_href(self.nodes)
        self.base_url = url.resolve(base_href) if base_href else url
        resolve_from = self.base_url

        # Gather stylesheets: UA + toe-injected + <style> + <link rel=stylesheet>.
        rules = list(DEFAULT_STYLE_SHEET)
        if self.browser:
            injected = toes.extra_css(self.browser.toe_contexts, url)
            if injected:
                try:
                    rules.extend(CSSParser(injected).parse())
                except Exception:  # noqa: BLE001 - a broken sheet shouldn't stop the page
                    pass
        for sheet in inline_styles(self.nodes, []):
            try:
                sheet = _expand_imports(sheet, resolve_from, log=self._add_error)
                rules.extend(CSSParser(sheet).parse())
            except Exception:  # noqa: BLE001 - a broken sheet shouldn't stop the page
                pass
        for href in find_links(self.nodes, []):
            sheet_url = None
            try:
                sheet_url = resolve_from.resolve(href)
                _h, css_body, _c = sheet_url.request()
                css_body = _expand_imports(css_body, sheet_url,
                                           log=self._add_error)
                rules.extend(CSSParser(css_body).parse())
            except Exception as e:  # noqa: BLE001 - skip stylesheets that fail
                self._add_error(
                    f"CSS {sheet_url or href} ({type(e).__name__})")
                continue

        style(self.nodes, rules)
        # Keep the rules around so JS mutations can re-style the tree.
        self._last_rules = rules

        # Resolve <img src> to absolute URLs now so the layout's cache lookup
        # keys (absolute) always match what load_images() fetches.
        for node in tree_to_list(self.nodes, []):
            if isinstance(node, Element) and node.tag == "img" \
                    and node.attributes.get("src"):
                try:
                    node.attributes["src"] = str(
                        resolve_from.resolve(node.attributes["src"]))
                except Exception:  # noqa: BLE001 - bad src renders placeholder
                    pass
        self.render()
        self._run_scripts()

    def render(self):
        self.selection = None
        self.document = DocumentLayout(self.nodes, WIDTH)
        self.document.image_cache = self.image_cache
        self.document.layout()
        self.document.input_boxes = self.document.collect_inputs([])
        self.display_list = []
        paint_tree(self.document, self.display_list)

    # -- scripting ------------------------------------------------------

    def _run_scripts(self):
        """Execute every <script> (inline or external) against a fresh
        interpreter bridged to the document, then restyle and re-render."""
        scripts = [el for el in tree_to_list(self.nodes, [])
                   if isinstance(el, Element) and el.tag == "script"]
        if not scripts:
            return
        self._js_interp = Interpreter()
        doc = JSDocument(self.nodes, base_url=self.base_url,
                         mark_dirty=self._js_mutated,
                         interp=self._js_interp)
        self._js_doc = doc
        self._js_interp.globals["document"] = doc
        self._js_interp.globals["window"] = doc
        # Browser-provided host APIs (network + nothing Tk).
        self._js_interp.globals["fetch"] = self._js_fetch
        self._js_interp.globals["XMLHttpRequest"] = self._js_xhr_ctor()
        self._register_js_host(self._js_interp, doc)
        for el in scripts:
            try:
                code = None
                src = el.attributes.get("src")
                if src:
                    sheet_url = None
                    try:
                        sheet_url = self.base_url.resolve(src) \
                            if self.base_url else URL(src)
                        _h, code, _c = sheet_url.request()
                    except Exception as e:  # noqa: BLE001 - skip bad/unreachable src
                        self._add_error(
                            f"JS {sheet_url or src} ({type(e).__name__})")
                        code = None
                else:
                    code = "".join(ch.text for ch in el.children
                                   if isinstance(ch, Text))
                if code:
                    self._js_interp.run(code)
            except JSException as e:
                self._js_interp.logs.append(f"JS error: {e}")
        self.js_logs.extend(self._js_interp.logs)
        self._capture_js_errors(self._js_interp.logs)
        # Run microtasks/timers the scripts scheduled (promise .then chains,
        # setTimeout(0), ...) before deciding whether anything changed.
        self._drain_js()
        # Only re-render when a script actually mutated the DOM. Most pages'
        # scripts run read-only (feature detection, counters) and forcing a
        # full restyle+layout for them dominates page-load time.
        if doc._flag["dirty"]:
            self._js_mutated()

    def _add_error(self, msg):
        self.net_errors.append(msg)
        if len(self.net_errors) > 500:
            del self.net_errors[:len(self.net_errors) - 500]

    def _capture_js_errors(self, logs):
        """Append any not-yet-scanned JS error lines to net_errors."""
        start = self._js_log_cursor
        self._js_log_cursor = len(logs)
        for line in logs[start:]:
            if line.startswith("JS error"):
                msg = line[len("JS error: "):]
                self._add_error(f"JS {msg}")

    def _js_mutated(self):
        """Re-style the tree with the stored rules and re-render after a
        script or click handler finished mutating the DOM."""
        if self.nodes is None:
            return
        # style() reassigns node.style and recomputes inheritance, so JS-driven
        # overrides must be folded into the inline style attribute first (this
        # also keeps them winning over author rules on restyle).
        for node in tree_to_list(self.nodes, []):
            overrides = getattr(node, "_js_style_overrides", None)
            if not overrides:
                continue
            merged = dict(parse_inline(node.attributes.get("style", "")))
            merged.update(overrides)
            node.attributes["style"] = "; ".join(
                f"{k}: {v}" for k, v in merged.items())
        if self._last_rules is not None:
            style(self.nodes, self._last_rules)
        fresh_title = get_title(self.nodes)
        if fresh_title:
            self.title = fresh_title
        self.render()

    # -- JS host APIs (fetch, XMLHttpRequest) ------------------------------

    def _js_fetch(self, url, options=UNDEFINED):
        """Host `fetch()`: resolve relative to the document, fetch on a
        background thread, and settle the returned Promise on the UI thread."""
        interp = self._js_interp
        promise = interp.create_promise()
        try:
            target = self.base_url.resolve(str(url)) if self.base_url \
                else URL(str(url))
        except Exception as e:  # noqa: BLE001 - malformed URL
            promise.reject(str(e))
            return promise

        def worker():
            try:
                headers, body, ctype = target.request()
                err = None
                status = 200
            except Exception as e:  # noqa: BLE001 - network failure
                headers, body, ctype, status, err = {}, "", "text/plain", 0, str(e)
            self._js_fetch_results.append((promise, headers, body, ctype,
                                           status, err))

        threading.Thread(target=worker, daemon=True).start()
        return promise

    def _js_xhr_ctor(self):
        return _JSXHRCtor(self)

    def _register_js_host(self, interp, doc):
        """Register browser-environment globals (window/document companions)
        so real-world pages can poke at them without throwing."""
        url = self.base_url or self.url
        url_str = str(url) if url else ""
        interp.globals["performance"] = {
            "now": lambda: time.time() * 1000,
            "timing": {
                "navigationStart": time.time() * 1000,
                "domContentLoadedEventEnd": 0,
                "loadEventEnd": 0,
            },
            "navigation": {"type": 0, "redirectCount": 0},
            "mark": lambda *a: None,
            "measure": lambda *a: None,
            "getEntriesByName": lambda *a: [],
            "getEntriesByType": lambda *a: [],
            "timeOrigin": time.time() * 1000,
        }
        interp.globals["navigator"] = {
            "userAgent": "FeetBrowser/0.1.1",
            "platform": "Linux x86_64",
            "language": "en-US",
            "languages": ["en-US", "en"],
            "vendor": "FeetBrowser",
            "appName": "Netscape",
            "appVersion": "5.0 (X11; Linux x86_64) FeetBrowser/0.1.1",
            "product": "Gecko",
            "onLine": True,
            "cookieEnabled": True,
            "hardwareConcurrency": 4,
            "maxTouchPoints": 0,
            "webdriver": False,
            "connection": {"effectiveType": "4g", "downlink": 10,
                           "rtt": 50},
            "sendBeacon": lambda *a: True,
            "clipboard": {"writeText": lambda *a: None},
            "permissions": {"query": lambda *a: {"state": "denied"}},
        }
        interp.globals["location"] = self._js_location(url_str)
        interp.globals["history"] = {
            "length": 0,
            "state": None,
            "back": lambda: None,
            "forward": lambda: None,
            "go": lambda *a: None,
            "pushState": lambda *a: None,
            "replaceState": lambda *a: None,
        }
        interp.globals["screen"] = {
            "width": 1000, "height": 720,
            "availWidth": 1000, "availHeight": 720,
            "colorDepth": 24, "pixelDepth": 24,
        }
        interp.globals["innerWidth"] = 1000
        interp.globals["innerHeight"] = 720
        interp.globals["outerWidth"] = 1000
        interp.globals["outerHeight"] = 720
        interp.globals["devicePixelRatio"] = 1
        interp.globals["pageXOffset"] = 0
        interp.globals["pageYOffset"] = 0
        interp.globals["scrollX"] = 0
        interp.globals["scrollY"] = 0
        interp.globals["matchMedia"] = lambda query: _js_match_media(str(query))
        interp.globals["getComputedStyle"] = \
            lambda el, pseudo=None: _js_computed_style(el)
        interp.globals["requestAnimationFrame"] = \
            lambda fn: interp._native_set_timeout(fn, 0)
        interp.globals["cancelAnimationFrame"] = interp._native_clear_timer
        interp.globals["requestIdleCallback"] = \
            lambda fn: interp._native_set_timeout(fn, 0)
        interp.globals["cancelIdleCallback"] = interp._native_clear_timer
        interp.globals["Event"] = _JSEventCtor()
        interp.globals["CustomEvent"] = _JSEventCtor()
        interp.globals["MouseEvent"] = _JSEventCtor()
        interp.globals["KeyboardEvent"] = _JSEventCtor()
        interp.globals["alert"] = lambda *a: UNDEFINED
        interp.globals["confirm"] = lambda *a: False
        interp.globals["prompt"] = lambda *a: UNDEFINED
        interp.globals["open"] = lambda *a: None
        interp.globals["close"] = lambda: None
        interp.globals["print"] = lambda: None
        interp.globals["scrollTo"] = lambda *a: None
        interp.globals["scrollBy"] = lambda *a: None
        interp.globals["focus"] = lambda: None
        interp.globals["blur"] = lambda: None
        interp.globals["addEventListener"] = doc._add_event_listener
        interp.globals["removeEventListener"] = doc._remove_event_listener
        interp.globals["dispatchEvent"] = doc._dispatch_event
        interp.globals["globalThis"] = doc
        interp.globals["parent"] = doc
        interp.globals["top"] = doc
        interp.globals["self"] = doc
        interp.globals["frames"] = doc
        interp.globals["origin"] = "null"
        interp.globals["caches"] = {
            "open": lambda *a: _js_fresh_promise(interp),
            "match": lambda *a: _js_fresh_promise(interp),
        }
        interp.globals["localStorage"] = _js_storage()
        interp.globals["sessionStorage"] = _js_storage()
        interp.globals["indexedDB"] = {
            "open": lambda *a: _js_fresh_promise(interp),
        }
        interp.globals["crypto"] = {
            "getRandomValues": lambda arr: _js_random_values(arr),
            "randomUUID": lambda: str(uuid.uuid4()),
        }
        interp.globals["queueMicrotask"] = interp._native_queue_microtask

    def _js_location(self, url_str):
        import urllib.parse as _up
        try:
            parts = _up.urlsplit(url_str)
            origin = f"{parts.scheme}://{parts.netloc}"
        except Exception:
            parts = None
            origin = ""
        if parts is None or not parts.scheme:
            return {"href": url_str, "hostname": "", "protocol": "",
                    "pathname": "", "search": "", "hash": "",
                    "host": "", "origin": "", "port": "",
                    "reload": lambda: None, "assign": lambda u=None: None,
                    "replace": lambda u=None: None,
                    "toString": lambda: url_str}
        return {
            "href": url_str,
            "hostname": parts.hostname or "",
            "protocol": parts.scheme + ":",
            "pathname": parts.path,
            "search": "?" + parts.query if parts.query else "",
            "hash": "",
            "host": parts.netloc,
            "origin": origin,
            "port": str(parts.port or ""),
            "reload": lambda: None,
            "assign": lambda u=None: None,
            "replace": lambda u=None: None,
            "toString": lambda: url_str,
        }

    def _drain_js(self):
        """UI thread: settle JS network results and run pending microtasks /
        due timers, re-rendering if any handler mutated the DOM."""
        interp = self._js_interp
        if interp is None:
            return
        while self._js_fetch_results:
            promise, headers, body, ctype, status, err = \
                self._js_fetch_results.popleft()
            if err:
                promise.reject(err)
            else:
                promise.resolve(JSResponse(interp, headers, body, ctype,
                                           status))
        while self._js_xhr_results:
            xhr, headers, body, ctype, status, err = self._js_xhr_results.popleft()
            xhr._finish(headers, body, status, err)
        try:
            interp.drain()
        except JSException as e:
            self.js_logs.append(f"JS error: {e}")
            self._add_error(f"JS {e}")
        if self._js_doc is not None and self._js_doc._flag["dirty"]:
            # Rate-limit restyles+relayouts so a page whose JS mutates the
            # DOM every timer tick (animation loops) can't saturate the UI
            # thread re-rendering continuously.
            if time.time() - self._last_js_render > 0.1:
                self._last_js_render = time.time()
                self._js_mutated()

    def _dispatch_js_click(self, node):
        """Run click handlers (addEventListener + onclick attrs) registered on
        `node` or any ancestor. Returns True if any handler attempted to run."""
        interp = self._js_interp
        if interp is None:
            return False
        handled = False
        cur = node
        while cur is not None:
            if isinstance(cur, Element):
                handlers = getattr(cur, "_js_handlers", None)
                if handlers:
                    for fn in handlers.get("click", []):
                        try:
                            interp.call(fn)
                        except JSException as e:
                            interp.logs.append(f"JS error: {e}")
                        handled = True
                onclick = cur.attributes.get("onclick")
                if onclick:
                    try:
                        interp.run(onclick)
                    except JSException as e:
                        interp.logs.append(f"JS error: {e}")
                    handled = True
            cur = cur.parent
        if handled:
            self.js_logs.extend(interp.logs)
            self._capture_js_errors(interp.logs)
            self._drain_js()
            self._js_mutated()
            return True
        return False

    # -- images ----------------------------------------------------------

    def load_images(self, root=None, done=None):
        """Collect <img> sources missing from the cache and fetch them
        asynchronously (off the UI thread), re-rendering as each arrives."""
        self._image_root = root
        self._image_done = done
        self._image_queue = []
        # Background threads stash raw bytes here; the UI thread drains the
        # deque on a timer so Tk (Photos, canvas) is only ever touched on the
        # main thread. deque append/popleft are atomic under the GIL.
        self._image_results = deque()
        seen = set()
        if self.nodes is None:
            return
        for node in tree_to_list(self.nodes, []):
            if not (isinstance(node, Element) and node.tag == "img"):
                continue
            src = node.attributes.get("src")
            if not src:
                continue
            try:
                url = self.base_url.resolve(src) if self.base_url else URL(src)
            except Exception:  # noqa: BLE001 - bad src shouldn't kill the page
                continue
            key = str(url)
            if key in self.image_cache or key in seen:
                continue
            seen.add(key)
            self._image_queue.append((key, url))
        if not self._image_queue:
            if done:
                done()
            return
        if root is None:
            # No UI loop (tests / headless): fetch and decode synchronously so
            # results are available immediately and deterministically.
            while self._image_queue:
                key, url = self._image_queue[0]
                try:
                    _headers, data, ctype = url.request_bytes()
                except Exception:  # noqa: BLE001 - keep placeholder on failure
                    data, ctype = None, None
                self._decode_and_finish(key, data, ctype)
            return
        for key, url in self._image_queue:
            threading.Thread(
                target=self._fetch_image, args=(key, url), daemon=True).start()

    def _fetch_image(self, key, url):
        """Background thread: fetch bytes, hand them back to the UI thread via
        the results queue. Never touches Tk directly. The semaphore bounds
        how many image fetches run at once browser-wide."""
        try:
            with _image_fetch_sem:
                _headers, data, ctype = url.request_bytes()
        except Exception as e:  # noqa: BLE001 - failed image fetch: keep placeholder
            data, ctype = None, None
            self._image_failures.append(f"{url} ({type(e).__name__})")
        self._image_results.append((key, data, ctype))

    def _drain_images(self):
        """Called on the UI thread: decode any finished downloads and
        re-render when the last one arrives."""
        while self._image_failures:
            url = self._image_failures.popleft()
            self._add_error(f"IMG {url}")
        if not self._image_results:
            return
        pending = []
        try:
            while True:
                pending.append(self._image_results.popleft())
        except IndexError:
            pass
        for key, data, ctype in pending:
            self._decode_and_finish(key, data, ctype)

    def _decode_and_finish(self, key, data, ctype):
        if data:
            photo = self._decode_image(data, ctype)
            if photo is not None:
                self.image_cache[key] = photo
                # Bound the per-tab image cache so long browsing sessions
                # cannot grow it (and the X/PhotoImage resources behind it)
                # without limit. Dict preserves insertion order: drop oldest.
                while len(self.image_cache) > MAX_CACHED_IMAGES:
                    self.image_cache.pop(next(iter(self.image_cache)))
        # Remove this URL (not necessarily the head) — background threads
        # finish in arbitrary order, so popping the head would reorder the
        # remaining queue and skip images.
        self._image_queue = [q for q in self._image_queue if q[0] != key]
        if self._image_queue:
            return  # still waiting on the remaining threads
        self.render()
        if self._image_done:
            self._image_done()

    @staticmethod
    def _decode_image(data, ctype):
        """Decode image bytes to a Tk PhotoImage. PNG/GIF/PNM are handled
        natively by Tk; JPEG/WebP/BMP/ICO/TIFF are converted to PNG through
        Pillow when it is installed; SVG is rasterized via cairosvg when
        available (or handed to Tk on Tk 8.7+, which can rasterize it)."""
        ctype = (ctype or "").split(";")[0].strip().lower()
        # Formats Tk decodes natively.
        if ctype in ("image/png", "image/gif", "image/x-xbitmap"):
            try:
                return tkinter.PhotoImage(data=data)
            except Exception:  # noqa: BLE001 - bad bytes; try Pillow below
                pass
        # Formats Pillow can convert to PNG (otherwise fall through to Tk
        # sniffing, which may still decode).
        if ctype in ("image/jpeg", "image/jpg", "image/webp", "image/bmp",
                     "image/x-icon", "image/vnd.microsoft.icon", "image/tiff"):
            photo = Tab._photo_from_pillow(data)
            if photo is not None:
                return photo
        if ctype == "image/svg+xml":
            photo = Tab._photo_from_svg(data)
            if photo is not None:
                return photo
        # Unknown type: let Tk sniff the data (it may still decode).
        try:
            return tkinter.PhotoImage(data=data)
        except Exception:  # noqa: BLE001 - undecodable data -> placeholder
            return None

    @staticmethod
    def _photo_from_pillow(data):
        """Convert image bytes to a Tk PhotoImage via Pillow. Returns None
        if Pillow is missing or the data is undecodable."""
        try:
            from PIL import Image as PILImage
            import io
            pil = PILImage.open(io.BytesIO(data))
            pil.load()
            pil = pil.convert("RGBA")
            buf = io.BytesIO()
            pil.save(buf, format="PNG")
            return tkinter.PhotoImage(data=buf.getvalue())
        except Exception:  # noqa: BLE001 - Pillow missing / bad data
            return None

    @staticmethod
    def _photo_from_svg(data):
        """Rasterize SVG bytes to a Tk PhotoImage via cairosvg (optional)."""
        try:
            import cairosvg
            png = cairosvg.svg2png(bytestring=data)
            return tkinter.PhotoImage(data=png)
        except Exception:  # noqa: BLE001 - cairosvg missing / bad data
            return None

    def content_height(self):
        return self.document.height if self.document else 0

    def go_back(self):
        if not self.history:
            return
        self.future.append((self.url, self.scroll))
        url, scroll = self.history.pop()
        self.load(url, push=False, pending_scroll=scroll)

    def go_forward(self):
        if not self.future:
            return
        self.history.append((self.url, self.scroll))
        url, scroll = self.future.pop()
        self.load(url, push=False, pending_scroll=scroll)

    # -- interaction -----------------------------------------------------

    def scroll_by(self, delta):
        self.scroll += delta
        self._clamp_scroll()

    def _clamp_scroll(self):
        max_y = max(0, self.content_height() - self.tab_height)
        self.scroll = max(0, min(self.scroll, max_y))

    def scroll_to_fragment(self, frag):
        for node in tree_to_list(self.nodes, []):
            if isinstance(node, Element) and \
                    (node.attributes.get("id") == frag or
                     (node.tag == "a" and node.attributes.get("name") == frag)):
                box = self._find_box(self.document, node)
                if box:
                    self.scroll = max(0, box.y - 20)
                    self._clamp_scroll()
                return

    def _find_box(self, box, node):
        if getattr(box, "node", None) is node:
            return box
        for child in box.children:
            found = self._find_box(child, node)
            if found:
                return found
        return None

    def _node_at(self, x, y):
        """Return the DOM node under (x, y), first checking form controls."""
        y += self.scroll
        if self.document:
            for lx, ty, rx, by, node in self.document.input_boxes:
                if lx <= x < rx and ty <= y < by:
                    return node
        # The display list is in paint order, so the topmost command under a
        # point is the *last* match. Scanning in reverse returns the same node
        # as the old full scan but exits on the first hit, which matters on
        # text-heavy pages where this runs on every mouse-move for hover.
        for cmd in reversed(self.display_list):
            if getattr(cmd, "node", None) is not None and hasattr(cmd, "hit") \
                    and cmd.hit(x, y):
                return cmd.node
        return None

    @staticmethod
    def _enclosing_link(node):
        while node:
            if isinstance(node, Element) and node.tag == "a" \
                    and "href" in node.attributes:
                return node.attributes["href"]
            node = node.parent
        return None

    def click(self, x, y):
        """Handle a click at document coords.

        Returns a URL to load, a FormAction (form submit), or None.
        """
        node = self._node_at(x, y)
        control = self._hit_control(node)
        if control is not None:
            result = self._activate_control(control)
        else:
            href = self._enclosing_link(node)
            if not href:
                result = None
            elif href.startswith(("javascript:", "mailto:", "tel:")):
                self.status = href
                result = None
            else:
                result = self.base_url.resolve(href) if self.base_url \
                    else self.url.resolve(href)
        # A JS click handler (if any) consumes the click and cancels navigation.
        if node is not None and self._dispatch_js_click(node):
            return None
        return result

    def link_at(self, x, y):
        """Return href under the cursor for hover feedback, else None."""
        return self._enclosing_link(self._node_at(x, y))

    # -- text selection --------------------------------------------------

    def _text_char_at(self, x, y):
        """Return the DrawText command and char index under (x, y), or
        (None, None) if no text is under the point."""
        for cmd in self.display_list:
            if isinstance(cmd, DrawText) and cmd.text and cmd.hit(x, y):
                return cmd, self._char_at_x(cmd, x)
        return None, None

    @staticmethod
    def _char_at_x(cmd, x):
        """Index of the character whose left edge is nearest to x within a
        DrawText command."""
        i = 0
        while i < len(cmd.text) and \
                cmd.left + _measure(cmd.font, cmd.text[:i + 1]) <= x:
            i += 1
        return i

    def start_selection(self, x, y):
        """Begin (or reset) a selection anchored at document coords (x, y)."""
        cmd, i = self._text_char_at(x, y)
        if cmd:
            x = cmd.left + _measure(cmd.font, cmd.text[:i])
        self.selection = (x, y, x, y)

    def extend_selection(self, x, y):
        """Extend the selection to document coords (x, y)."""
        if self.selection is None:
            self.start_selection(x, y)
            return
        cmd, i = self._text_char_at(x, y)
        if cmd:
            x = cmd.left + _measure(cmd.font, cmd.text[:i])
        self.selection = (self.selection[0], self.selection[1], x, y)

    def _selection_spans(self):
        """Selected character ranges as (cmd, start_char, end_char) tuples,
        in document order, or [] when nothing is selected."""
        if self.selection is None:
            return []
        ax, ay, ex, ey = self.selection
        if ax == ex and ay == ey:
            return []
        spans = []
        for cmd in self.display_list:
            if not isinstance(cmd, DrawText) or not cmd.text:
                continue
            if cmd.bottom <= min(ay, ey) or cmd.top > max(ay, ey):
                continue
            s, e = 0, len(cmd.text)
            on_anchor = cmd.top <= ay < cmd.bottom
            on_end = cmd.top <= ey < cmd.bottom
            forward = (ey, ex) >= (ay, ax)
            if on_anchor:
                if forward:
                    s = max(s, self._char_at_x(cmd, ax))
                else:
                    e = min(e, self._char_at_x(cmd, ax))
            if on_end:
                if forward:
                    e = min(e, self._char_at_x(cmd, ex))
                else:
                    s = max(s, self._char_at_x(cmd, ex))
            if s < e:
                spans.append((cmd, s, e))
        return spans

    def selected_text(self):
        """The selected text, line-by-line, for clipboard copying."""
        lines, cur, last_top = [], [], None
        for cmd, s, e in self._selection_spans():
            if last_top is not None and cmd.top != last_top:
                lines.append(" ".join(cur))
                cur = []
            cur.append(cmd.text[s:e])
            last_top = cmd.top
        if cur:
            lines.append(" ".join(cur))
        return "\n".join(lines)

    # -- forms -----------------------------------------------------------

    @staticmethod
    def _hit_control(node):
        while node is not None:
            if isinstance(node, Element) and \
                    node.tag in ("input", "button", "textarea", "select"):
                return node
            node = node.parent
        return None

    @staticmethod
    def _enclosing_form(node):
        while node is not None:
            if isinstance(node, Element) and node.tag == "form":
                return node
            node = node.parent
        return None

    def _activate_control(self, control):
        if control.tag == "input" and control.attributes.get("type", "").lower() \
                in ("checkbox", "radio"):
            current = control.attributes.get("value", "on")
            control.attributes["value"] = "off" if current == "on" else "on"
            self.render()
            return None
        if control.tag == "input" and control.attributes.get("type", "").lower() == "reset":
            form = self._enclosing_form(control)
            if form:
                self.reset_form(form)
            return None
        is_submit = (control.tag == "button"
                     or control.attributes.get("type", "").lower()
                     in ("submit", "image"))
        if is_submit:
            form = self._enclosing_form(control)
            if form:
                return self._submit_form(form)
            return None
        # Focusable field.
        if self.focused_input is not None:
            self.focused_input.attributes.pop("data-focused", None)
        self.focused_input = control
        control.attributes["data-focused"] = ""
        self.render()
        return None

    def reset_form(self, form):
        for node in tree_to_list(form, []):
            if not isinstance(node, Element):
                continue
            if node.tag == "input":
                itype = node.attributes.get("type", "text").lower()
                if itype in ("checkbox", "radio"):
                    node.attributes["value"] = "off"
                    if "checked" in node.attributes:
                        node.attributes["value"] = "on"
                elif itype not in ("submit", "button", "reset", "image"):
                    node.attributes["value"] = ""
            elif node.tag == "textarea":
                node.attributes["value"] = ""
        self.render()

    def blur_input(self):
        if self.focused_input is not None:
            self.focused_input.attributes.pop("data-focused", None)
            self.focused_input = None
            self.render()

    def type_char(self, ch):
        inp = self.focused_input
        if inp is None:
            return False
        value = inp.attributes.get("value", "")
        if inp.tag == "textarea":
            inp.attributes["value"] = value + ch
        else:
            itype = inp.attributes.get("type", "text").lower()
            if itype in ("checkbox", "radio"):
                return False
            inp.attributes["value"] = value + ch
        self.render()
        return True

    def delete_char(self):
        inp = self.focused_input
        if inp is None:
            return False
        value = inp.attributes.get("value", "")
        inp.attributes["value"] = value[:-1]
        self.render()
        return True

    def submit_focused(self):
        inp = self.focused_input
        if inp is None:
            return None
        form = self._enclosing_form(inp)
        if form:
            return self._submit_form(form)
        return None

    def _submit_form(self, form):
        method = form.attributes.get("method", "get").lower()
        action = form.attributes.get("action", "")
        base = (self.base_url if self.base_url else self.url)
        if action:
            url = base.resolve(action)
        else:
            url = URL(str(self.url).split("#", 1)[0])

        params = []
        for node in tree_to_list(form, []):
            if not isinstance(node, Element):
                continue
            name = node.attributes.get("name")
            if not name:
                continue
            if node.tag == "input":
                itype = node.attributes.get("type", "text").lower()
                if itype in ("submit", "button", "reset", "image"):
                    continue
                if itype in ("checkbox", "radio") and \
                        node.attributes.get("value") != "on":
                    continue
                params.append((name, node.attributes.get("value", "")))
            elif node.tag == "textarea":
                params.append((name, node.attributes.get("value", "")))
            elif node.tag == "select":
                opts = [c for c in node.children
                        if isinstance(c, Element) and c.tag == "option"]
                chosen = [o for o in opts if "selected" in o.attributes] or opts[:1]
                value = chosen[0].attributes.get("value", "") if chosen else ""
                params.append((name, value))

        query = urllib.parse.urlencode(params)
        if method == "post":
            plain = str(url).split("#", 1)[0]
            return FormAction(URL(plain), query)
        # GET: merge the query into any query the action already carries.
        plain = str(url).split("#", 1)[0]
        new_url = URL(plain)
        base_path, _, existing = new_url.path.partition("?")
        parts = [p for p in (existing, query) if p]
        new_url.path = base_path + ("?" + "&".join(parts) if parts else "")
        return FormAction(new_url, None)

    def draw(self, canvas, offset):
        for cmd in self.display_list:
            if cmd.top > self.scroll + self.tab_height:
                continue
            if cmd.bottom < self.scroll:
                continue
            cmd.execute(self.scroll - offset, canvas)
        self._draw_selection(canvas, offset)

    def _draw_selection(self, canvas, offset):
        """Paint the text-selection highlight (blue fill + white text) over
        whatever was already drawn."""
        for cmd, s, e in self._selection_spans():
            x1 = cmd.left + _measure(cmd.font, cmd.text[:s])
            x2 = cmd.left + _measure(cmd.font, cmd.text[:e])
            y1, y2 = cmd.top, cmd.bottom
            if y2 < self.scroll or y1 > self.scroll + self.tab_height:
                continue
            try:
                canvas.create_rectangle(
                    x1, y1 - self.scroll + offset,
                    x2, y2 - self.scroll + offset,
                    fill="#1a73e8", width=0)
                canvas.create_text(
                    x1, y1 - self.scroll + offset, text=cmd.text[s:e],
                    font=cmd.font, fill="white", anchor="nw")
            except tkinter.TclError:
                pass


class ContextMenu:
    """A hand-drawn context menu painted on the browser canvas.

    Stays true to the "chrome is drawn by hand" design: no native Tk menu
    widgets, just rectangles and text, so it looks and behaves like the rest
    of the UI. Items are None (a separator) or (label, callback, enabled).

    It renders on top of everything in Browser.draw() and tracks its own
    hover state; the browser feeds it mouse/keyboard events while open.
    """

    ITEM_H = 26
    PAD = 4
    PAD_X = 10
    SEP = 8

    def __init__(self):
        self.items = []
        self.x = self.y = 0
        self.width = self.height = 0
        self.hover = -1
        self.open_ = False

    def open(self, x, y, items, canvas_w, canvas_h):
        self.items = items
        self.hover = -1
        font = get_font(12, "normal", "roman", "Helvetica")
        width = 170
        for item in items:
            if item is not None:
                width = max(width, _measure(font, item[0])
                            + 2 * self.PAD_X + 8)
        height = self.PAD
        for item in items:
            height += self.SEP if item is None else self.ITEM_H
        height += self.PAD
        self.width = max(120, min(width, canvas_w - 4))
        self.height = height
        self.x = max(2, min(x, canvas_w - self.width - 2))
        self.y = max(2, min(y, canvas_h - self.height - 2))
        self.open_ = True

    def close(self):
        self.open_ = False
        self.items = []
        self.hover = -1

    def point_in_menu(self, x, y):
        return (self.open_ and self.x <= x <= self.x + self.width
                and self.y <= y <= self.y + self.height)

    def hit(self, x, y):
        """Index of the item under (x, y), or -1 (separators never hit)."""
        if not self.point_in_menu(x, y):
            return -1
        y0 = self.y + self.PAD
        for i, item in enumerate(self.items):
            if item is None:
                y0 += self.SEP
                continue
            if y0 <= y < y0 + self.ITEM_H:
                return i
            y0 += self.ITEM_H
        return -1

    def set_hover(self, x, y):
        idx = self.hit(x, y)
        changed = idx != self.hover
        self.hover = idx
        return changed

    def _enabled_indices(self):
        return [i for i, item in enumerate(self.items)
                if item is not None and item[2]]

    def move(self, delta):
        """Move keyboard focus to the next/previous enabled item."""
        enabled = self._enabled_indices()
        if not enabled:
            return
        if self.hover in enabled:
            pos = enabled.index(self.hover)
        else:
            pos = -1 if delta > 0 else 0
        self.hover = enabled[(pos + delta) % len(enabled)]

    def activate(self):
        """Return the callback of the hovered enabled item, else None."""
        if 0 <= self.hover < len(self.items):
            item = self.items[self.hover]
            if item is not None and item[2]:
                return item[1]
        return None

    def draw(self, canvas):
        if not self.open_:
            return
        c = canvas
        x, y = self.x, self.y
        c.create_rectangle(x - 1, y - 1, x + self.width + 1,
                           y + self.height + 1, fill="#d0d0d0", width=0)
        c.create_rectangle(x, y, x + self.width, y + self.height,
                           fill="white", outline="#666666", width=1)
        y0 = y + self.PAD
        for i, item in enumerate(self.items):
            if item is None:
                y0 += self.SEP / 2
                c.create_line(x + 8, y0, x + self.width - 8, y0,
                              fill="#dddddd", width=1)
                y0 += self.SEP / 2
                continue
            label, _callback, enabled = item
            if i == self.hover and enabled:
                c.create_rectangle(x + 1, y0, x + self.width - 1,
                                   y0 + self.ITEM_H, fill="#1a73e8", width=0)
                color = "white"
            else:
                color = "#111111" if enabled else "#aaaaaa"
            c.create_text(x + self.PAD_X, y0 + self.ITEM_H / 2, text=label,
                          anchor="w", font=get_font(12, "normal", "roman",
                                                    "Helvetica"), fill=color)
            y0 += self.ITEM_H


class Browser:
    def __init__(self):
        self.tabs = []
        self.active_tab = None
        self.focus = None  # "address" or None
        self.address_text = ""
        self.bookmarks = self._load_bookmarks()
        self.address_caret = 0
        self.address_sel = None  # (start, end) while selecting, else None
        self.address_view = 0  # horizontal scroll offset in px
        self._drag_moved = False  # a press+move (vs. a plain click) happened
        self._resize_after = None
        self._last_size = (WIDTH, HEIGHT)
        # Chrome-style loading spinner: current arc start angle (degrees).
        self._loading_angle = 0

        # Toes: one Context per loaded toe, all optional hooks.
        self.toes = toes.discover_toes()
        self.toe_contexts = [toes.Context(self, toe.module) for toe in self.toes]
        self.toe_handlers = {}
        for ctx in self.toe_contexts:
            for btn in (ctx.call("buttons") or []):
                self.toe_handlers[btn.id] = ctx

        self.window = tkinter.Tk()
        self.window.title("FeetBrowser")
        self.window.geometry(f"{WIDTH}x{HEIGHT}")
        self.window.minsize(480, 320)
        self.canvas = tkinter.Canvas(
            self.window, width=WIDTH, height=HEIGHT,
            bg="white", highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)

        self.chrome_font = get_font(14, "normal", "roman", "Helvetica")
        self.bold_font = get_font(14, "bold", "roman", "Helvetica")

        self.context_menu = ContextMenu()

        self._bind()

    def _bind(self):
        w = self.window
        w.bind("<Down>", self._on_down)
        w.bind("<Up>", self._on_up)
        w.bind("<Next>", self._on_page_down)   # PageDown
        w.bind("<Prior>", self._on_page_up)    # PageUp
        w.bind("<Left>", self._on_left)
        w.bind("<Right>", self._on_right)
        w.bind("<Home>", self._on_home)
        w.bind("<End>", self._on_end)
        w.bind("<Control-Home>", self._on_home_key)
        w.bind("<Control-End>", self._on_end_key)
        w.bind("<MouseWheel>", self._on_wheel)
        w.bind("<Button-4>", lambda e: self._scroll(-SCROLL_STEP))
        w.bind("<Button-5>", lambda e: self._scroll(SCROLL_STEP))
        w.bind("<Button-1>", self._on_click)
        w.bind("<B1-Motion>", self._on_drag)
        w.bind("<ButtonRelease-1>", self._on_release)
        w.bind("<Button-2>", self._on_middle_click)
        w.bind("<Button-3>", self._on_context_menu)
        w.bind("<Motion>", self._on_motion)
        w.bind("<Key>", self._on_key)
        w.bind("<Return>", self._on_enter)
        w.bind("<BackSpace>", self._on_backspace)
        w.bind("<Delete>", self._on_delete)
        w.bind("<Escape>", self._on_escape)
        w.bind("<Configure>", self._on_resize)
        w.bind("<Control-l>", lambda e: self._focus_address())
        w.bind("<Control-t>", lambda e: self.new_tab("about:blank",
                                                     focus_address=True))
        w.bind("<Control-w>", lambda e: self.close_tab())
        w.bind("<Control-r>", lambda e: self._reload())
        w.bind("<Control-d>", lambda e: self._toggle_bookmark())
        w.bind("<Control-h>", lambda e: self._open_history_page())
        w.bind("<Control-Tab>", lambda e: self._cycle_tab(1))
        w.bind("<Control-ISO_Left_Tab>", lambda e: self._cycle_tab(-1))
        w.bind("<Control-Prior>", lambda e: self._next_tab(-1))
        w.bind("<Control-Next>", lambda e: self._next_tab(1))
        w.bind("<Alt-Left>", lambda e: self._back())
        w.bind("<Alt-Right>", lambda e: self._forward())

    # -- tab management --------------------------------------------------

    def chrome_bands(self):
        """Chrome bands declared by toes, as [(id, height, y), ...]."""
        return toes.compute_bands(self.toe_contexts)

    def reload_toes(self):
        """Re-discover installed toes and rebuild their contexts live.

        Called by the ToeHub after an install/uninstall so changes take
        effect without restarting the browser.
        """
        self.toes = toes.discover_toes()
        self.toe_contexts = [toes.Context(self, toe.module)
                             for toe in self.toes]
        self.toe_handlers = {}
        for ctx in self.toe_contexts:
            for btn in (ctx.call("buttons") or []):
                self.toe_handlers[btn.id] = ctx
        self.draw()

    def chrome_height(self):
        """Total chrome height: the fixed chrome, the log strip, and any toe
        bands."""
        return CHROME_HEIGHT + LOG_HEIGHT + toes.band_height(self.chrome_bands())

    def tab_height(self):
        h = self.canvas.winfo_height()
        if h <= 1:  # window not mapped yet
            h = HEIGHT
        return max(50, h - self.chrome_height())

    def new_tab(self, url, focus_address=False):
        tab = Tab(self.tab_height(), self)
        page = self._coerce_url(url)
        if isinstance(page, _AboutURL):
            tab.load(page)  # routes welcome page through the full pipeline
            tab.status = "Type a URL and press Enter"
        else:
            tab.load(page)
        self.tabs.append(tab)
        self.active_tab = tab
        toes.dispatch(self.toe_contexts, "on_new_tab")
        self.draw()
        if focus_address:
            self._focus_address()

    def close_tab(self):
        if not self.active_tab:
            return
        idx = self.tabs.index(self.active_tab)
        self.tabs.remove(self.active_tab)
        if not self.tabs:
            self.window.destroy()
            return
        self.active_tab = self.tabs[min(idx, len(self.tabs) - 1)]
        self.draw()

    # -- event handlers --------------------------------------------------

    def _on_resize(self, e):
        # <Configure> fires continuously during a drag and also on window
        # moves. Only react to real size changes, and debounce the (possibly
        # expensive) re-layout until the drag settles.
        if e.widget is not self.window:
            return
        size = (self.canvas.winfo_width(), self.canvas.winfo_height())
        if size == self._last_size or size[0] <= 1:
            return
        self._last_size = size
        if self._resize_after is not None:
            self.window.after_cancel(self._resize_after)
        self._resize_after = self.window.after(100, self._apply_resize)

    def _apply_resize(self):
        self._resize_after = None
        global WIDTH, HEIGHT
        WIDTH = self.canvas.winfo_width()
        HEIGHT = self.canvas.winfo_height()
        set_viewport(WIDTH, HEIGHT)
        for tab in self.tabs:
            tab.tab_height = self.tab_height()
            if tab.nodes:
                tab.render()
                tab._clamp_scroll()
        self.draw()

    def _on_down(self, e):
        if self.focus == "address":
            return
        self._scroll(SCROLL_STEP)

    def _on_up(self, e):
        if self.focus == "address":
            return
        self._scroll(-SCROLL_STEP)

    def _on_left(self, e):
        if self.focus == "address":
            self._address_move_caret(-1, extend=bool(e.state & 0x1))
            self.draw()

    def _on_right(self, e):
        if self.focus == "address":
            self._address_move_caret(1, extend=bool(e.state & 0x1))
            self.draw()

    def _on_page_down(self, e):
        if self.focus == "address":
            return
        self._scroll(max(1, self.tab_height() - 120))
        return "break"

    def _on_page_up(self, e):
        if self.focus == "address":
            return
        self._scroll(-max(1, self.tab_height() - 120))
        return "break"

    def _on_home(self, e):
        if self.focus == "address" or not self.active_tab:
            return
        self.active_tab.scroll = 0
        self.draw()
        return "break"

    def _on_end(self, e):
        if self.focus == "address" or not self.active_tab:
            return
        self.active_tab.scroll = self.active_tab.content_height()
        self.active_tab._clamp_scroll()
        self.draw()
        return "break"

    def _on_wheel(self, e):
        self._scroll(-e.delta if abs(e.delta) < 30 else -int(e.delta / 30) * SCROLL_STEP)

    def _scroll(self, delta):
        if self.active_tab:
            self.active_tab.scroll_by(delta)
            self.draw()

    def _on_home_key(self, e):
        if self.focus == "address":
            self.address_caret = 0
            self.address_sel = None
            self._address_ensure_visible()
            self.draw()
            return
        if self.active_tab:
            self.active_tab.scroll = 0
            self.draw()

    def _on_end_key(self, e):
        if self.focus == "address":
            self.address_caret = len(self.address_text)
            self.address_sel = None
            self._address_ensure_visible()
            self.draw()
            return
        if self.active_tab:
            self.active_tab.scroll_by(10 ** 9)
            self.draw()

    def _on_click(self, e):
        if self.context_menu.open_:
            self._context_menu_click(e.x, e.y)
            return
        was_address = self.focus == "address"
        self.focus = None
        self._drag_moved = False
        if e.y < self.chrome_height():
            self._chrome_click(e.x, e.y, was_address)
            return
        if not self.active_tab:
            return
        ctrl = bool(getattr(e, "state", 0) & 0x4)
        dest = self.active_tab.click(e.x, e.y - self.chrome_height())
        if isinstance(dest, FormAction):
            self.active_tab.selection = None
            self._navigate(self.active_tab, dest.url, payload=dest.payload)
        elif dest and ctrl:
            self.active_tab.selection = None
            self.new_tab(str(dest))
        elif dest:
            self.active_tab.selection = None
            self._navigate(self.active_tab, dest)
        else:
            # Clicking plain text (or blank space) anchors a selection; it is
            # cleared again by a plain click in `_on_release`.
            node = self.active_tab._node_at(e.x, e.y - self.chrome_height())
            if not self.active_tab._hit_control(node):
                self.active_tab.start_selection(e.x, e.y - self.chrome_height())
        self.draw()

    def _on_middle_click(self, e):
        if self.context_menu.open_:
            self.context_menu.close()
            self.draw()
            return
        band_h = toes.band_height(self.chrome_bands())
        if e.y < band_h + 40:
            # Tab bar: middle-click a tab to close it, empty space (or the
            # "+" zone) to open a fresh one.
            if e.x < 34:
                self.new_tab("about:blank", focus_address=True)
                self.draw()
                return
            for i, tab in enumerate(self.tabs):
                x0 = 40 + i * 160
                if x0 <= e.x < x0 + 158:
                    self.active_tab = tab
                    self.close_tab()
                    return
            self.new_tab("about:blank", focus_address=True)
            self.draw()
            return
        if not self.active_tab or e.y < self.chrome_height():
            return
        dest = self.active_tab.click(e.x, e.y - self.chrome_height())
        if isinstance(dest, FormAction):
            self._navigate(self.active_tab, dest.url, payload=dest.payload)
        elif dest:
            self.new_tab(str(dest))

    def _on_release(self, e):
        if self.focus == "address":
            return
        tab = self.active_tab
        if not tab or tab.selection is None:
            return
        if not self._drag_moved:
            # A plain click (press + release, no drag) clears the selection.
            tab.selection = None
        self._drag_moved = False
        self.draw()

    def _on_drag(self, e):
        if self.focus == "address" and e.x >= self._address_bar_x() - 10:
            self._drag_moved = True
            if self.address_sel is None:
                self.address_sel = (self.address_caret, self.address_caret)
            anchor = self.address_sel[0]
            self.address_caret = self._caret_from_x(e.x)
            self.address_sel = (anchor, self.address_caret)
            self._address_ensure_visible()
            self.draw()
            return
        # Dragging on the page extends the text selection.
        if self.active_tab and e.y >= self.chrome_height():
            self._drag_moved = True
            self.active_tab.extend_selection(e.x, e.y - self.chrome_height())
            self.draw()

    def _chrome_click(self, x, y, was_address=False):
        # Toe chrome bands (above the tabs).
        bands = self.chrome_bands()
        band_h = toes.band_height(bands)
        if band_h and y < band_h:
            if toes.dispatch(self.toe_contexts, "on_chrome_click",
                             x, y, bands):
                return
        # Tab bar (top 40px).
        if y < band_h + 40:
            # New-tab button.
            if x < 34:
                self.new_tab("about:blank", focus_address=True)
                return
            for i, tab in enumerate(self.tabs):
                x0 = 40 + i * 160
                if x0 <= x < x0 + 158:
                    # close box
                    if x >= x0 + 158 - 20:
                        self.active_tab = tab
                        self.close_tab()
                        return
                    self.active_tab = tab
                    self.draw()
                    return
            return
        # Toolbar (40..80).
        if 8 <= x < 34 and band_h + 48 <= y < band_h + 72:
            self._back()
            return
        if 40 <= x < 66 and band_h + 48 <= y < band_h + 72:
            self._forward()
            return
        if 72 <= x < 98 and band_h + 48 <= y < band_h + 72:
            self._reload()
            return
        if 104 <= x < 130 and band_h + 48 <= y < band_h + 72:
            self._home()
            return
        # Toe toolbar buttons.
        bx = 136
        for btn in self._toe_buttons():
            if bx <= x < bx + 26 and band_h + 48 <= y < band_h + 72:
                ctx = self.toe_handlers.get(btn.id)
                if ctx:
                    ctx.call("on_click", btn.id)
                self.draw()
                return
            bx += 30
        # Bookmark star (after toe buttons).
        star_x = 136 + self._toe_buttons_offset()
        if star_x <= x < star_x + 26 and band_h + 48 <= y < band_h + 72:
            self._toggle_bookmark()
            return
        # Address bar.
        if x >= 136 + self._toe_buttons_offset() + 30:
            self.focus = "address"
            if not was_address:
                self._address_reset_from_tab()
                self._address_select_all()
            else:
                self._set_address_caret_from_x(x)
                self.address_sel = None
            self._address_ensure_visible()
            self.draw()

    def _on_motion(self, e):
        if self.context_menu.open_:
            if self.context_menu.set_hover(e.x, e.y):
                # Redraw just the menu, not the whole page, on hover moves.
                self.context_menu.draw(self.canvas)
            return
        if not self.active_tab:
            return
        if e.y >= self.chrome_height():
            doc_x, doc_y = e.x, e.y - self.chrome_height()
            toes.dispatch(self.toe_contexts, "on_motion", doc_x, doc_y)
            href = self.active_tab.link_at(doc_x, doc_y)
            self.canvas.config(cursor="hand2" if href else "")
            new_status = href or str(self.active_tab.url or "")
            if new_status != self.active_tab.status:
                self.active_tab.status = new_status
                self._draw_status()
        else:
            self.canvas.config(cursor="")

    # -- context menu ----------------------------------------------------

    def _on_context_menu(self, e):
        items = self._context_items(e.x, e.y)
        self.context_menu.open(e.x, e.y, items,
                               self.canvas.winfo_width(),
                               self.canvas.winfo_height())
        self.draw()

    def _context_menu_click(self, x, y):
        menu = self.context_menu
        if not menu.point_in_menu(x, y):
            menu.close()
            self.draw()
            return
        idx = menu.hit(x, y)
        if idx < 0:
            menu.close()
            self.draw()
            return
        menu.hover = idx
        cb = menu.activate()
        menu.close()
        self.draw()
        if cb:
            cb()

    @staticmethod
    def _enclosing_image(node):
        while node is not None:
            if isinstance(node, Element) and node.tag == "img" \
                    and node.attributes.get("src"):
                return node.attributes["src"]
            node = node.parent
        return None

    def _copy_text(self, text):
        try:
            self.window.clipboard_clear()
            self.window.clipboard_append(text)
        except tkinter.TclError:
            pass

    def _view_source(self):
        tab = self.active_tab
        if tab and isinstance(tab.url, URL):
            self._navigate(tab, URL("view-source:" + str(tab.url)))

    def _context_items(self, x, y):
        """Build the context-menu entries for a right-click at (x, y)."""
        tab = self.active_tab
        if not tab or y < self.chrome_height():
            return [
                ("Back", self._back, bool(tab and tab.history)),
                ("Forward", self._forward, bool(tab and tab.future)),
                ("Reload", self._reload, bool(tab)),
                None,
                ("New Tab", lambda: self.new_tab("about:blank"), True),
                ("Close Tab", self.close_tab, len(self.tabs) > 1),
                None,
                ("Home", self._home, bool(tab)),
                ("Bookmark This Page", self._toggle_bookmark,
                 bool(tab and self._bookmark_key(tab.url))),
                ("View Source", self._view_source,
                 bool(tab and isinstance(tab.url, URL))),
                ("History", self._open_history_page, bool(tab)),
            ]
        doc_y = y - self.chrome_height()
        node = tab._node_at(x, doc_y)
        href = tab._enclosing_link(node)
        img_src = self._enclosing_image(node)
        items = []
        if href:
            try:
                resolved = tab.base_url.resolve(href) if tab.base_url \
                    else tab.url.resolve(href)
            except Exception:  # noqa: BLE001 - malformed href: skip link actions
                resolved = None
            if resolved is not None:
                items.append(("Open Link",
                              lambda r=resolved: self._navigate(tab, r), True))
                items.append(("Open Link in New Tab",
                              lambda r=resolved: self.new_tab(str(r)), True))
            items.append(("Copy Link Address",
                          lambda h=href: self._copy_text(h), True))
            items.append(None)
        if img_src:
            try:
                img_url = tab.base_url.resolve(img_src) if tab.base_url \
                    else URL(img_src)
            except Exception:  # noqa: BLE001 - malformed src: skip image actions
                img_url = None
            if img_url is not None:
                items.append(("Open Image",
                              lambda u=img_url: self._navigate(tab, u), True))
                items.append(("Copy Image URL",
                              lambda u=str(img_url): self._copy_text(u), True))
            items.append(None)
        items.extend([
            ("Back", self._back, bool(tab.history)),
            ("Forward", self._forward, bool(tab.future)),
            ("Reload", self._reload, True),
            None,
            ("Bookmark This Page", self._toggle_bookmark,
             bool(self._bookmark_key(tab.url))),
            ("View Source", self._view_source, isinstance(tab.url, URL)),
            ("Copy Page URL",
             lambda u=str(tab.url): self._copy_text(u),
             bool(tab.url and not isinstance(tab.url, _AboutURL))),
            None,
            ("New Tab", lambda: self.new_tab("about:blank"), True),
            ("Close Tab", self.close_tab, len(self.tabs) > 1),
        ])
        return items

    def _on_key(self, e):
        if self.context_menu.open_:
            keysym = getattr(e, "keysym", "")
            if keysym == "Up":
                self.context_menu.move(-1)
                self.context_menu.draw(self.canvas)
            elif keysym == "Down":
                self.context_menu.move(1)
                self.context_menu.draw(self.canvas)
            elif keysym in ("Return", "KP_Enter"):
                cb = self.context_menu.activate()
                self.context_menu.close()
                self.draw()
                if cb:
                    cb()
            return
        if self.focus == "address":
            self._address_key(e)
            return
        ctrl = bool(getattr(e, "state", 0) & 0x4)
        if ctrl and getattr(e, "keysym", "").lower() == "c":
            self._copy_selection()
            return
        # Toes get first crack at keys when no address bar has focus, but
        # only consume the key when a toe explicitly returns True (a False
        # return means "not handled").
        if any(r is True for r in toes.dispatch(
                self.toe_contexts, "on_keypress", e)):
            return
        # Typing into a focused form field.
        if self.active_tab and self.active_tab.focused_input and \
                len(e.char) == 1 and e.char.isprintable():
            self.active_tab.type_char(e.char)
            self.draw()

    def _on_backspace(self, e):
        if self.focus == "address":
            self._address_backspace()
            self.draw()
            return
        if self.active_tab and self.active_tab.delete_char():
            self.draw()

    def _on_delete(self, e):
        if self.focus == "address":
            self._address_forward_delete()
            self.draw()

    def _address_key(self, e):
        ctrl = bool(getattr(e, "state", 0) & 0x4)
        if ctrl:
            k = getattr(e, "keysym", "").lower()
            if k == "a":
                self._address_select_all()
                self.draw()
                return
            if k == "c":
                self._address_copy()
                return
            if k == "x":
                self._address_cut()
                self.draw()
                return
            if k == "v":
                self._address_paste()
                self.draw()
                return
            if k == "u":
                self.address_text = ""
                self.address_caret = 0
                self.address_sel = None
                self._address_ensure_visible()
                self.draw()
                return
        if len(e.char) == 1 and ord(e.char) >= 32 and e.char.isprintable():
            self._address_insert(e.char)
            self.draw()

    def _copy_selection(self):
        """Copy the active tab's selected text to the system clipboard."""
        if not self.active_tab:
            return
        text = self.active_tab.selected_text()
        if not text:
            return
        self.window.clipboard_clear()
        self.window.clipboard_append(text)

    def _address_bar_x(self):
        """Canvas x where the address-bar text starts (after toe buttons
        and the bookmark star)."""
        return 136 + self._toe_buttons_offset() + 30 + 10

    def _address_reset_from_tab(self):
        url = str(self.active_tab.url) if \
            (self.active_tab and self.active_tab.url and
             not isinstance(self.active_tab.url, _AboutURL)) else ""
        self.address_text = url
        self.address_caret = len(url)
        self.address_sel = None
        self.address_view = 0

    def _caret_from_x(self, x):
        """Index of the address-bar caret under a canvas x coordinate."""
        font = self.chrome_font
        text = self.address_text
        rel = max(0.0, x - self._address_bar_x() + self.address_view)
        i = 0
        while i < len(text) and _measure(font, text[:i + 1]) <= rel:
            i += 1
        return i

    def _set_address_caret_from_x(self, x):
        self.address_caret = self._caret_from_x(x)

    def _address_selection(self):
        if self.address_sel is None:
            return None
        s, e = self.address_sel
        s = max(0, min(s, len(self.address_text)))
        e = max(0, min(e, len(self.address_text)))
        if s == e:
            return None
        return (s, e) if s < e else (e, s)

    def _address_delete_selection(self):
        sel = self._address_selection()
        if sel is None:
            return False
        s, e = sel
        self.address_text = self.address_text[:s] + self.address_text[e:]
        self.address_caret = s
        self.address_sel = None
        return True

    def _address_insert(self, text):
        self._address_delete_selection()
        self.address_text = (self.address_text[:self.address_caret] + text
                             + self.address_text[self.address_caret:])
        self.address_caret += len(text)
        self.address_sel = None
        self._address_ensure_visible()

    def _address_backspace(self):
        if self._address_delete_selection():
            return
        if self.address_caret > 0:
            self.address_text = (self.address_text[:self.address_caret - 1]
                                 + self.address_text[self.address_caret:])
            self.address_caret -= 1
            self._address_ensure_visible()

    def _address_forward_delete(self):
        if self._address_delete_selection():
            return
        if self.address_caret < len(self.address_text):
            self.address_text = (self.address_text[:self.address_caret]
                                 + self.address_text[self.address_caret + 1:])
            self._address_ensure_visible()

    def _address_select_all(self):
        self.address_caret = len(self.address_text)
        self.address_sel = (0, len(self.address_text))
        self._address_ensure_visible()

    def _address_move_caret(self, delta, extend=False):
        lo, hi = 0, len(self.address_text)
        if extend:
            if self.address_sel is None:
                self.address_sel = (self.address_caret, self.address_caret)
            anchor = self.address_sel[0]
            self.address_caret = max(lo, min(hi, self.address_caret + delta))
            self.address_sel = (anchor, self.address_caret)
        else:
            self.address_caret = max(lo, min(hi, self.address_caret + delta))
            self.address_sel = None
        self._address_ensure_visible()

    def _address_copy(self):
        sel = self._address_selection()
        if sel is None:
            return
        s, e = sel
        try:
            self.window.clipboard_clear()
            self.window.clipboard_append(self.address_text[s:e])
        except tkinter.TclError:
            pass

    def _address_cut(self):
        if self._address_selection() is None:
            return
        self._address_copy()
        self._address_delete_selection()

    def _address_paste(self):
        try:
            data = self.window.clipboard_get()
        except tkinter.TclError:
            return
        if data:
            self._address_insert(data)

    def _address_ensure_visible(self):
        """Horizontal scroll of the address text so the caret stays in view."""
        font = self.chrome_font
        caret_x = _measure(font, self.address_text[:self.address_caret])
        box_w = max(40, self.canvas.winfo_width() - 8 - self._address_bar_x() - 8)
        if caret_x < self.address_view:
            self.address_view = max(0, caret_x - 8)
        elif caret_x > self.address_view + box_w:
            self.address_view = caret_x - box_w + 8

    def _on_escape(self, e):
        if self.context_menu.open_:
            self.context_menu.close()
            self.draw()
        elif self.focus == "address":
            self.focus = None
            self.draw()
        elif self.active_tab and self.active_tab.focused_input:
            self.active_tab.blur_input()
            self.draw()

    def _on_enter(self, e):
        if self.focus == "address":
            if not self.address_text.strip():
                return
            self.focus = None
            query = self.address_text.strip()
            if query == "about:blank":
                dest = _AboutURL()
            else:
                if not self._looks_like_url(query):
                    query = "https://duckduckgo.com/html/?q=" + \
                        query.replace(" ", "+")
                elif "://" not in query and not query.startswith(
                        ("file:", "data:", "view-source:", "about:")):
                    query = "https://" + query
                dest = query
            if self.active_tab:
                self._navigate(self.active_tab, self._coerce_url(dest))
            return
        # Enter in a focused form field submits its form.
        if self.active_tab and self.active_tab.focused_input:
            action = self.active_tab.submit_focused()
            self.active_tab.blur_input()
            if action:
                self._navigate(self.active_tab, action.url,
                               payload=action.payload)

    @staticmethod
    def _looks_like_url(text):
        if " " in text.strip():
            return False
        if text.startswith(("http://", "https://", "file:", "data:",
                            "view-source:", "about:")):
            return True
        if text.startswith("."):
            return False
        if ":" in text:
            host, _, rest = text.partition(":")
            if rest.isdigit():
                return True  # hostname:port / IPv4:port / [v6]:port
            if "]" in text and text.startswith("["):
                return True
        return "." in text

    def _focus_address(self):
        self.focus = "address"
        if self.active_tab:
            self.active_tab.blur_input()
        self._address_reset_from_tab()
        self._address_select_all()
        self.draw()

    @staticmethod
    def _bookmark_key(url):
        if not url or isinstance(url, (_AboutURL, _BookmarksURL)):
            return None
        return str(url)

    def _is_bookmarked(self, url):
        key = self._bookmark_key(url)
        return bool(key and key in self.bookmarks)

    def _toggle_bookmark(self):
        if not self.active_tab:
            return
        key = self._bookmark_key(self.active_tab.url)
        if not key:
            self.active_tab.status = "This page can't be bookmarked"
            self.draw()
            return
        if key in self.bookmarks:
            self.bookmarks.remove(key)
            self.active_tab.status = "Bookmark removed"
        else:
            self.bookmarks.append(key)
            self.active_tab.status = "Bookmarked"
        self._save_bookmarks()
        self.draw()

    def _history_snapshot(self):
        tab = self.active_tab
        if not tab:
            return {"back": [], "current": "", "forward": []}
        return {
            "back": [str(url) for url, _scroll in tab.history],
            "current": str(tab.url) if tab.url else "",
            "forward": [str(url) for url, _scroll in reversed(tab.future)],
        }

    def _open_history_page(self):
        if self.active_tab:
            self.active_tab.load(self._coerce_url("about:history"))
            self.draw()

    def _cycle_tab(self, step):
        if not self.tabs or not self.active_tab:
            return "break"
        i = self.tabs.index(self.active_tab)
        self.active_tab = self.tabs[(i + step) % len(self.tabs)]
        self.draw()
        return "break"

    def _coerce_url(self, raw):
        if not isinstance(raw, str):
            return raw
        text = raw.strip().lower()
        if text in ("about:blank", "about:newtab"):
            return _AboutURL(lambda: list(self.bookmarks))
        if text == "about:bookmarks":
            return _BookmarksURL(lambda: list(self.bookmarks))
        if text == "about:history":
            return _HistoryURL(self._history_snapshot)
        return raw

    @staticmethod
    def _sanitize_bookmarks(values):
        if not isinstance(values, list):
            return []
        out = []
        seen = set()
        for item in values:
            if not isinstance(item, str):
                continue
            value = item.strip()
            if not value or value.startswith("about:"):
                continue
            if value in seen:
                continue
            seen.add(value)
            out.append(value)
        return out

    def _load_bookmarks(self):
        try:
            with open(BOOKMARKS_FILE, "r", encoding="utf8") as f:
                data = json.load(f)
            return self._sanitize_bookmarks(data)
        except (OSError, json.JSONDecodeError):
            return []

    def _save_bookmarks(self):
        try:
            with open(BOOKMARKS_FILE, "w", encoding="utf8") as f:
                json.dump(self.bookmarks, f, indent=2)
        except OSError:
            pass

    def _back(self):
        if self.active_tab:
            self.active_tab.go_back()
            self.draw()

    def _forward(self):
        if self.active_tab:
            self.active_tab.go_forward()
            self.draw()

    def _reload(self):
        # Pass the URL object (not its string) so internal pages like the
        # about:blank welcome page reload without being re-parsed as a URL.
        # `refresh=True` bypasses the response cache so the page actually
        # re-fetches.
        if self.active_tab and self.active_tab.url:
            self.active_tab.load(self.active_tab.url, push=False, refresh=True)
            self.draw()

    def _home(self):
        if self.active_tab:
            self.active_tab.load(_AboutURL())
            self.active_tab.status = "Type a URL and press Enter"
            self.draw()

    def _next_tab(self, direction):
        if not self.tabs:
            return
        idx = self.tabs.index(self.active_tab)
        self.active_tab = self.tabs[(idx + direction) % len(self.tabs)]
        self.draw()

    def _navigate(self, tab, url, payload=None):
        """Load `url` on `tab`; image fetching + repaint happen when the
        document is ready (see Tab._complete_load)."""
        tab.load(url, payload=payload)
        self.draw()

    # -- painting --------------------------------------------------------

    def draw(self):
        self.canvas.delete("all")
        chrome = self.chrome_height()
        if self.active_tab:
            self.active_tab.tab_height = self.tab_height()
            self.active_tab.draw(self.canvas, chrome)
        toes.dispatch(self.toe_contexts, "on_draw", self.canvas, chrome)
        # Chrome background covers page content that scrolled up under it.
        self.canvas.create_rectangle(0, 0, self.canvas.winfo_width(),
                                     chrome, fill="#e8e8e8", width=0)
        # Toe chrome bands paint on top of the chrome background.
        bands = self.chrome_bands()
        if bands:
            toes.dispatch(self.toe_contexts, "on_chrome_draw",
                          self.canvas, bands)
        self._draw_tabs()
        self._draw_toolbar()
        self._draw_log()
        self._draw_toe_buttons()
        self._draw_status()
        self._draw_scrollbar()
        self._draw_spinner()
        self.context_menu.draw(self.canvas)
        self.window.title(
            (self.active_tab.title if self.active_tab else "FeetBrowser")
            + " — FeetBrowser")

    def _draw_log(self):
        """Draw the load-error strip under the toolbar: the most recent
        network/JS failure (if any) for the active tab, plus a count."""
        c = self.canvas
        tab = self.active_tab
        top = toes.band_height(self.chrome_bands()) + CHROME_HEIGHT
        c.create_rectangle(0, top, c.winfo_width(), top + LOG_HEIGHT,
                           fill="#fff4e6", width=0)
        c.create_line(0, top, c.winfo_width(), top, fill="#e0cda8")
        if not tab or not tab.net_errors:
            return
        latest = tab.net_errors[-1]
        total = len(tab.net_errors)
        msg = f"[{total} load error{'s' if total != 1 else ''}] {latest}"
        width = max(0, c.winfo_width() - 16)
        font = get_font(10, "normal", "roman", "Helvetica")
        if _measure(font, msg) > width:
            while msg and _measure(font, msg + "\u2026") > width:
                msg = msg[:-1]
            msg += "\u2026"
        c.create_text(8, top + LOG_HEIGHT / 2, text=msg, anchor="w",
                      font=font, fill="#8a5a00")

    def _draw_tabs(self):
        c = self.canvas
        top = toes.band_height(self.chrome_bands())
        c.create_rectangle(0, top, c.winfo_width(), top + 40, fill="#d0d0d0",
                           width=0)
        # New-tab button.
        c.create_text(17, top + 20, text="+", font=self.bold_font, fill="#333")
        for i, tab in enumerate(self.tabs):
            x0 = 40 + i * 160
            active = tab is self.active_tab
            c.create_rectangle(x0, top + 4, x0 + 158, top + 40,
                               fill="white" if active else "#c4c4c4",
                               width=0)
            title = tab.title or "New Tab"
            # Tabs are 158px wide; fit the title in the space before the
            # close box (which starts at x0 + 148) so long page titles never
            # spill out past the tab edge.
            title_w = 128
            if _measure(self.chrome_font, title) > title_w:
                t = title
                while t and _measure(self.chrome_font, t + "…") > title_w:
                    t = t[:-1]
                title = t + "…"
            c.create_text(x0 + 10, top + 20, text=title, anchor="w",
                          font=self.chrome_font, fill="#222")
            c.create_text(x0 + 148, top + 20, text="×", font=self.bold_font,
                          fill="#666")

    def _draw_toolbar(self):
        c = self.canvas

        def btn(x, glyph, enabled):
            c.create_rectangle(x, top + 48, x + 26, top + 72, outline="#999",
                               fill="#f4f4f4", width=1)
            c.create_text(x + 13, top + 60, text=glyph,
                          fill="#333" if enabled else "#bbb",
                          font=self.bold_font)

        top = toes.band_height(self.chrome_bands())
        tab = self.active_tab
        btn(8, "‹", bool(tab and tab.history))
        btn(40, "›", bool(tab and tab.future))
        btn(72, "⟳", bool(tab))
        btn(104, "⌂", bool(tab))
        marked = bool(tab and self._is_bookmarked(tab.url))
        btn(136 + self._toe_buttons_offset(), "★" if marked else "☆",
            bool(tab))

        # Address bar (after the toe buttons and bookmark star).
        addr_x = 136 + self._toe_buttons_offset() + 30
        c.create_rectangle(addr_x, top + 48, c.winfo_width() - 8, top + 72,
                           outline="#3b82f6" if self.focus == "address" else "#999",
                           fill="white", width=2 if self.focus == "address" else 1)
        if self.focus == "address":
            self._draw_address_editor(c, addr_x, top)
        else:
            url = ""
            if tab and tab.url and not isinstance(tab.url, _AboutURL):
                url = str(tab.url)
            c.create_text(addr_x + 10, top + 60, text=url, anchor="w",
                          font=self.chrome_font, fill="#111")

    def _draw_address_editor(self, c, addr_x, top):
        """Paint the focused address bar: text (with horizontal scroll),
        selection highlight, and the caret."""
        font = self.chrome_font
        text = self.address_text
        x0 = addr_x + 10
        x1 = c.winfo_width() - 16
        if x1 - x0 < 30:
            x1 = x0 + 30
        sel = self._address_selection()
        view = self.address_view

        if not text:
            c.create_text(x0, top + 60, text="Type a URL or search term…",
                          anchor="w", font=font, fill="#aaa")
            c.create_line(x0, top + 52, x0, top + 68, fill="#111")
            return

        def char_x(i):
            return x0 + (_measure(font, text[:i]) - view)

        # Visible slice of the text.
        start = 0
        while start < len(text) and _measure(font, text[:start + 1]) <= view:
            start += 1
        end = start
        while end < len(text) and _measure(font, text[start:end + 1]) <= (x1 - x0):
            end += 1
        if self.address_caret < start:
            start = self.address_caret
        if self.address_caret > end:
            end = self.address_caret

        # Selection highlight.
        if sel is not None and sel[1] > start and sel[0] < end:
            c.create_rectangle(char_x(max(start, sel[0])), top + 51,
                               char_x(min(end, sel[1])), top + 69,
                               fill="#1a73e8", width=0)

        y = top + 60
        if sel is not None and sel[0] < end and sel[1] > start:
            s1, s2 = max(start, sel[0]), min(end, sel[1])
            part1, part2, part3 = text[start:s1], text[s1:s2], text[s2:end]
            if part1:
                c.create_text(char_x(start), y, text=part1, anchor="w",
                              font=font, fill="#111")
            if part2:
                c.create_text(char_x(s1), y, text=part2, anchor="w",
                              font=font, fill="white")
            if part3:
                c.create_text(char_x(s2), y, text=part3, anchor="w",
                              font=font, fill="#111")
        else:
            c.create_text(char_x(start), y, text=text[start:end], anchor="w",
                          font=font, fill="#111")

        # Caret.
        cx = char_x(self.address_caret)
        c.create_line(cx, top + 52, cx, top + 68, fill="#111")

    def _toe_buttons(self):
        return [btn for ctx in self.toe_contexts
                for btn in (ctx.call("buttons") or [])]

    def _toe_buttons_offset(self):
        return len(self._toe_buttons()) * 30

    def _draw_toe_buttons(self):
        c = self.canvas
        top = toes.band_height(self.chrome_bands())
        x = 136
        for btn in self._toe_buttons():
            c.create_rectangle(x, top + 48, x + 26, top + 72, outline="#999",
                               fill="#fdf6e3", width=1)
            c.create_text(x + 13, top + 60, text=btn.glyph[:2], fill="#333",
                          font=self.bold_font)
            x += 30

    def _draw_status(self):
        c = self.canvas
        h = c.winfo_height()
        c.create_rectangle(0, h - 22, c.winfo_width(), h,
                           fill="#efefef", width=0)
        c.create_line(0, h - 22, c.winfo_width(), h - 22, fill="#ccc")
        status = self.active_tab.status if self.active_tab else ""
        c.create_text(8, h - 11, text=status[:200], anchor="w",
                      font=get_font(11, "normal", "roman", "Helvetica"),
                      fill="#444")

    def _draw_scrollbar(self):
        tab = self.active_tab
        if not tab:
            return
        view = self.tab_height()
        total = tab.content_height()
        if total <= view:
            return
        c = self.canvas
        track_x = c.winfo_width() - 10
        track_top = self.chrome_height()
        track_h = view
        frac = view / total
        thumb_h = max(30, track_h * frac)
        thumb_top = track_top + (track_h - thumb_h) * (tab.scroll / (total - view))
        c.create_rectangle(track_x, thumb_top, track_x + 6, thumb_top + thumb_h,
                           fill="#9aa0a6", width=0)

    def run(self):
        self.window.update_idletasks()
        self.draw()
        # Redraw once the window is actually mapped and sized.
        self.window.after(120, self.draw)
        self._poll_images()
        self.window.mainloop()

    def _poll_images(self):
        """Periodic UI-thread sweep: pick up decoded image bytes left by the
        fetch threads, re-render, and spin the loading indicator while any
        tab is still fetching. The `after` chain lives for the whole session,
        which keeps the loop alive across navigations."""
        loading = False
        for tab in self.tabs:
            tab._drain_images()
            if tab._js_interp is not None:
                # Advance the JS virtual clock so setTimeout/setInterval fire
                # on schedule, then run microtasks/timers/fetch settlements.
                tab._js_interp.advance(60)
                tab._drain_js()
            if tab.loading:
                loading = True
        if loading:
            self._loading_angle = (self._loading_angle + 18) % 360
            self.canvas.delete("spinner")
            self._draw_spinner()
        self.window.after(60, self._poll_images)

    def _draw_spinner(self):
        """Chrome-style spinning arc at the left of the address bar."""
        tab = self.active_tab
        if not tab or not tab.loading:
            return
        c = self.canvas
        top = toes.band_height(self.chrome_bands())
        addr_x = 136 + self._toe_buttons_offset() + 30
        cx = addr_x + 16
        cy = top + 60
        c.create_arc(cx - 6, cy - 6, cx + 6, cy + 6,
                     start=self._loading_angle, extent=250,
                     style="arc", outline="#1a73e8", width=2,
                     tags=("spinner",))


class PopupWindow:
    """A real popup window (a separate Tk Toplevel), not a redirect.

    Each popup is a mini-browser: its own canvas, a hand-drawn title bar
    with a close button, a Tab rendering the URL through the full pipeline,
    wheel scrolling, and a scrollbar. Popups share the browser's toe
    contexts, so toe:// pages, the detective's paper trail, and link
    navigation all work inside them.

    Special links a page can use:
        popup:close            close this popup
        popup:spawn:<url>      open another popup (the classic adware chain)
    """

    TITLE_BAR = 22

    def __init__(self, browser, url, width=320, height=240):
        self.browser = browser
        self.width = width
        self.height = height
        self.window = tkinter.Toplevel(browser.window)
        self.window.title("")
        self.window.geometry(f"{width}x{height}")
        self.canvas = tkinter.Canvas(
            self.window, width=width, height=height,
            bg="white", highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        self.tab = Tab(height - self.TITLE_BAR, browser)
        self.tab.load(URL(str(url)) if isinstance(url, str) else url)
        self.context_menu = ContextMenu()
        self._bind()
        self.draw()

    def _bind(self):
        self.window.bind("<MouseWheel>", self._on_wheel)
        self.window.bind("<Button-4>", lambda e: self._scroll(-SCROLL_STEP))
        self.window.bind("<Button-5>", lambda e: self._scroll(SCROLL_STEP))
        self.window.bind("<Button-1>", self._on_click)
        self.window.bind("<Button-3>", self._on_context_menu)
        self.window.bind("<Motion>", self._on_motion)
        self.window.bind("<Escape>", self._on_escape)

    def _on_wheel(self, e):
        self._scroll(-e.delta if abs(e.delta) < 30
                     else -int(e.delta / 30) * SCROLL_STEP)

    def _scroll(self, delta):
        self.tab.scroll_by(delta)
        self.draw()

    def _on_click(self, e):
        if self.context_menu.open_:
            self._context_menu_click(e.x, e.y)
            return
        if e.y < self.TITLE_BAR:
            if e.x >= self.width - 20:
                self.window.destroy()
            return
        dest = self.tab.click(e.x, e.y - self.TITLE_BAR)
        if dest:
            self._navigate(dest)
        self.draw()

    # -- context menu ----------------------------------------------------

    def _on_context_menu(self, e):
        items = self._context_items(e.x, e.y)
        self.context_menu.open(e.x, e.y, items, self.width, self.height)
        self.draw()

    def _context_menu_click(self, x, y):
        menu = self.context_menu
        if not menu.point_in_menu(x, y):
            menu.close()
            self.draw()
            return
        idx = menu.hit(x, y)
        if idx < 0:
            menu.close()
            self.draw()
            return
        menu.hover = idx
        cb = menu.activate()
        menu.close()
        self.draw()
        if cb:
            cb()

    def _on_motion(self, e):
        if self.context_menu.open_ and self.context_menu.set_hover(e.x, e.y):
            self.context_menu.draw(self.canvas)

    def _on_escape(self, e):
        if self.context_menu.open_:
            self.context_menu.close()
            self.draw()

    def _context_items(self, x, y):
        if y < self.TITLE_BAR:
            return [
                ("Reload", lambda: self.tab.load(self.tab.url, push=False),
                 True),
                None,
                ("Close Popup", self.window.destroy, True),
            ]
        items = [
            ("Back", self.tab.go_back, bool(self.tab.history)),
            ("Forward", self.tab.go_forward, bool(self.tab.future)),
            ("Reload", lambda: self.tab.load(self.tab.url, push=False), True),
            None,
            ("Copy Page URL",
             lambda u=str(self.tab.url): self._copy_text(u),
             bool(self.tab.url)),
            ("Close Popup", self.window.destroy, True),
        ]
        return items

    def _copy_text(self, text):
        try:
            self.window.clipboard_clear()
            self.window.clipboard_append(text)
        except tkinter.TclError:
            pass

    def _navigate(self, dest):
        if isinstance(dest, FormAction):
            s = str(dest.url)
            if s == "popup:close":
                self.window.destroy()
                return
            if s.startswith("popup:spawn:"):
                for ctx in self.browser.toe_contexts:
                    if hasattr(ctx, "popup"):
                        ctx.popup(s[len("popup:spawn:"):])
                return
            self.tab.load(dest.url, payload=dest.payload)
        else:
            s = str(dest)
            if s == "popup:close":
                self.window.destroy()
                return
            if s.startswith("popup:spawn:"):
                for ctx in self.browser.toe_contexts:
                    if hasattr(ctx, "popup"):
                        ctx.popup(s[len("popup:spawn:"):])
                return
            self.tab.load(dest)
        self.draw()

    def draw(self):
        c = self.canvas
        c.delete("all")
        self.tab.tab_height = self.height - self.TITLE_BAR
        self.tab.draw(c, self.TITLE_BAR)
        c.create_rectangle(0, 0, self.width, self.TITLE_BAR,
                           fill="#d0d0d0", width=0)
        c.create_line(0, self.TITLE_BAR, self.width, self.TITLE_BAR,
                      fill="#999")
        c.create_text(6, self.TITLE_BAR // 2, text=str(self.tab.url)[:40],
                      anchor="w", font=get_font(10, "normal", "roman",
                                                "Helvetica"), fill="#333")
        c.create_text(self.width - 10, self.TITLE_BAR // 2, text="×",
                      font=get_font(12, "bold", "roman", "Helvetica"),
                      fill="#333")
        # Scrollbar.
        view = self.height - self.TITLE_BAR
        total = self.tab.content_height()
        if total > view:
            frac = view / total
            thumb_h = max(20, view * frac)
            thumb_top = self.TITLE_BAR + (view - thumb_h) * (
                self.tab.scroll / (total - view))
            c.create_rectangle(self.width - 6, thumb_top,
                                self.width - 2, thumb_top + thumb_h,
                                fill="#9aa0a6", width=0)
        self.context_menu.draw(self.canvas)


class JSResponse:
    """Host `fetch()` Response: ok/status/statusText/headers, text(), json()."""

    def __init__(self, interp, headers, body, ctype, status):
        self._interp = interp
        self._headers = {k: v for k, v in headers.items()}
        self._body = body
        self._ctype = ctype
        self._status = status

    def js_get(self, name):
        if name == "ok":
            return 200 <= self._status < 300
        if name == "status":
            return self._status
        if name == "statusText":
            return "OK" if 200 <= self._status < 300 else "error"
        if name == "headers":
            return dict(self._headers)
        if name == "text":
            return self._text
        if name == "json":
            return self._json
        return UNDEFINED

    def _text(self):
        p = self._interp.create_promise()
        p.resolve(self._body)
        return p

    def _json(self):
        p = self._interp.create_promise()
        try:
            p.resolve(json.loads(self._body))
        except Exception as e:  # noqa: BLE001 - bad JSON body
            p.reject(str(e))
        return p


class _JSXHRCtor:
    """The `XMLHttpRequest` global: a constructor object."""

    def __init__(self, tab):
        self._tab = tab

    def js_new(self, *args):
        return _JSXHR(self._tab)


class _JSXHR:
    """A minimal XMLHttpRequest: open/send, readyState/status/responseText,
    and onreadystatechange/onload/onerror handlers."""

    def __init__(self, tab):
        self._tab = tab
        self._method = "GET"
        self._url = None
        self._headers = {}
        self._ready = 0
        self._status = 0
        self._text = ""
        self.onreadystatechange = None
        self.onload = None
        self.onerror = None

    def js_get(self, name):
        if name == "open":
            return self.open
        if name == "send":
            return self.send
        if name == "setRequestHeader":
            return self.set_request_header
        if name == "readyState":
            return self._ready
        if name == "status":
            return self._status
        if name == "responseText":
            return self._text
        if name == "onreadystatechange":
            return self.onreadystatechange
        if name == "onload":
            return self.onload
        if name == "onerror":
            return self.onerror
        return UNDEFINED

    def js_set(self, name, value):
        if name in ("onreadystatechange", "onload", "onerror"):
            setattr(self, name, value)

    def open(self, method, url, async_=True):
        self._method = str(method).upper()
        try:
            self._url = self._tab.base_url.resolve(str(url)) \
                if self._tab.base_url else URL(str(url))
        except Exception:  # noqa: BLE001 - keep the raw string for the error
            self._url = str(url)
        self._ready = 1

    def set_request_header(self, name, value):
        self._headers[str(name)] = str(value)

    def send(self, body=None):
        if self._ready < 1 or self._url is None:
            return UNDEFINED
        self._ready = 2
        target = self._url
        payload = None
        if isinstance(body, str) and body:
            payload = body
        elif body is not None and not (body is True or body is False):
            payload = self._tab._js_interp.repr(body) if body is not UNDEFINED \
                else None

        def worker():
            try:
                headers, resp, ctype = target.request(payload=payload)
                err = None
                status = 200
            except Exception as e:  # noqa: BLE001 - network failure
                headers, resp, ctype, status, err = {}, "", "text/plain", 0, str(e)
            self._tab._js_xhr_results.append((self, headers, resp, ctype,
                                              status, err))

        threading.Thread(target=worker, daemon=True).start()
        return UNDEFINED

    def _finish(self, headers, body, status, err):
        self._ready = 4
        self._status = status if not err else 0
        self._text = body
        interp = self._tab._js_interp
        for handler in (self.onreadystatechange, self.onload if not err
                        else self.onerror):
            if handler is not None and handler is not UNDEFINED:
                try:
                    interp.call(handler)
                except JSException as e:
                    if interp is not None:
                        interp.logs.append(f"JS error: {e}")


def _js_match_media(query):
    return {
        "matches": False,
        "media": query,
        "onchange": None,
        "addEventListener": lambda *a: None,
        "removeEventListener": lambda *a: None,
        "addListener": lambda *a: None,
        "removeListener": lambda *a: None,
    }


def _js_computed_style(el):
    style = getattr(el, "js_get", None)
    computed = {}

    def getter(name):
        if style is not None:
            try:
                v = style("style")
                if v is not None and hasattr(v, "js_get"):
                    return v.js_get(str(name))
            except Exception:
                pass
        return computed.get(str(name), "")

    return {
        "getPropertyValue": lambda name: getter(name),
        "getPropertyPriority": lambda name: "",
    }


def _js_storage():
    store = {}

    def get(name):
        if name == "length":
            return len(store)
        if name == "getItem":
            return lambda k: store.get(str(k), None)
        if name == "setItem":
            return lambda k, v: store.__setitem__(str(k), str(v))
        if name == "removeItem":
            return lambda k: store.pop(str(k), None)
        if name == "clear":
            return lambda: store.clear()
        if name == "key":
            return lambda i: list(store)[int(i)] if int(i) < len(store) else None
        return store.get(str(name), None)

    def set(name, value):
        store[str(name)] = str(value)

    return {"js_get": get, "js_set": set}


def _js_fresh_promise(interp):
    p = interp.create_promise()
    p.resolve(UNDEFINED)
    return p


def _js_random_values(arr):
    if not isinstance(arr, list):
        return arr
    for i in range(len(arr)):
        arr[i] = int.from_bytes(os.urandom(4), "little") & 0xFFFFFFFF
    return arr


class _JSEventCtor:
    """Minimal Event/CustomEvent/MouseEvent constructor."""

    def js_new(self, *args):
        event_type = args[0] if args else ""
        return _JSEvent(event_type)

    def js_call(self, *args):
        return self.js_new(*args)


class _JSEvent:
    def __init__(self, event_type):
        self.type = str(event_type)
        self.bubbles = False
        self.cancelable = False
        self.defaultPrevented = False
        self.target = None
        self.currentTarget = None
        self.timeStamp = time.time() * 1000

    def js_get(self, name):
        if name == "type":
            return self.type
        if name == "bubbles":
            return self.bubbles
        if name == "cancelable":
            return self.cancelable
        if name == "defaultPrevented":
            return self.defaultPrevented
        if name == "target":
            return self.target
        if name == "currentTarget":
            return self.currentTarget
        if name == "timeStamp":
            return self.timeStamp
        if name == "preventDefault":
            return self._prevent_default
        if name == "stopPropagation":
            return lambda: None
        if name == "stopImmediatePropagation":
            return lambda: None
        return UNDEFINED

    def _prevent_default(self):
        self.defaultPrevented = True


class _AboutURL:
    """Placeholder URL for the internal welcome page."""
    view_source = False
    fragment = ""

    def __init__(self, bookmarks_provider=None):
        self.bookmarks_provider = bookmarks_provider

    def resolve(self, url):
        if url == "about:blank":
            return _AboutURL(self.bookmarks_provider)
        if url == "about:bookmarks":
            return _BookmarksURL(self.bookmarks_provider)
        if url == "about:history":
            return _HistoryURL(lambda: {"back": [], "current": "", "forward": []})
        return URL(url) if "://" in url else URL("https://" + url)

    def request(self, payload=None):
        return {}, WELCOME_HTML, "text/html"

    def __str__(self):
        return "about:blank"


class _BookmarksURL:
    """Internal URL for the bookmarks page."""
    view_source = False
    fragment = ""

    def __init__(self, bookmarks_provider=None):
        self.bookmarks_provider = bookmarks_provider or (lambda: [])

    def resolve(self, url):
        if url == "about:blank":
            return _AboutURL(self.bookmarks_provider)
        if url == "about:bookmarks":
            return _BookmarksURL(self.bookmarks_provider)
        if url == "about:history":
            return _HistoryURL(lambda: {"back": [], "current": "", "forward": []})
        return URL(url) if "://" in url else URL("https://" + url)

    def request(self, payload=None):
        return {}, bookmarks_html(self.bookmarks_provider()), "text/html"

    def __str__(self):
        return "about:bookmarks"


class _HistoryURL:
    """Internal URL for the current tab's history page."""
    view_source = False
    fragment = ""

    def __init__(self, snapshot_provider=None):
        self.snapshot_provider = snapshot_provider or (
            lambda: {"back": [], "current": "", "forward": []})

    def resolve(self, url):
        if url == "about:blank":
            return _AboutURL()
        if url == "about:bookmarks":
            return _BookmarksURL()
        if url == "about:history":
            return _HistoryURL(self.snapshot_provider)
        return URL(url) if "://" in url else URL("https://" + url)

    def request(self, payload=None):
        return {}, history_html(self.snapshot_provider()), "text/html"

    def __str__(self):
        return "about:history"


def bookmarks_html(bookmarks):
    items = []
    for entry in bookmarks:
        safe = html.escape(entry, quote=True)
        items.append(f'<li><a href="{safe}">{safe}</a></li>')
    listing = "\n".join(items) if items else "<li>No bookmarks yet.</li>"
    return f"""
<!doctype html>
<html><head><title>Bookmarks</title>
<style>
  body {{ font-family: Helvetica; margin: 60px; color: #222; }}
  h1 {{ font-size: 40px; color: #1a73e8; }}
  .sub {{ color: #666; font-size: 18px; }}
  li {{ margin-top: 8px; }}
  a {{ color: #1a73e8; word-break: break-all; }}
</style></head>
<body>
  <h1>Bookmarks</h1>
  <p class="sub">Saved pages from Ctrl-D or the star button.</p>
  <ul>{listing}</ul>
</body></html>
"""


def history_html(snapshot):
    back_items = []
    for url in snapshot.get("back", []):
        safe = html.escape(url, quote=True)
        back_items.append(f'<li><a href="{safe}">{safe}</a></li>')
    current = html.escape(snapshot.get("current", "") or "(none)", quote=True)
    forward_items = []
    for url in snapshot.get("forward", []):
        safe = html.escape(url, quote=True)
        forward_items.append(f'<li><a href="{safe}">{safe}</a></li>')
    back_list = "\n".join(back_items) if back_items else "<li>None</li>"
    forward_list = "\n".join(forward_items) if forward_items else "<li>None</li>"
    return f"""
<!doctype html>
<html><head><title>History</title>
<style>
  body {{ font-family: Helvetica; margin: 60px; color: #222; }}
  h1 {{ font-size: 40px; color: #1a73e8; }}
  h2 {{ margin-top: 30px; }}
  .sub {{ color: #666; font-size: 18px; }}
  li {{ margin-top: 8px; }}
  a {{ color: #1a73e8; word-break: break-all; }}
  .current {{ background: #f0f4ff; padding: 10px; border-left: 4px solid #1a73e8; }}
</style></head>
<body>
  <h1>History</h1>
  <p class="sub">Current tab timeline. Open with <b>Ctrl-H</b>.</p>
  <h2>Back stack (oldest → newest)</h2>
  <ul>{back_list}</ul>
  <h2>Current page</h2>
  <p class="current">{current}</p>
  <h2>Forward stack (next first)</h2>
  <ul>{forward_list}</ul>
</body></html>
"""


WELCOME_HTML = """
<!doctype html>
<html><head><title>New Tab</title>
<style>
  body { font-family: Helvetica; margin: 60px; color: #222; }
  h1 { font-size: 40px; color: #1a73e8; }
  .sub { color: #666; font-size: 18px; }
  ul { margin-top: 20px; }
  li { margin-top: 6px; }
  code { background: #f0f0f0; }
  a { color: #1a73e8; }
</style></head>
<body>
  <h1>🦶 FeetBrowser</h1>
  <p class="sub">A web browser built from scratch — its own HTTP client,
  HTML parser, CSS engine, and layout engine.</p>
  <h3>Try these</h3>
  <ul>
    <li><a href="https://example.com">example.com</a> — the classic test page</li>
    <li><a href="https://info.cern.ch/hypertext/WWW/TheProject.html">the first web page ever</a></li>
    <li><a href="https://news.ycombinator.com">Hacker News</a></li>
    <li><a href="https://en.wikipedia.org/wiki/Web_browser">Wikipedia: Web browser</a></li>
    <li><a href="about:bookmarks">about:bookmarks</a> — your saved pages</li>
    <li><a href="about:history">about:history</a> — back/forward timeline</li>
    <li><a href="view-source:https://example.com">view-source:example.com</a></li>
  </ul>
  <h3>Your toes</h3>
  <ul>
    <li><a href="toe://hub">toe://hub</a> — browse and install toes</li>
  </ul>
  <h3>Shortcuts</h3>
  <ul>
    <li><b>Ctrl-L</b> focus address bar &nbsp; <b>Ctrl-T</b> new tab &nbsp;
        <b>Ctrl-W</b> close tab &nbsp; <b>Ctrl-Tab</b> / <b>Ctrl-PgUp/Dn</b> cycle tabs</li>
    <li><b>Ctrl-R</b> reload &nbsp; <b>Ctrl-D</b> bookmark page &nbsp;
        <b>Ctrl-H</b> history &nbsp; <b>Alt-Left/Right</b> back / forward</li>
    <li><b>↑ ↓ / wheel</b> scroll &nbsp; <b>PgUp/Dn</b> scroll by page &nbsp;
        <b>Home / End</b> jump to top / bottom &nbsp; <b>Esc</b> blur</li>
    <li><b>Middle-click</b> or <b>Ctrl-click</b> a link to open it in a new tab</li>
  </ul>
  <p class="sub">Forms are wired up, you can focus a text field to type, press
  Enter to submit, and toggle checkboxes. Images, floats, flexbox, CSS grid,
  and tables also render. JavaScript is a work in progress: scripts might execute on
  load, could rewrite the page via the DOM, and occasionally handle clicks.</p>
  <p class="sub">Type a URL or a search term in the address bar to begin.</p>
</body></html>
"""


def main():
    browser = Browser()
    start = sys.argv[1] if len(sys.argv) > 1 else "about:blank"
    browser.new_tab(start)
    browser.run()


if __name__ == "__main__":
    main()
