"""Headless test of click-to-navigate, history, and forms plumbing."""
import sys, os, tkinter
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from feetbrowser.browser import Tab
from feetbrowser.layout import DrawText


def find_link_point(tab, needle):
    """Find canvas coords of the DrawText whose text contains needle."""
    for cmd in tab.display_list:
        if isinstance(cmd, DrawText) and needle.lower() in cmd.text.lower():
            return cmd.left + 3, cmd.top + 3
    return None


def main():
    root = tkinter.Tk(); root.withdraw()
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

    print("\nALL NAVIGATION TESTS PASSED")


if __name__ == "__main__":
    main()
