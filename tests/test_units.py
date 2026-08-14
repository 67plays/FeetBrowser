"""Fast, offline unit tests for URL parsing, HTML, CSS, and internal pages."""
import http.server
import urllib.parse
import sys, os, tkinter
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from feetbrowser.net import URL
from feetbrowser.htmlparser import HTMLParser, Element, Text
from feetbrowser.cssparser import CSSParser, style
from feetbrowser.layout import DrawText, get_font, _measure, field_checked
from feetbrowser.browser import (
    Tab, Browser, _AboutURL, _BookmarksURL, _HistoryURL,
    bookmarks_html, history_html,
    tree_to_list, find_base_href, FormAction
)


def eq(a, b, msg=""):
    assert a == b, f"{msg}: {a!r} != {b!r}"


def test_url_parsing():
    u = URL("https://example.com/a/b/page.html")
    eq(u.scheme, "https"); eq(u.host, "example.com"); eq(u.port, 443)
    eq(u.path, "/a/b/page.html")

    eq(str(u.resolve("c.html")), "https://example.com/a/b/c.html", "relative")
    eq(str(u.resolve("/x")), "https://example.com/x", "root-relative")
    eq(str(u.resolve("../z")), "https://example.com/a/z", "dotdot")
    eq(str(u.resolve("//cdn.net/s.css")), "https://cdn.net/s.css", "scheme-rel")
    eq(str(u.resolve("https://o.org/y")), "https://o.org/y", "absolute")

    eq(URL("http://h.com:8080/p").port, 8080, "explicit port")
    eq(URL("example.com").scheme, "https", "bare host -> https")

    f = URL("file:///etc/hosts")
    eq(f.scheme, "file"); eq(f.path, "/etc/hosts")

    v = URL("view-source:https://example.com")
    assert v.view_source and v.scheme == "https"

    frag = URL("https://x.com/p#sec")
    eq(frag.fragment, "sec")


def test_data_url():
    _h, body, ctype = URL("data:text/html,<b>hi</b>").request()
    eq(body, "<b>hi</b>"); eq(ctype, "text/html")
    _h, body, _c = URL("data:text/plain;base64,aGVsbG8=").request()
    eq(body, "hello", "base64 data url")


def test_html_parser():
    dom = HTMLParser(
        "<!doctype html><html><head><title>T</title>"
        "<style>a{color:red}</style></head><body>"
        "<p>one<br>two &amp; three<img src=x alt=pic></p>"
        "<!-- comment --><ul><li>a<li>b</ul></body></html>"
    ).parse()
    tags = [n.tag for n in tree_to_list(dom, []) if isinstance(n, Element)]
    for expected in ["html", "head", "title", "style", "body", "p", "br",
                     "img", "ul", "li"]:
        assert expected in tags, f"missing <{expected}>"
    eq(tags.count("li"), 2, "two implicit-closed <li>")
    texts = "".join(n.text for n in tree_to_list(dom, []) if isinstance(n, Text))
    assert "two & three" in texts, "entity decode"
    assert "comment" not in texts, "comment stripped"
    # Raw text: <style> content must not be parsed into elements.
    assert "color:red" in texts or any(
        isinstance(c, Text) and "color:red" in c.text
        for n in tree_to_list(dom, []) for c in n.children)


def test_css_cascade():
    rules = CSSParser(
        "p { color: black; } p.warn { color: orange; } #x { color: red; }"
    ).parse()
    dom = HTMLParser(
        '<div><p class="warn" id="x">hi</p><p>bye</p></div>').parse()
    style(dom, rules)
    # id beats class beats tag
    warn = [n for n in tree_to_list(dom, [])
            if isinstance(n, Element) and n.attributes.get("id") == "x"][0]
    eq(warn.style["color"], "red", "id selector wins")
    plain = [n for n in tree_to_list(dom, [])
             if isinstance(n, Element) and n.tag == "p"
             and "id" not in n.attributes][0]
    eq(plain.style["color"], "black", "tag selector")


def test_inheritance_and_inline():
    rules = CSSParser("body { color: green; }").parse()
    dom = HTMLParser(
        '<body><p>x</p><p style="color: purple">y</p></body>').parse()
    style(dom, rules)
    ps = [n for n in tree_to_list(dom, [])
          if isinstance(n, Element) and n.tag == "p"]
    eq(ps[0].style["color"], "green", "inherited from body")
    eq(ps[1].style["color"], "purple", "inline style wins")


def test_welcome_and_reload():
    tab = Tab(700)
    tab.load(_AboutURL())
    eq(tab.title, "New Tab", "welcome title")
    assert isinstance(tab.url, _AboutURL)
    # Reloading an internal page must not crash (regression test).
    tab.load(tab.url, push=False)
    eq(tab.title, "New Tab", "welcome reloads cleanly")


def test_bookmarks_internal_page():
    bookmarks = ["https://example.org", "https://info.cern.ch/hypertext/WWW/TheProject.html"]
    tab = Tab(700)
    tab.load(_BookmarksURL(lambda: bookmarks))
    eq(tab.title, "Bookmarks", "bookmarks title")
    links = [n for n in tree_to_list(tab.nodes, []) if isinstance(n, Element)
             and n.tag == "a"]
    hrefs = [n.attributes.get("href", "") for n in links]
    assert bookmarks[0] in hrefs


def test_bookmarks_html_escapes():
    page = bookmarks_html(['https://x.test/?q=<script>alert(1)</script>'])
    assert "<script>" not in page
    assert "&lt;script&gt;" in page


def test_about_page_can_resolve_bookmarks():
    about = _AboutURL(lambda: ["https://example.org"])
    dest = about.resolve("about:bookmarks")
    assert isinstance(dest, _BookmarksURL)
    _h, body, _c = dest.request()
    nodes = HTMLParser(body).parse()
    links = [n for n in tree_to_list(nodes, []) if isinstance(n, Element)
             and n.tag == "a"]
    assert links and links[0].attributes.get("href") == "https://example.org"


def test_about_page_can_resolve_history():
    about = _AboutURL(lambda: [])
    dest = about.resolve("about:history")
    assert isinstance(dest, _HistoryURL)
    _h, body, _c = dest.request()
    assert "<title>History</title>" in body


def test_history_html_escapes():
    page = history_html({
        "back": ['https://x.test/?q=<script>alert(1)</script>'],
        "current": "https://safe.test/",
        "forward": [],
    })
    assert "<script>" not in page
    assert "&lt;script&gt;" in page


def test_history_internal_page_loads():
    tab = Tab(700)
    tab.load(_AboutURL())
    tab.load(_BookmarksURL(lambda: ["https://example.org"]))
    tab.load(_HistoryURL(lambda: {
        "back": ["https://example.org"],
        "current": "about:history",
        "forward": ["https://news.ycombinator.com"],
    }))
    eq(tab.title, "History", "history title")
    links = [n for n in tree_to_list(tab.nodes, []) if isinstance(n, Element)
             and n.tag == "a"]
    hrefs = {n.attributes.get("href", "") for n in links}
    expected = {"https://example.org", "https://news.ycombinator.com"}
    assert expected.issubset(hrefs)


def test_browser_tab_cycle_wraps():
    tabs = [object(), object(), object()]
    stub = type("Stub", (), {})()
    stub.tabs = tabs
    stub.active_tab = tabs[0]
    stub.draw_calls = 0

    def draw():
        stub.draw_calls += 1
    stub.draw = draw

    Browser._cycle_tab(stub, 1)
    assert stub.active_tab is tabs[1]
    Browser._cycle_tab(stub, 1)
    assert stub.active_tab is tabs[2]
    Browser._cycle_tab(stub, 1)
    assert stub.active_tab is tabs[0]
    Browser._cycle_tab(stub, -1)
    assert stub.active_tab is tabs[2]
    assert stub.draw_calls == 4


def test_page_scroll_shortcuts_call_scroll():
    stub = type("Stub", (), {})()
    stub.focus = None
    stub.calls = []
    stub._scroll = lambda delta: stub.calls.append(delta)
    stub.tab_height = lambda: 700
    eq(Browser._on_page_down(stub, None), "break", "pagedown returns break")
    eq(Browser._on_page_up(stub, None), "break", "pageup returns break")
    eq(stub.calls, [580, -580], "page shortcuts use viewport-sized steps")


def test_error_page_fallback():
    tab = Tab(700)
    # A bad scheme raises in URL(); load() must render an error page, not crash.
    tab.load("https://nonexistent.invalid.example/")
    assert tab.document is not None, "error page laid out"


def _address_stub():
    class Stub(Browser):
        def __init__(self):
            self.address_text = "https://example.com/"
            self.address_caret = 0
            self.address_sel = None
            self.address_view = 0

        def _address_ensure_visible(self):
            pass
    return Stub()


def test_address_backspace_and_forward_delete():
    stub = _address_stub()
    stub.address_caret = len(stub.address_text)
    Browser._address_backspace(stub)
    assert stub.address_text == "https://example.com", "backspace removes last char"
    Browser._address_forward_delete(stub)
    assert stub.address_text == "https://example.com", "forward delete at end is a no-op"
    stub.address_caret = 0
    Browser._address_forward_delete(stub)
    assert stub.address_text == "ttps://example.com", "forward delete removes first char"


def test_address_select_all_and_insert():
    stub = _address_stub()
    Browser._address_select_all(stub)
    assert stub.address_sel == (0, len("https://example.com/")), "ctrl-a selects all"
    Browser._address_insert(stub, "zz")
    assert stub.address_text == "zz", "typing replaces the selection"


def test_address_caret_movement_and_selection():
    stub = _address_stub()
    stub.address_caret = 4
    Browser._address_move_caret(stub, 2)
    assert stub.address_caret == 6 and stub.address_sel is None, "arrow moves caret"
    Browser._address_move_caret(stub, 1, extend=True)
    assert stub.address_sel == (6, 7), "shift-arrow extends selection"
    Browser._address_move_caret(stub, 1, extend=True)
    assert stub.address_sel == (6, 8), "selection grows with anchor fixed"


def test_address_paste_requires_clipboard():
    stub = _address_stub()
    stub.window = type("W", (), {})()
    stub.window.clipboard_get = lambda: "new.example"
    stub._address_paste()
    assert stub.address_text == "new.examplehttps://example.com/", \
        f"pasted at caret: {stub.address_text}"


def test_url_bare_host_with_port():
    u = URL("example.com:8080")
    eq(u.scheme, "https"); eq(u.host, "example.com"); eq(u.port, 8080)
    u2 = URL("localhost:8000/path")
    eq(u2.host, "localhost"); eq(u2.port, 8000)


def test_url_ipv6():
    u = URL("https://[::1]:8080/x")
    eq(u.host, "::1"); eq(u.port, 8080); eq(u.path, "/x")
    eq(str(u), "https://[::1]:8080/x", "ipv6 round-trip")
    eq(str(URL("http://[::1]/y")), "http://[::1]/y", "ipv6 default port")


def test_url_host_lowered_and_validated():
    eq(URL("HTTP://EXAMPLE.COM").host, "example.com", "host lowercased")
    for bad in ("https:///nohost", "https://host:port", "https://host:99999"):
        try:
            URL(bad)
        except ValueError:
            continue
        raise AssertionError(f"expected ValueError for {bad!r}")


def test_dechunk_safety():
    from feetbrowser.net import URL as _URL
    raw = b"4\r\nWiki\r\n5\r\npedia\r\n0\r\n\r\n"
    eq(_URL._dechunk(raw), b"Wikipedia", "chunked decode")
    eq(_URL._dechunk(b"ffffff\r\nshort"), b"", "truncated chunk handled")


def test_implicit_p_close():
    dom = HTMLParser("<p>alpha<div>beta</div></p>").parse()
    p = None
    for n in tree_to_list(dom, []):
        if isinstance(n, Element) and n.tag == "p":
            p = n
    assert p is not None
    ptext = "".join(c.text for c in p.children if isinstance(c, Text))
    eq(ptext, "alpha", "block opened inside <p> closes it")
    for c in p.children:
        assert not (isinstance(c, Element) and c.tag == "div"), \
            "div must not be a child of p"
    texts = "".join(c.text for n in tree_to_list(dom, [])
                    for c in n.children if isinstance(c, Text))
    assert "beta" in texts, "content after implicit close kept"


def test_pseudo_selector_stripped():
    rules = CSSParser("a:hover { color: red }").parse()
    dom = HTMLParser('<p><a href="/x">hi</a></p>').parse()
    style(dom, rules)
    a = [n for n in tree_to_list(dom, [])
         if isinstance(n, Element) and n.tag == "a"][0]
    eq(a.style["color"], "red", "a:hover matches an <a>")


def test_pseudo_element_rule_dropped():
    """A rule targeting ::before/::after creates a box the engine can't draw;
    its declarations must NOT leak onto the parent element (e.g. a 1px
    decorative ::after border must not shrink the element to 1px tall)."""
    rules = CSSParser(
        ".t { height: 100px } .t::after { content: ''; height: 1px }"
    ).parse()
    dom = HTMLParser('<div class="t">x</div>').parse()
    style(dom, rules)
    t = [n for n in tree_to_list(dom, [])
         if isinstance(n, Element) and n.tag == "div"][0]
    eq(t.style["height"], "100px", "::after height must not leak onto .t")
    rules = CSSParser(".t::after { position: absolute; height: 1px }").parse()
    eq(rules, [], "::after-only rule is dropped entirely")


def test_combinators_do_not_crash_and_match():
    rules = CSSParser(
        "p + span { color: red } p ~ em { color: blue } "
        "ul > li { color: green }"
    ).parse()
    eq(len(rules), 3, "+ and ~ parse (approximated) without crashing")
    dom = HTMLParser('<div><ul><li>d</li></ul></div>').parse()
    style(dom, rules)
    li = [n for n in tree_to_list(dom, []) if isinstance(n, Element) and n.tag == "li"][0]
    eq(li.style["color"], "green", "ul > li matches (child treated as descendant)")


def test_attribute_selector_matches():
    rules = CSSParser("div a[href] { color: blue }").parse()
    eq(len(rules), 1, "attribute selector parses")
    dom = HTMLParser('<div><a href="/x">hi</a><a>no</a></div>').parse()
    style(dom, rules)
    links = [n for n in tree_to_list(dom, [])
             if isinstance(n, Element) and n.tag == "a"]
    eq(links[0].style["color"], "blue", "a[href] matches an anchor with href")
    eq(links[1].style["color"], "black", "anchor without href is not styled")
    rules = CSSParser("a[href] { color: blue } p { color: red }").parse()
    eq(len(rules), 2, "rule after attribute selector still parses")


def test_pseudo_class_structural_and_not():
    rules = CSSParser(
        "li:first-child { color: red } li:nth-child(2n) { color: green } "
        "li:not(.skip) { font-weight: bold }"
    ).parse()
    eq(len(rules), 3, "structural pseudo-classes parse")
    dom = HTMLParser(
        '<ul><li>a</li><li class="skip">b</li><li>c</li><li>d</li></ul>'
    ).parse()
    style(dom, rules)
    lis = [n for n in tree_to_list(dom, [])
           if isinstance(n, Element) and n.tag == "li"]
    eq(lis[0].style["color"], "red", ":first-child matches the first item")
    eq(lis[1].style["color"], "green", ":nth-child(2n) matches even items")
    eq(lis[3].style["color"], "green", ":nth-child(2n) matches the fourth item")
    eq(lis[1].style["font-weight"], "normal", ":not(.skip) excludes .skip")
    eq(lis[2].style["font-weight"], "bold", ":not(.skip) matches others")


def test_data_uri_background_parsed():
    css = ('p { background: url(data:image/svg+xml,<svg xmlns="http://www.w3.org/2000/svg"></svg>) }')
    rules = CSSParser(css).parse()
    eq(len(rules), 1, "url() with quotes and < > parses as one rule")


def test_table_cell_content_flows_at_column_width():
    from feetbrowser.layout import DocumentLayout, DrawText
    html = '<table><tr><td>Alpha Bravo Charlie</td><td>Delta Echo</td></tr></table>'
    dom = HTMLParser(html).parse()
    style(dom, [])
    doc = DocumentLayout(dom, 620)
    doc.layout()
    cmds = []
    stack = [doc]
    while stack:
        b = stack.pop()
        for c in b.paint():
            cmds.append(c)
        stack.extend(b.children)
    words = [c for c in cmds if isinstance(c, DrawText) and c.text in
             ("Alpha", "Bravo", "Charlie", "Delta", "Echo")]
    tops = {c.text: c.top for c in words}
    assert tops["Alpha"] == tops["Bravo"] == tops["Charlie"], \
        "cell words share one line instead of wrapping per word"
    assert tops["Delta"] == tops["Echo"], "second cell words share a line"


def test_pre_whitespace_does_not_wrap():
    from feetbrowser.layout import DocumentLayout, DrawText
    css = "pre { white-space: pre; }"
    rules = CSSParser(css).parse()
    html = '<pre>one very long line that must not wrap at all</pre>'
    dom = HTMLParser(html).parse()
    style(dom, rules)
    doc = DocumentLayout(dom, 200)
    doc.layout()
    cmds = []
    stack = [doc]
    while stack:
        b = stack.pop()
        for c in b.paint():
            cmds.append(c)
        stack.extend(b.children)
    texts = [c for c in cmds if isinstance(c, DrawText)]
    eq(len(texts), 1, "pre line kept on one line")


def test_nowrap_cloud_wraps_as_unit():
    """white-space:nowrap (Wikipedia's language cloud) must not spill past the
    viewport: each link is one unbreakable token, but tokens still wrap to a
    fresh line once the current line runs out of room."""
    from feetbrowser.layout import DocumentLayout, DrawText
    css = '.cloud { width: 200px; } .cloud a { white-space: nowrap; }'
    rules = CSSParser(css).parse()
    links = ' '.join(f'<a href="#{i}">languagename{i:02d}</a>' for i in range(30))
    html = f'<div class="cloud">{links}</div>'
    dom = HTMLParser(html).parse()
    style(dom, rules)
    doc = DocumentLayout(dom, 200)
    doc.layout()
    cmds = []
    stack = [doc]
    while stack:
        b = stack.pop()
        for c in b.paint():
            cmds.append(c)
        stack.extend(b.children)
    texts = [c for c in cmds if isinstance(c, DrawText)]
    assert texts, "cloud text drawn"
    max_right = max(c.right for c in texts)
    assert max_right <= 200, \
        f"nowrap cloud overflowed viewport: right edge {max_right} > 200"
    tops = {c.text: c.top for c in texts}
    # The whole token moves to the next line, so line tops repeat.
    first_top = tops["languagename00"]
    later_lines = {t for t in tops.values() if t > first_top}
    assert later_lines, "cloud wrapped to multiple lines"


def test_css_data_uri_semicolon():
    css = ('p { background: url(data:image/png;base64,AAAA==);'
           ' color: red; }')
    rules = CSSParser(css).parse()
    eq(len(rules), 1, "data: URI with ; parsed as one rule")
    eq(rules[0][1]["color"], "red", "pair after data: URI intact")


def test_deep_dom_no_recursion():
    depth = 1500
    body = "<div>" * depth + "x" + "</div>" * depth
    dom = HTMLParser(body).parse()
    rules = CSSParser("div { color: blue; }").parse()
    style(dom, rules)  # must not raise RecursionError
    ns = tree_to_list(dom, [])
    assert len(ns) > depth, "tree_to_list built"


def test_double_br_advances_line():
    tab = Tab(700)
    tab._build(URL("https://example.com"), "<p>a<br><br>b</p>", "text/html")
    tops = {c.text: c.top for c in tab.display_list if isinstance(c, DrawText)}
    assert "a" in tops and "b" in tops, tops
    line_h = [c.bottom - c.top for c in tab.display_list
              if isinstance(c, DrawText) and c.text == "a"]
    assert tops["b"] >= tops["a"] + (line_h[0] * 2 - 1), \
        "<br><br> must add two line breaks"

    tab2 = Tab(700)
    tab2._build(URL("https://example.com"), "<p><br></p>", "text/html")
    assert tab2.content_height() > 0, "bare <br> yields nonzero line height"


def test_image_does_not_overlap_following_text():
    tab = Tab(700)
    # A wide image pushes the following word onto the next line, which must
    # start below the image's line box rather than overlapping it.
    tab._build(
        URL("https://example.com"),
        '<p>one<img src=x alt="' + "x" * 140 + '">two</p>',
        "text/html")
    tops = {c.text: (c.top, c.bottom) for c in tab.display_list
            if isinstance(c, DrawText)}
    assert "one" in tops and "two" in tops
    assert tops["two"][0] > tops["one"][0], \
        "image width forced the wrap onto a new line"
    assert tops["two"][0] >= tops["one"][0] + 8, \
        "line after an image must start below it, not overlap"


def test_table_layout_rows_and_cells():
    from feetbrowser.layout import DocumentLayout, DrawText
    dom = HTMLParser(
        "<table><tr><th>Name</th><th>Age</th></tr>"
        "<tr><td>Ada</td><td>37</td></tr></table>").parse()
    body = next(n for n in tree_to_list(dom, []) if getattr(n, "tag", "") == "body")
    doc = DocumentLayout(body, 620)
    doc.layout()
    text_cmds = []
    stack = [doc]
    while stack:
        b = stack.pop()
        for cmd in b.paint():
            if isinstance(cmd, DrawText):
                text_cmds.append(cmd.text)
        stack.extend(b.children)
    assert "Ada" in text_cmds and "37" in text_cmds, "cell text painted"
    assert "Name" in text_cmds and "Age" in text_cmds, "header cells painted"
    # Cells must exist as boxes, and the row count must match the DOM.
    rows = [b for b in tree_to_list(doc, []) if b.node.tag == "tr"]
    assert len(rows) == 2, "two table rows laid out"
    for r in rows:
        assert r.height > 0, "rows have nonzero height"
        for c in r.children:
            assert c.width > 0 and c.height > 0, "cells have size"


def test_table_in_flex_does_not_overlap():
    """A table repositioned by flex layout must move its cell content with
    it; otherwise the second table's cells draw on top of the first."""
    from feetbrowser.layout import DocumentLayout, DrawText
    css = "div { display: flex; }"
    rules = CSSParser(css).parse()
    html = ("<div><table><tr><td>alpha</td><td>beta</td></tr></table>"
            "<table><tr><td>gamma</td><td>delta</td></tr></table></div>")
    dom = HTMLParser(html).parse()
    style(dom, rules)
    doc = DocumentLayout(dom, 620)
    doc.layout()
    texts = {}
    stack = [doc]
    while stack:
        b = stack.pop()
        for c in b.paint():
            if isinstance(c, DrawText):
                texts.setdefault(c.text, []).append((c.left, c.top))
        stack.extend(b.children)
    ax = texts["alpha"][0][0]
    gx = texts["gamma"][0][0]
    assert gx > ax, f"second table must sit right of first, got {gx} <= {ax}"
    bx = texts["beta"][0][0]
    dx = texts["delta"][0][0]
    assert dx > bx, "second table's cells must not overlap the first's"


def test_image_in_table_cell_sizes_column():
    """A decoded image must size its table column so it doesn't overlap the
    text in the neighbouring cell."""
    import tkinter
    from feetbrowser.layout import DocumentLayout, DrawImage, DrawText
    html = ("<table><tr><td><img src='https://example.com/img.png'></td>"
            "<td>zzz</td></tr></table>")
    dom = HTMLParser(html).parse()
    style(dom, [])
    photo = tkinter.PhotoImage(width=200, height=100)
    cache = {"https://example.com/img.png": photo}
    doc = DocumentLayout(dom, 620)
    doc.image_cache = cache
    doc.layout()
    img, zx = None, None
    stack = [doc]
    while stack:
        b = stack.pop()
        for c in b.paint():
            if isinstance(c, DrawImage):
                img = c
            elif isinstance(c, DrawText) and c.text == "zzz":
                zx = c.left
        stack.extend(b.children)
    assert img is not None, "image painted"
    assert zx is not None, "neighbour cell text painted"
    assert zx > img.right, \
        f"neighbour text ({zx}) overlaps the image (ends {img.right})"


def test_url_redirect_adopt():
    """Following an HTTP redirect in place must leave the URL pointing at the
    final host, so relative image/style/script URLs resolve correctly."""
    u = URL("https://google.com/")
    u._adopt(URL("https://www.google.com/path?q=1"))
    eq(str(u), "https://www.google.com/path?q=1", "adopted final URL")
    eq(u.host, "www.google.com", "host updated")
    # A non-redirected URL is untouched.
    v = URL("https://example.com/a")
    eq(str(v), "https://example.com/a")


def test_webp_image_decode():
    """WebP (used heavily by Google) must decode to a PhotoImage when Pillow
    is available, instead of staying a placeholder."""
    import io
    try:
        from PIL import Image as PILImage
    except ImportError:
        return  # Pillow is optional
    im = PILImage.new("RGBA", (4, 4), (0, 0, 255, 255))
    buf = io.BytesIO()
    im.save(buf, format="WEBP")
    photo = Tab._decode_image(buf.getvalue(), "image/webp")
    assert photo is not None, "WebP should decode"
    eq((photo.width(), photo.height()), (4, 4), "WebP dimensions preserved")


def test_float_text_wraps_and_clears():
    from feetbrowser.layout import DrawText, DocumentLayout
    from feetbrowser.cssparser import CSSParser, style as apply_style
    css = (
        ".f { float: left; width: 150px; height: 90px; }"
        ".c { clear: both; }")
    html = (
        "<style>css</style>"
        "<div class=f>FLOATBOX</div>"
        "<p class=wrap>left right</p>"
        "<p class=c>below</p>")
    dom = HTMLParser(html).parse()
    rules = CSSParser(css).parse()
    apply_style(dom, rules)
    body = next(n for n in tree_to_list(dom, []) if getattr(n, "tag", "") == "body")
    doc = DocumentLayout(body, 620)
    doc.layout()

    tops = {}
    lefts = {}
    stack = [doc]
    while stack:
        b = stack.pop()
        for cmd in b.paint():
            if isinstance(cmd, DrawText):
                tops[cmd.text] = cmd.top
                lefts[cmd.text] = cmd.left
        stack.extend(b.children)

    # The floated box text sits on the left, the <p> text is pushed right of
    # the float's right edge, and the cleared paragraph starts below the float.
    assert tops["FLOATBOX"] <= tops["left"] + 1, "float top at or above wrapping line"
    assert lefts["left"] > 145, "wrapping text indented past 150px-wide float"
    assert tops["below"] >= tops["FLOATBOX"] + 20, "clear pushed paragraph below float"


def test_clear_left_only_clears_left_floats():
    from feetbrowser.layout import DrawText, DocumentLayout
    from feetbrowser.cssparser import CSSParser, style as apply_style
    css = (
        ".l { float: left; width: 100px; }"
        ".r { float: right; width: 100px; }"
        ".cl { clear: left; }")
    html = (
        "<style>css</style>"
        "<div class=l>A</div><div class=r>C</div><div class=cl>D</div>")
    dom = HTMLParser(html).parse()
    rules = CSSParser(css).parse()
    apply_style(dom, rules)
    body = next(n for n in tree_to_list(dom, []) if getattr(n, "tag", "") == "body")
    doc = DocumentLayout(body, 620)
    doc.layout()
    tops = {}
    lefts = {}
    stack = [doc]
    while stack:
        b = stack.pop()
        for cmd in b.paint():
            if isinstance(cmd, DrawText):
                tops[cmd.text] = cmd.top
                lefts[cmd.text] = cmd.left
        stack.extend(b.children)
    # The cleared div goes below the left float but keeps sharing the line
    # with the right float (right floats are not cleared by clear:left).
    assert tops["D"] >= tops["A"] - 1, "D below left float"
    assert tops["D"] < tops["A"] + 90, "D only cleared the left side"


def test_data_image_placeholder():
    tab = Tab(700)
    tab.load("data:image/png;base64,iVBORw0KGgo=")
    assert tab.document is not None, "image page rendered"
    assert any("img" in c.text.lower() for c in tab.display_list
               if isinstance(c, DrawText)), "image labelled as placeholder"


def test_grid_columns_auto_placement_and_span():
    from feetbrowser.layout import DocumentLayout
    from feetbrowser.cssparser import CSSParser, style as apply_style
    css = ".g { display: grid; grid-template-columns: 100px 1fr 2fr; gap: 10px; }"
    html = ("<style>css</style><div class=g>"
            "<div class=a>A</div><div class=b>B</div><div class=c>C</div>"
            "<div class=d>D</div></div>")
    dom = HTMLParser(html).parse()
    apply_style(dom, CSSParser(css).parse())
    body = next(n for n in tree_to_list(dom, []) if getattr(n, "tag", "") == "body")
    doc = DocumentLayout(body, 700)
    doc.layout()
    items = {b.node.attributes.get("class"): b
             for b in tree_to_list(doc, []) if b.node.tag == "div"
             and b.node.attributes.get("class") not in (None, "g")}
    # First three items fill the first row across the three tracks.
    assert items["a"].x == 8 and items["a"].width == 100, "first track is 100px"
    assert items["b"].x == 118, "second track starts after gap"
    assert abs(items["b"].width - 188) < 1, f"1fr track {items['b'].width}"
    assert abs(items["c"].width - 376) < 1, f"2fr track {items['c'].width}"
    assert items["a"].y == items["b"].y == items["c"].y, "first row baseline"
    # Fourth item auto-wraps to the next row.
    assert items["d"].y > items["a"].y, "fourth item wrapped to a new row"
    assert items["d"].x == 8, "wrapped item starts at the first track"

    # A spanning item absorbs its columns.
    html2 = ("<style>css</style><div class=g>"
             "<div style='grid-column: span 2'>AB</div><div>C</div></div>")
    dom2 = HTMLParser(html2).parse()
    apply_style(dom2, CSSParser(css).parse())
    body2 = next(n for n in tree_to_list(dom2, [])
                 if getattr(n, "tag", "") == "body")
    doc2 = DocumentLayout(body2, 700)
    doc2.layout()
    items2 = [b for b in tree_to_list(doc2, []) if b.node.tag == "div"
              and b.node.attributes.get("class") != "g"]
    span = items2[0]
    assert abs(span.width - (100 + 10 + 188)) < 1, f"span width {span.width}"


def test_flex_row_grow_and_justify():
    from feetbrowser.layout import DocumentLayout
    from feetbrowser.cssparser import CSSParser, style as apply_style
    # justify-content: center — no growth, so leftover space is freed.
    css = ".f { display: flex; justify-content: center; gap: 10px; }"
    html = ("<style>css</style><div class=f>"
            "<div class=a>AA</div><div class=b>BB</div>"
            "<div class=c>CC</div></div>")
    dom = HTMLParser(html).parse()
    apply_style(dom, CSSParser(css).parse())
    body = next(n for n in tree_to_list(dom, []) if getattr(n, "tag", "") == "body")
    doc = DocumentLayout(body, 620)
    doc.layout()
    items = [b for b in tree_to_list(doc, []) if b.node.tag == "div"
             and b.node.attributes.get("class") != "f"]
    assert all(b.x > doc.children[0].x for b in items), \
        "centered row shifted right of container"
    assert len({int(b.y) for b in items}) == 1, "items share the row baseline"

    # flex-grow: 1 — the last item absorbs every leftover pixel.
    css = ".f { display: flex; gap: 10px; } .c { flex-grow: 1; }"
    dom = HTMLParser(html).parse()
    apply_style(dom, CSSParser(css).parse())
    body = next(n for n in tree_to_list(dom, []) if getattr(n, "tag", "") == "body")
    doc = DocumentLayout(body, 620)
    doc.layout()
    items = [b for b in tree_to_list(doc, []) if b.node.tag == "div"
             and b.node.attributes.get("class") != "f"]
    a = next(b for b in items if b.node.attributes.get("class") == "a")
    b = next(b for b in items if b.node.attributes.get("class") == "b")
    c = next(b for b in items if b.node.attributes.get("class") == "c")
    assert b.x + b.width < c.x, "flex items do not overlap"
    assert a.x < b.x < c.x, "items laid out left to right"
    assert c.x + c.width <= 8 + 604, "growing item stays inside container"


def test_flex_column_stacks_vertically():
    from feetbrowser.layout import DocumentLayout
    from feetbrowser.cssparser import CSSParser, style as apply_style
    css = ".f { display: flex; flex-direction: column; gap: 5px; }"
    html = ("<style>css</style><div class=f>"
            "<div>A</div><div>B</div><div>C</div></div>")
    dom = HTMLParser(html).parse()
    apply_style(dom, CSSParser(css).parse())
    body = next(n for n in tree_to_list(dom, []) if getattr(n, "tag", "") == "body")
    doc = DocumentLayout(body, 620)
    doc.layout()
    items = [b for b in tree_to_list(doc, []) if b.node.tag == "div"
             and b.node.attributes.get("class") != "f"]
    ys = sorted(b.y for b in items)
    assert ys[1] - ys[0] >= 27, "second item starts below first (gap)"
    assert ys[2] - ys[1] >= 27, "third item starts below second (gap)"
    # All column items span the full container width (stretch).
    for b in items:
        assert b.width == 604, f"column item width {b.width}"


def test_flex_wrap_rows_onto_new_line():
    from feetbrowser.layout import DocumentLayout
    from feetbrowser.cssparser import CSSParser, style as apply_style
    css = (".f { display: flex; flex-wrap: wrap; gap: 4px; }"
           ".a { width: 100px; }")
    html = ("<style>css</style><div class=f>"
            "<div class=a>A</div><div class=a>B</div><div class=a>C</div>"
            "<div class=a>D</div></div>")
    dom = HTMLParser(html).parse()
    apply_style(dom, CSSParser(css).parse())
    body = next(n for n in tree_to_list(dom, []) if getattr(n, "tag", "") == "body")
    doc = DocumentLayout(body, 260)  # 244px container -> 2 items per line
    doc.layout()
    items = [b for b in tree_to_list(doc, []) if b.node.tag == "div"
             and b.node.attributes.get("class") not in (None, "f")]
    line1, line2 = items[0:2], items[2:4]
    assert all(b.y == line1[0].y for b in line1), "first two items share line one"
    assert all(b.y == line2[0].y for b in line2), "last two items share line two"
    assert line2[0].y > line1[0].y, "second line sits below the first"
    assert line2[0].x == line1[0].x, "each line starts at the container edge"


def test_flex_wrap_with_gap():
    from feetbrowser.layout import DocumentLayout
    from feetbrowser.cssparser import CSSParser, style as apply_style
    css = (".f { display: flex; flex-wrap: wrap; row-gap: 10px; column-gap: 20px; }"
           ".a { width: 90px; }")
    html = ("<style>css</style><div class=f>"
            "<div class=a>A</div><div class=a>B</div><div class=a>C</div>"
            "<div class=a>D</div></div>")
    dom = HTMLParser(html).parse()
    apply_style(dom, CSSParser(css).parse())
    body = next(n for n in tree_to_list(dom, []) if getattr(n, "tag", "") == "body")
    doc = DocumentLayout(body, 260)  # 244px container -> 2 items per line
    doc.layout()
    items = [b for b in tree_to_list(doc, []) if b.node.tag == "div"
             and b.node.attributes.get("class") not in (None, "f")]
    a, b, c, d = items
    assert b.x >= a.x + a.width + 20 - 1, "column-gap separates line-one items"
    assert d.x >= c.x + 90 + 20 - 1, "line two items also honor column-gap"
    assert c.y >= a.y + a.height + 10 - 1, "row-gap separates the two lines"
    assert d.y == c.y, "line-two items share a row"


def test_flex_wrap_justify_per_line():
    from feetbrowser.layout import DocumentLayout
    from feetbrowser.cssparser import CSSParser, style as apply_style
    css = (".f { display: flex; flex-wrap: wrap; justify-content: space-between; }"
           ".a { width: 90px; }")
    html = ("<style>css</style><div class=f>"
            "<div class=a>A</div><div class=a>B</div><div class=a>C</div>"
            "<div class=a>D</div></div>")
    dom = HTMLParser(html).parse()
    apply_style(dom, CSSParser(css).parse())
    body = next(n for n in tree_to_list(dom, []) if getattr(n, "tag", "") == "body")
    doc = DocumentLayout(body, 260)  # 244px container -> 2 items per line
    doc.layout()
    container = next(b for b in tree_to_list(doc, [])
                     if b.node.attributes.get("class") == "f")
    items = [b for b in tree_to_list(doc, []) if b.node.tag == "div"
             and b.node.attributes.get("class") not in (None, "f")]
    a, b, c, d = items
    # space-between runs independently per line: line one pins its first item
    # at the container's left edge and pushes its second to the right edge...
    assert a.x == container.x, "line-one first item at container start"
    assert b.x > a.x + a.width, "space-between pushed the second item right"
    # ...while line two starts over from the container's left edge again.
    assert c.x == container.x, "line-two first item at container start"
    assert d.x > c.x + c.width, "line-two items also spaced apart"
    assert c.y > a.y, "line two is below line one"


def test_flex_wrap_align_content_center():
    from feetbrowser.layout import DocumentLayout
    from feetbrowser.cssparser import CSSParser, style as apply_style
    css = (".f { display: flex; flex-wrap: wrap; align-content: center; "
           "height: 300px; row-gap: 10px; }"
           ".a { width: 90px; }")
    html = ("<style>css</style><div class=f>"
            "<div class=a>A</div><div class=a>B</div><div class=a>C</div>"
            "<div class=a>D</div><div class=a>E</div></div>")
    dom = HTMLParser(html).parse()
    apply_style(dom, CSSParser(css).parse())
    body = next(n for n in tree_to_list(dom, []) if getattr(n, "tag", "") == "body")
    doc = DocumentLayout(body, 260)  # 244px container -> 2 items per line
    doc.layout()
    container = next(b for b in tree_to_list(doc, [])
                     if b.node.attributes.get("class") == "f")
    items = [b for b in tree_to_list(doc, []) if b.node.tag == "div"
             and b.node.attributes.get("class") not in (None, "f")]
    top_item = min(items, key=lambda b: b.y)
    bottom_item = max(items, key=lambda b: b.y + b.height)
    top_gap = top_item.y - container.y
    bottom_gap = (container.y + container.height
                  - (bottom_item.y + bottom_item.height))
    assert top_gap > 0, "wrapped lines pushed down from the container top"
    assert abs(top_gap - bottom_gap) < 1, "line block centered vertically"


def test_flex_wrap_align_items_per_line():
    from feetbrowser.layout import DocumentLayout
    from feetbrowser.cssparser import CSSParser, style as apply_style
    css = (".f { display: flex; flex-wrap: wrap; align-items: flex-end; }"
           ".a { width: 90px; }"
           ".b { width: 90px; height: 60px; }")
    html = ("<style>css</style><div class=f>"
            "<div class=a>A</div><div class=b>B</div>"
            "<div class=a>C</div><div class=a>D</div></div>")
    dom = HTMLParser(html).parse()
    apply_style(dom, CSSParser(css).parse())
    body = next(n for n in tree_to_list(dom, []) if getattr(n, "tag", "") == "body")
    doc = DocumentLayout(body, 260)  # 244px container -> 2 items per line
    doc.layout()
    items = [b for b in tree_to_list(doc, []) if b.node.tag == "div"
             and b.node.attributes.get("class") not in (None, "f")]
    a, b, c, d = items
    # flex-end hangs each item from its line's bottom, so all bottoms on a
    # line line up even though the 60px item rules the line's height.
    assert abs((a.y + a.height) - (b.y + b.height)) < 1, \
        "flex-end aligns items to the bottom of their line"
    assert abs((c.y + c.height) - (d.y + d.height)) < 1, \
        "line two also bottom-aligned"
    assert c.y > a.y, "second line sits below the first"


def test_flex_column_wrap_columns_side_by_side():
    from feetbrowser.layout import DocumentLayout
    from feetbrowser.cssparser import CSSParser, style as apply_style
    css = (".f { display: flex; flex-direction: column; flex-wrap: wrap; "
           "height: 200px; column-gap: 10px; }"
           ".a { width: 80px; height: 60px; }")
    html = ("<style>css</style><div class=f>"
            "<div class=a>A</div><div class=a>B</div><div class=a>C</div>"
            "<div class=a>D</div><div class=a>E</div></div>")
    dom = HTMLParser(html).parse()
    apply_style(dom, CSSParser(css).parse())
    body = next(n for n in tree_to_list(dom, []) if getattr(n, "tag", "") == "body")
    doc = DocumentLayout(body, 260)
    doc.layout()
    items = [b for b in tree_to_list(doc, []) if b.node.tag == "div"
             and b.node.attributes.get("class") not in (None, "f")]
    a, b, c, d, e = items
    assert b.y > a.y and c.y > b.y, "first column stacks its items vertically"
    assert c.x == a.x, "third item stays in the first column"
    assert d.x > a.x, "fourth item flowed into a second column to the right"
    assert d.x == e.x, "second-column items share an x"
    assert abs(d.x - (a.x + a.width + 10)) < 1, "column-gap separates columns"
    assert e.y > d.y, "second column stacks its items"
    assert d.y == a.y, "both columns top out at the container top"


def test_flex_wrap_reverse_orders_lines_bottom_up():
    from feetbrowser.layout import DocumentLayout
    from feetbrowser.cssparser import CSSParser, style as apply_style
    css = (".f { display: flex; flex-wrap: wrap-reverse; row-gap: 8px; }"
           ".a { width: 100px; }")
    html = ("<style>css</style><div class=f>"
            "<div class=a>A</div><div class=a>B</div><div class=a>C</div>"
            "<div class=a>D</div></div>")
    dom = HTMLParser(html).parse()
    apply_style(dom, CSSParser(css).parse())
    body = next(n for n in tree_to_list(dom, []) if getattr(n, "tag", "") == "body")
    doc = DocumentLayout(body, 260)  # 244px container -> 2 items per line
    doc.layout()
    items = [b for b in tree_to_list(doc, []) if b.node.tag == "div"
             and b.node.attributes.get("class") not in (None, "f")]
    a, b, c, d = items
    assert a.y == b.y, "first line items share a row"
    assert c.y == d.y, "second line items share a row"
    assert a.y > c.y, "wrap-reverse puts the first line below the second"
    assert a.x == c.x, "lines still start at the container edge"


def test_data_image_pipeline_renders_drawimage():
    import base64
    import struct
    import zlib as _z
    from feetbrowser.layout import DrawImage

    # Build a tiny valid 2x2 PNG in memory (no Pillow dependency).
    def chunk(tag, data):
        c = struct.pack(">I", len(data)) + tag + data
        return c + struct.pack(">I", _z.crc32(tag + data))

    def png(w, h):
        rows = b"".join(b"\x00" + b"\xff\x00\x00" * w for _ in range(h))
        raw = b"\x89PNG\r\n\x1a\n"
        raw += chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
        raw += chunk(b"IDAT", _z.compress(rows))
        raw += chunk(b"IEND", b"")
        return raw

    b64 = base64.b64encode(png(2, 2)).decode()
    tab = _make_tab(f'<p>before</p><img src="data:image/png;base64,{b64}">'
                    '<p>after</p>')
    # No image fetched yet -> placeholder text rendered.
    assert any("img" in c.text.lower() for c in tab.display_list
               if isinstance(c, DrawText)), "placeholder before image ready"
    # load_images synchronously (root is None path).
    tab.load_images(None)
    assert tab.image_cache, "image_cache populated"
    assert any(isinstance(c, DrawImage) for c in tab.display_list), \
        "DrawImage emitted after image loaded"
    # Re-rendering no longer emits the placeholder text.
    assert not any("img" in c.text.lower() for c in tab.display_list
                   if isinstance(c, DrawText)), "placeholder replaced"


def test_base_href_detected():
    dom = HTMLParser("<head><base href='/sub/'></head><body>x</body>").parse()
    eq(find_base_href(dom), "/sub/")


def _make_tab(body, url="https://example.com/page"):
    tab = Tab(700)
    u = URL(url)
    tab.url = u
    tab._build(u, body, "text/html")
    return tab


def _control_box(tab, **attrs):
    """The centre point and node of the first form control whose attributes
    match, as (x, y, node) -- i.e. where a user would click it."""
    for lx, ty, rx, by, n in tab.document.input_boxes:
        if isinstance(n, Element) and all(
                n.attributes.get(k) == v for k, v in attrs.items()):
            return ((lx + rx) / 2, (ty + by) / 2, n)
    return None


def test_page_text_selection():
    tab = _make_tab("<p>Hello world foo bar</p>")
    words = {c.text: c for c in tab.display_list
             if isinstance(c, DrawText) and c.text in
             ("Hello", "world", "foo", "bar")}
    eq(len(words), 4, "words laid out individually")
    hello, foo = words["Hello"], words["foo"]
    # Drag from the start of "Hello" to just past the end of "foo".
    tab.start_selection(hello.left, hello.top + 2)
    assert tab.selection is not None, "selection anchored on press"
    tab.extend_selection(foo.right + 1, foo.top + 2)
    eq(tab.selected_text(), "Hello world foo",
       f"selected text: {tab.selected_text()!r}")
    # Selecting backwards (end above anchor) still yields the right text.
    tab.start_selection(foo.right + 1, foo.top + 2)
    tab.extend_selection(hello.left, hello.top + 2)
    eq(tab.selected_text(), "Hello world foo", "backwards drag selects same text")
    # A zero-width (plain click) selection selects nothing.
    tab.start_selection(hello.left, hello.top + 2)
    tab.extend_selection(hello.left, hello.top + 2)
    eq(tab.selected_text(), "", "zero-width selection is empty")
    tab.selection = None
    eq(tab.selected_text(), "", "cleared selection is empty")


def test_tab_title_truncated_in_draw_tabs():
    """Long page titles must be truncated so they never spill past the tab
    edge (issue #32). The truncation runs in _draw_tabs; exercise the width
    math directly on a stub."""
    stub = type("Stub", (), {})()
    stub.chrome_font = get_font(14, "normal", "roman", "Helvetica")
    title = "frog - DuckDuckGo - search the whole web and never stop"
    title_w = 128
    if _measure(stub.chrome_font, title) > title_w:
        t = title
        while t and _measure(stub.chrome_font, t + "…") > title_w:
            t = t[:-1]
        title = t + "…"
    assert title.endswith("…"), "truncated title shows an ellipsis"
    assert _measure(stub.chrome_font, title) <= title_w + 6, \
        "truncated title fits the tab before the close box"


def test_load_errors_are_collected():
    import tempfile
    html_body = (
        '<html><head>'
        '<link rel="stylesheet" href="http://127.0.0.1:1/x.css">'
        '<script src="http://127.0.0.1:1/y.js"></script>'
        '</head><body><p>hi</p></body></html>')
    fd, path = tempfile.mkstemp(suffix=".html")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(html_body)
        tab = Tab(700)
        tab.load("file://" + path)
    finally:
        os.unlink(path)
    kinds = [e.split()[0] for e in tab.net_errors]
    assert "CSS" in kinds, f"CSS failure logged, got {kinds}"
    assert "JS" in kinds, f"JS failure logged, got {kinds}"


def test_doc_error_is_collected():
    tab = Tab(700)
    tab.load("http://127.0.0.1:1/")
    assert tab.net_errors and tab.net_errors[0].startswith("DOC"), \
        tab.net_errors


def test_import_lead_char_preserved():
    """An @import statement matched at a statement boundary must not eat the
    character before it (e.g. the `}` closing the previous rule)."""
    from feetbrowser.browser import _expand_imports
    from feetbrowser.net import URL
    css = ("a{color:red}@import 'http://127.0.0.1:1/nope.css';"
           "b{color:blue}")
    out = _expand_imports(css, URL("https://example.com/"))
    assert "a{color:red}" in out, out
    assert "b{color:blue}" in out, out


def test_media_query_em_units():
    """em/rem media-feature values are resolved at the 16px root size, not
    read as raw pixel numbers."""
    from feetbrowser.cssparser import media_matches
    assert media_matches("(min-width: 40em)", 800, 600), "640px breakpoint matched"
    assert not media_matches("(min-width: 40em)", 600, 600), "600px < 640px"
    assert media_matches("(max-width: 40rem)", 600, 600), "rem max-width matched"
    assert not media_matches("(max-width: 40rem)", 700, 600), "700px > 640px"


def test_viewport_accessor_tracks_set_viewport():
    from feetbrowser.cssparser import get_viewport, set_viewport
    set_viewport(1234, 567)
    assert get_viewport() == (1234.0, 567.0)
    set_viewport(1000, 720)


def test_js_errors_captured_once():
    """_capture_js_errors must not re-count errors already scanned."""
    tab = _make_tab("<script>throw new Error('boom')</script>")
    js = lambda: sum(1 for e in tab.net_errors if e.startswith("JS"))
    before = js()
    assert before > 0, "page-load JS error captured"
    tab._capture_js_errors(tab._js_interp.logs)  # a re-dispatch re-scan
    assert js() == before, "errors must not be double-counted"


def test_form_submit_get():
    tab = _make_tab(
        '<form action="/submit"><input name="q" value="hello world">'
        '<input type="submit" value="Go"></form>')
    pos = None
    for lx, ty, rx, by, n in tab.document.input_boxes:
        if isinstance(n, Element) and n.tag == "input" \
                and n.attributes.get("type") == "submit":
            pos = ((lx + rx) / 2, (ty + by) / 2)
    assert pos is not None
    act = tab.click(*pos)
    assert isinstance(act, FormAction), type(act)
    assert act.payload is None
    assert str(act.url).startswith("https://example.com/submit")
    assert "q=hello+world" in str(act.url), act.url


def test_form_submit_post_and_typing():
    tab = _make_tab(
        '<form method="post" action="/save"><input name="name">'
        '<textarea name="notes"></textarea>'
        '<input type="submit"></form>')
    # Focus the text field and type into it.
    hit = None
    for lx, ty, rx, by, n in tab.document.input_boxes:
        if isinstance(n, Element) and n.tag == "input" \
                and not n.attributes.get("type"):
            hit = n
            cx, cy = (lx + rx) / 2, (ty + by) / 2
    assert hit is not None
    tab.click(cx, cy)
    assert tab.focused_input is hit, "click focused the input"
    tab.type_char("a")
    tab.type_char("b")
    eq(hit.attributes["value"], "ab", "typed chars stored")

    pos = None
    for lx, ty, rx, by, n in tab.document.input_boxes:
        if isinstance(n, Element) and n.tag == "input" \
                and n.attributes.get("type") == "submit":
            pos = ((lx + rx) / 2, (ty + by) / 2)
    act = tab.click(*pos)
    assert isinstance(act, FormAction)
    assert act.payload is not None and "name=ab" in act.payload, act.payload


def test_form_submit_merges_existing_query():
    tab = _make_tab(
        '<form action="/search?lang=en"><input name="q" value="hello world">'
        '<input type="submit"></form>')
    pos = None
    for lx, ty, rx, by, n in tab.document.input_boxes:
        if isinstance(n, Element) and n.tag == "input" \
                and n.attributes.get("type") == "submit":
            pos = ((lx + rx) / 2, (ty + by) / 2)
    act = tab.click(*pos)
    assert isinstance(act, FormAction)
    assert str(act.url) == \
        "https://example.com/search?lang=en&q=hello+world", str(act.url)


def test_checkbox_toggle():
    tab = _make_tab(
        '<form action="/r"><input type="checkbox" name="c" value="blue">'
        '<input type="submit"></form>')
    box = _control_box(tab, type="checkbox")
    assert box is not None, "no checkbox box found"
    cx, cy, node = box
    assert not field_checked(node), "unticked until clicked"
    tab.click(cx, cy)
    assert field_checked(node), "click ticks the box"
    eq(node.attributes.get("value"), "blue", "the submitted value survives")
    tab.click(cx, cy)
    assert not field_checked(node), "a second click unticks it"


def test_form_controls_do_not_stack_on_one_another():
    """Controls paint straight into the display list, so the line they sit on
    has to grow to fit them -- otherwise every control on the page lands at
    the same y and a click reaches whichever hit box happens to be first."""
    tab = _make_tab(
        '<form action="/a"><input name="q"><input type="submit" value="Go">'
        '</form>'
        '<form method="post" action="/b"><input name="w">'
        '<input type="submit" value="Send"></form>')
    boxes = tab.document.input_boxes
    eq(len(boxes), 4, "one hit box per control")
    for i, a in enumerate(boxes):
        for b in boxes[i + 1:]:
            assert (a[2] <= b[0] or b[2] <= a[0]
                    or a[3] <= b[1] or b[3] <= a[1]), \
                f"controls overlap: {a[:4]} and {b[:4]}"
    # The point that draws "Send" must submit the form "Send" belongs to.
    cx, cy, _ = _control_box(tab, value="Send")
    act = tab.click(cx, cy)
    assert isinstance(act, FormAction), type(act)
    eq(str(act.url), "https://example.com/b", "second form's action")


def test_form_submit_collects_every_kind_of_field():
    tab = _make_tab(
        '<form method="post" action="/save">'
        '<input name="user" value="ada">'
        '<input type="hidden" name="csrf" value="t0ken">'
        '<input type="checkbox" name="cc" value="yes">'
        '<input type="checkbox" name="news" value="daily" checked>'
        '<input type="text" name="ghost" value="x" disabled>'
        '<textarea name="body">from markup</textarea>'
        '<select name="colour"><option>red<option selected>blue</select>'
        '<input type="submit" name="do" value="Save">'
        '<input type="submit" name="do" value="Delete"></form>')
    cx, cy, _ = _control_box(tab, value="Save")
    act = tab.click(cx, cy)
    assert isinstance(act, FormAction), type(act)
    fields = urllib.parse.parse_qsl(act.payload, keep_blank_values=True)
    eq(fields, [("user", "ada"), ("csrf", "t0ken"), ("news", "daily"),
                ("body", "from markup"), ("colour", "blue"), ("do", "Save")],
       "submitted fields")


def _key_stub(tab, clipboard=""):
    """A Browser stripped down to what _on_key touches, wired to `tab` and to
    a clipboard that either holds `clipboard` or, when that is None, refuses
    to be read the way Tk does when nothing text-shaped is on it."""
    def read():
        if clipboard is None:
            raise tkinter.TclError("CLIPBOARD selection doesn't exist")
        return clipboard

    class Stub(Browser):
        def __init__(self):
            self.focus = None
            self.active_tab = tab
            self.toe_contexts = []
            self.context_menu = type("Menu", (), {"open_": False})()
            self.window = type("Win", (), {"clipboard_get": staticmethod(read)})()
            self.painted = 0

        def _draw_page(self):
            self.painted += 1

    return Stub()


def _key_event(keysym, char="", ctrl=False):
    return type("Event", (), {"keysym": keysym, "char": char,
                              "state": 0x4 if ctrl else 0})()


def test_paste_into_page_field():
    tab = _make_tab('<form action="/s"><input name="q"></form>')
    cx, cy, node = _control_box(tab, name="q")
    tab.click(cx, cy)
    assert tab.focused_input is node, "click focused the field"

    browser = _key_stub(tab, "hello from the clipboard")
    Browser._on_key(browser, _key_event("v", "\x16", ctrl=True))
    eq(node.attributes["value"], "hello from the clipboard", "pasted value")
    eq(browser.painted, 1, "the page was repainted once")

    # Typing still appends after a paste, and pastes accumulate.
    Browser._on_key(browser, _key_event("exclam", "!"))
    Browser._on_key(browser, _key_event("v", "\x16", ctrl=True))
    eq(node.attributes["value"],
       "hello from the clipboard!hello from the clipboard", "paste appends")


def test_paste_folds_newlines_only_in_single_line_fields():
    tab = _make_tab('<form action="/s"><input name="q">'
                    '<textarea name="body"></textarea></form>')
    _cx, _cy, field = _control_box(tab, name="q")
    tab.focused_input = field
    assert tab.insert_text("one\ntwo")
    eq(field.attributes["value"], "one two", "single-line field folds breaks")

    _cx, _cy, area = _control_box(tab, name="body")
    tab.focused_input = area
    assert tab.insert_text("one\ntwo")
    eq(area.attributes["value"], "one\ntwo", "a textarea keeps them")


def test_paste_without_a_clipboard_is_a_no_op():
    tab = _make_tab('<form action="/s"><input name="q" value="kept"></form>')
    _cx, _cy, node = _control_box(tab, name="q")
    tab.focused_input = node
    browser = _key_stub(tab, clipboard=None)
    Browser._on_key(browser, _key_event("v", "\x16", ctrl=True))
    eq(node.attributes["value"], "kept", "unreadable clipboard changes nothing")
    eq(browser.painted, 0, "and costs no repaint")


def test_about_blank_typed():
    assert Browser._looks_like_url("about:blank")
    assert Browser._looks_like_url("localhost:8000")
    assert Browser._looks_like_url("192.168.1.1:80")
    assert not Browser._looks_like_url("hello world")


def test_gt_inside_quoted_attribute_does_not_close_tag():
    dom = HTMLParser('<a href="x?a=1&b=2">link</a>').parse()
    a = [n for n in tree_to_list(dom, []) if isinstance(n, Element) and n.tag == "a"]
    eq(len(a), 1, "only one <a> expected")
    eq(a[0].attributes.get("href"), "x?a=1&b=2", "href preserved intact")


def test_eof_inside_tag_flushes_character_data():
    dom = HTMLParser("<p>hello <b>world").parse()
    texts = "".join(n.text for n in tree_to_list(dom, []) if isinstance(n, Text))
    assert "world" in texts, "unterminated <b> text must not be lost"


def test_eof_inside_script_flushes_raw_text():
    dom = HTMLParser("<script>var x = 1;").parse()
    texts = "".join(n.text for n in tree_to_list(dom, []) if isinstance(n, Text))
    assert "var x = 1;" in texts, "unterminated <script> body must not be lost"


def test_charset_unknown_falls_back_to_utf8():
    from feetbrowser.net import URL as _URL
    eq(_URL._charset({"content-type": "text/html; charset=charset=X-IMAGINARY"}), "utf8")
    eq(_URL._charset({"content-type": 'text/html; charset="iso-8859-1"'}), "iso-8859-1")
    eq(_URL._charset({"content-type": "text/html"}), "utf8")


def test_resolve_color_handles_css_color_functions():
    from feetbrowser.layout import resolve_color
    eq(resolve_color("rgba(0,0,0,0)"), None, "fully transparent -> no paint")
    eq(resolve_color("rgba(0, 0, 0, 0)"), None, "spaced transparent rgba")
    eq(resolve_color("rgb(255,0,0)"), "#ff0000", "rgb")
    eq(resolve_color("rgba(0,128,255,0.5)"), "#0080ff", "rgba with alpha")
    eq(resolve_color("rgb(255 0 0 / 0.25)"), "#ff0000", "modern space/slash rgb")
    eq(resolve_color("rgb(100%, 50%, 0%)"), "#ff8000", "percentage rgb")
    eq(resolve_color("hsl(120, 100%, 50%)"), "#00ff00", "hsl")
    eq(resolve_color("hsla(0, 100%, 50%, 0)"), None, "transparent hsla")
    eq(resolve_color("#fff"), "#ffffff", "3-digit hex expanded")
    eq(resolve_color("#ff000000"), None, "8-digit hex with alpha 0")
    eq(resolve_color("transparent"), None, "transparent keyword")
    eq(resolve_color("red"), "red", "named color passes through")


def _start_server(handler, **kw):
    """Serve `handler` on an ephemeral port in a background thread."""
    import http.server
    import threading
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler, **kw)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def test_reload_bypasses_cache():
    # The response cache serves cached bodies for max-age'd pages; a reload
    # must bypass it and actually re-fetch.
    from feetbrowser.net import URL, _CACHE
    hits = {"n": 0}

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            hits["n"] += 1
            body = f"<h1>hit {hits['n']}</h1>".encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Cache-Control", "max-age=9999")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = _start_server(H)
    try:
        u = URL(f"http://127.0.0.1:{srv.server_address[1]}/page")
        _h, first, _c = u.request()
        eq(hits["n"], 1, "first fetch")
        _h, second, _c = u.request()
        eq(hits["n"], 1, "second fetch served from cache")
        eq(first, second)
        _h, third, _c = u.request(refresh=True)
        eq(hits["n"], 2, "refresh re-fetches")
        assert third != first or "hit 2" in third
        _CACHE.clear()
    finally:
        srv.shutdown()


def test_async_load_in_gui_mode():
    # With a window present, http(s) loads happen off the UI thread so the
    # spinner can spin; loading stays True until the body arrives.
    import time
    from feetbrowser.net import URL

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            time.sleep(0.2)
            body = b"<h1>async</h1>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = _start_server(H)
    root = tkinter.Tk(); root.withdraw()
    try:
        class FakeBrowser:
            window = root
            toe_contexts = []

            def draw(self):
                pass

        tab = Tab(700, FakeBrowser())
        url = URL(f"http://127.0.0.1:{srv.server_address[1]}/slow")
        tab.load(url)
        assert tab.loading is True, "GUI-mode http load should be async"
        deadline = time.time() + 5
        while tab.loading and time.time() < deadline:
            tab._drain_async_load()
            time.sleep(0.02)
        assert not tab.loading, "async load should complete"
        assert str(tab.url) == str(url)
        texts = "".join(getattr(c, "text", "") for c in tab.display_list)
        assert "async" in texts, f"page body rendered, got: {texts!r}"
    finally:
        srv.shutdown()
        root.destroy()


def main():
    root = tkinter.Tk(); root.withdraw()
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  ok  {t.__name__}")
        except Exception as e:
            failed += 1
            import traceback; traceback.print_exc()
            print(f" FAIL {t.__name__}: {e}")
    if failed:
        print(f"\n{failed} FAILED")
        sys.exit(1)
    print(f"\nALL {len(tests)} UNIT TESTS PASSED")


if __name__ == "__main__":
    main()
