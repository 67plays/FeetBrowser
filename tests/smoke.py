"""Headless smoke test: exercise the whole pipeline without the GUI loop."""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from feetbrowser import gui

from feetbrowser.net import URL
from feetbrowser.htmlparser import HTMLParser
from feetbrowser.cssparser import CSSParser, style
from feetbrowser.layout import DocumentLayout, paint_tree
from feetbrowser.browser import (DEFAULT_STYLE_SHEET, inline_styles,
                                  find_links, get_title, tree_to_list)


def run(url_str):
    print(f"\n=== {url_str} ===")
    url = URL(url_str)
    headers, body, ctype = url.request()
    print(f"content-type: {ctype}, body {len(body)} bytes")
    if url.view_source or ctype.startswith("text/plain"):
        esc = body.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        body = f"<pre>{esc}</pre>"
    nodes = HTMLParser(body).parse()
    print("title:", get_title(nodes))
    node_count = len(tree_to_list(nodes, []))
    print("DOM nodes:", node_count)

    rules = list(DEFAULT_STYLE_SHEET)
    for sheet in inline_styles(nodes, []):
        rules.extend(CSSParser(sheet).parse())
    links = find_links(nodes, [])
    print("linked stylesheets:", len(links))
    for href in links[:3]:
        try:
            _h, css, _c = url.resolve(href).request()
            rules.extend(CSSParser(css).parse())
        except Exception as e:
            print("  (skip)", href, e)
    print("total CSS rules:", len(rules))

    style(nodes, rules)
    doc = DocumentLayout(nodes, 1000)
    doc.layout()
    dl = []
    paint_tree(doc, dl)
    print("content height:", round(doc.height), "px, display commands:", len(dl))
    kinds = {}
    for cmd in dl:
        kinds[type(cmd).__name__] = kinds.get(type(cmd).__name__, 0) + 1
    print("commands by kind:", kinds)
    # sample some text
    texts = [c.text for c in dl if type(c).__name__ == "DrawText"][:12]
    print("sample text:", " ".join(texts))


if __name__ == "__main__":
    root = gui.Tk()
    root.withdraw()
    targets = sys.argv[1:] or [
        "data:text/html,<h1>Hi</h1><p>Hello <b>bold</b> and <i>italic</i> and "
        "<a href=/x>a link</a>.</p><ul><li>one</li><li>two</li></ul>",
        "https://example.com",
        "https://info.cern.ch/hypertext/WWW/TheProject.html",
    ]
    failed = 0
    for t in targets:
        try:
            run(t)
        except Exception as e:
            failed += 1
            import traceback
            traceback.print_exc()
            print("FAILED:", t, e)
    # It printed the traceback and then exited 0, so every caller -- test.sh
    # included -- read a page that would not load as a pass.
    if failed:
        print(f"\n{failed} of {len(targets)} FAILED")
        sys.exit(1)
    print("\nOK")
