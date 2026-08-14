"""Headless test of click-to-navigate, history, and forms plumbing."""
import sys, os, threading, http.server
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from feetbrowser import gui

from feetbrowser.browser import Tab, FormAction
from feetbrowser.htmlparser import Element
from feetbrowser.layout import DrawText


FORM_PAGE = """<!doctype html><html><body>
<form method="get" action="/echo">
  <p><label>Search <input name="q"></label></p>
  <p><select name="colour"><option value="red">red
     <option value="blue" selected>blue</select></p>
  <p><input type="submit" value="Search"></p>
</form>
<form method="post" action="/echo">
  <p><label>Name <input name="who"></label></p>
  <p><textarea name="notes"></textarea></p>
  <p><input type="checkbox" name="ok" value="yes"></p>
  <p><input type="submit" value="Send"></p>
</form>
</body></html>"""


class _FormServer(http.server.BaseHTTPRequestHandler):
    """Serves a two-form page and echoes back whatever a submission sends."""

    def log_message(self, *args):
        pass

    def _reply(self, text):
        body = text.encode("utf8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/echo"):
            self._reply(f"<html><body><p>got {self.path}</p></body></html>")
        else:
            self._reply(FORM_PAGE)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode("utf8")
        self._reply("<html><body><p>posted to {} type {} body {}</p>"
                    "</body></html>".format(
                        self.path, self.headers.get("Content-Type"), body))


def find_link_point(tab, needle):
    """Find canvas coords of the DrawText whose text contains needle."""
    for cmd in tab.display_list:
        if isinstance(cmd, DrawText) and needle.lower() in cmd.text.lower():
            return cmd.left + 3, cmd.top + 3
    return None


def control_point(tab, **attrs):
    """Where a user would click the first control matching `attrs`, plus the
    node itself."""
    for lx, ty, rx, by, node in tab.document.input_boxes:
        if isinstance(node, Element) and all(
                node.attributes.get(k) == v for k, v in attrs.items()):
            return (lx + rx) / 2, (ty + by) / 2, node
    raise AssertionError(f"no control matching {attrs}")


def page_text(tab):
    return " ".join(c.text for c in tab.display_list
                    if isinstance(c, DrawText) and c.text)


def test_form_round_trip():
    """Fill in and submit both forms against a real server, over a real
    socket, and check what arrived on the other side."""
    server = http.server.HTTPServer(("127.0.0.1", 0), _FormServer)
    base = f"http://127.0.0.1:{server.server_address[1]}/"
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        # GET: the query is built from the fields and appended to the action.
        tab = Tab(700)
        tab.load(base)
        x, y, field = control_point(tab, name="q")
        tab.click(x, y)
        assert tab.focused_input is field, "clicking a field focuses it"
        assert tab.insert_text("feet & toes"), "paste lands in the field"
        x, y, _ = control_point(tab, value="Search")
        action = tab.click(x, y)
        assert isinstance(action, FormAction), action
        assert action.payload is None, "a GET carries no body"
        tab.load(action.url, payload=action.payload)
        text = page_text(tab)
        assert "/echo?q=feet+%26+toes&colour=blue" in text, text
        print("GET form round trip:", text)

        # POST: the same fields travel as a urlencoded body instead.
        tab.load(base)
        x, y, _ = control_point(tab, name="who")
        tab.click(x, y)
        tab.insert_text("ada")
        x, y, _ = control_point(tab, name="notes")
        tab.click(x, y)
        tab.insert_text("two\nlines")
        x, y, _ = control_point(tab, name="ok")
        tab.click(x, y)
        x, y, _ = control_point(tab, value="Send")
        action = tab.click(x, y)
        assert isinstance(action, FormAction), action
        assert action.payload == "who=ada&notes=two%0Alines&ok=yes", \
            action.payload
        tab.load(action.url, payload=action.payload)
        text = page_text(tab)
        assert "posted to /echo" in text, text
        assert "type application/x-www-form-urlencoded" in text, text
        # The echoed body reads back a word at a time: "&notes" is an HTML
        # character reference once it is rendered, so check the pieces.
        for part in ("who=ada", "es=two%0Alines", "ok=yes"):
            assert part in text, (part, text)
        print("POST form round trip:", text)
    finally:
        server.shutdown()
        server.server_close()


def main():
    root = gui.Tk(); root.withdraw()
    tab = Tab(700)
    tab.load("https://example.com")
    assert tab.title == "Example Domain", tab.title
    pt = find_link_point(tab, "more")
    assert pt, "link text not found in display list"
    print(f"'more' link at {pt}")
    dest = tab.click(pt[0], pt[1])
    assert dest is not None, "click did not resolve a link"
    print("click resolves to:", dest)
    assert "iana.org" in str(dest), dest

    # Follow it and check history/back.
    tab.load(dest)
    print("navigated to:", tab.url, "| title:", tab.title)
    assert len(tab.history) == 1
    tab.go_back()
    print("after back:", tab.url)
    assert "example.com" in str(tab.url)
    tab.go_forward()
    print("after forward:", tab.url)
    assert "iana.org" in str(tab.url)

    # view-source
    tab.load("view-source:https://example.com")
    assert any("<!doctype" in c.text.lower() or "<html" in c.text.lower()
               for c in tab.display_list if isinstance(c, DrawText)), \
        "view-source did not show markup"
    print("view-source OK")

    test_form_round_trip()

    print("\nALL NAVIGATION TESTS PASSED")


if __name__ == "__main__":
    main()
