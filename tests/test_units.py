"""Fast, offline unit tests for URL parsing, HTML, CSS, and internal pages."""
import sys, os, tkinter
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from feetbrowser.net import URL
from feetbrowser.htmlparser import HTMLParser, Element, Text
from feetbrowser.cssparser import CSSParser, style
from feetbrowser.browser import (
    Tab, Browser, _AboutURL, _BookmarksURL, _HistoryURL,
    bookmarks_html, history_html, tree_to_list
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
    hrefs = [n.attributes.get("href", "") for n in links]
    assert "https://example.org" in hrefs
    assert "https://news.ycombinator.com" in hrefs


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
