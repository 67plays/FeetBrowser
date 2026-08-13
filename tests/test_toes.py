"""Unit tests for the toes engine + ToeHub.

Uses a temporary toes/ dir and a local catalog served via file:// so the
tests are deterministic and offline.
"""
import json
import os
import sys
import tempfile
import tkinter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from feetbrowser import toes
from feetbrowser.browser import Tab
from feetbrowser.htmlparser import Text, Element
from feetbrowser.net import URL


class StubBrowser:
    """Minimal Browser stand-in: toe contexts plus the bits a Tab touches."""

    def __init__(self, toe_list=None):
        self.tabs = []
        self.active_tab = None
        self.toes = toe_list if toe_list is not None else toes.discover_toes()
        self.toe_contexts = [toes.Context(self, t.module) for t in self.toes]
        self.draw_calls = 0

    def reload_toes(self):
        self.toes = toes.discover_toes()
        self.toe_contexts = [toes.Context(self, t.module) for t in self.toes]
        self.draw_calls += 1

    def draw(self):
        pass


def find_element(node, tag, attrs):
    if isinstance(node, Element) and node.tag == tag \
            and all(node.attributes.get(k) == v for k, v in attrs.items()):
        return node
    for child in node.children:
        found = find_element(child, tag, attrs)
        if found:
            return found
    return None


def element_text(node):
    parts = []
    if isinstance(node, Text):
        parts.append(node.text)
    for child in node.children:
        parts.append(element_text(child))
    return "".join(parts)


def display_text(tab):
    return " ".join(
        c.text for c in tab.display_list if type(c).__name__ == "DrawText")


def test_no_toes_by_default():
    toe_list = toes.discover_toes()
    assert toe_list == [], f"expected bare framework, found {toe_list}"


def test_unknown_scheme_parses():
    u = URL("toe://hello")
    assert u.scheme == "toe"
    assert u.host == "hello"
    assert str(u) == "toe://hello"


def test_hub_renders_with_zero_toes():
    stub = StubBrowser()
    tab = Tab(700, stub)
    stub.active_tab = tab
    tab.load("toe://hub")
    assert tab.document is not None
    assert "TOEHUB" in display_text(tab)


def test_gallery_empty():
    stub = StubBrowser()
    tab = Tab(700, stub)
    stub.active_tab = tab
    tab.load("toe://gallery")
    assert "GALLERY" in display_text(tab)
    assert "No toes installed" in display_text(tab)


def test_hello_placeholder():
    stub = StubBrowser()
    tab = Tab(700, stub)
    stub.active_tab = tab
    tab.load("toe://hello")
    assert "hello" in display_text(tab).lower()


def test_broken_toe_is_skipped():
    with tempfile.TemporaryDirectory() as tmp:
        broken = os.path.join(tmp, "broken-toe")
        os.makedirs(broken)
        with open(os.path.join(broken, "toe.json"), "w") as f:
            f.write('{"name": "broken", "entry": "toe.py"}')
        with open(os.path.join(broken, "toe.py"), "w") as f:
            f.write("raise ImportError('kaput')\n")
        toe_list = toes.discover_toes(tmp)
        assert all(t.name != "broken" for t in toe_list), toe_list


def test_install_enable_disable_uninstall():
    from feetbrowser import toehub
    stub = StubBrowser()
    tab = Tab(700, stub)
    stub.active_tab = tab

    with tempfile.TemporaryDirectory() as tmp:
        catalog_dir = os.path.join(tmp, "cat")
        demo_dir = os.path.join(catalog_dir, "demo")
        os.makedirs(demo_dir)
        with open(os.path.join(demo_dir, "toe.json"), "w") as f:
            f.write('{"name": "demo", "version": "0.1.0",'
                    ' "description": "demo", "entry": "toe.py"}')
        with open(os.path.join(demo_dir, "toe.py"), "w") as f:
            f.write('def activate(ctx):\n    ctx.on("buttons",'
                    ' lambda: [])\n')
        idx = {"repo": "local", "toes": [
            {"name": "demo", "version": "0.1.0", "description": "demo",
             "files": ["toe.json", "toe.py"]}]}
        with open(os.path.join(catalog_dir, "index.json"), "w") as f:
            json.dump(idx, f)

        # Redirect repo_root to the temp dir FIRST so config writes land
        # there, then point the hub at the local catalog.
        orig_root = toes.repo_root
        toes.repo_root = lambda: tmp
        try:
            toehub.set_catalog_url("file://" + catalog_dir + "/index.json")
            assert "file://" in toehub.catalog_url(), toehub.catalog_url()

            # install
            catalog, _ = toehub.fetch_catalog()
            msg = toehub.install_toe("demo", catalog, stub)
            assert "Installed" in _strip(msg), msg
            assert "demo" in toehub.installed_toes()

            # disable
            msg = toehub.toggle_toe("demo", False, stub)
            assert "disabled" in _strip(msg)
            assert "demo" in toes.disabled_toes()

            # enable
            msg = toehub.toggle_toe("demo", True, stub)
            assert "enabled" in _strip(msg)
            assert "demo" not in toes.disabled_toes()

            # uninstall
            msg = toehub.uninstall_toe("demo", stub)
            assert "Uninstalled" in _strip(msg)
            assert "demo" not in toehub.installed_toes()
        finally:
            toes.repo_root = orig_root
            toehub.set_catalog_url(
                "https://raw.githubusercontent.com/xplosivex/"
                "feetbrowser-toes/main/index.json")


def test_toggle_persists_across_instances():
    with tempfile.TemporaryDirectory() as tmp:
        toe_dir = os.path.join(tmp, "toe")
        os.makedirs(toe_dir)
        with open(os.path.join(toe_dir, "toe.json"), "w") as f:
            f.write('{"name": "demo", "entry": "toe.py"}')
        with open(os.path.join(toe_dir, "toe.py"), "w") as f:
            f.write('def activate(ctx):\n    pass\n')
        orig_root = toes.repo_root
        toes.repo_root = lambda: tmp
        try:
            toes.set_toe_enabled("demo", False)
            assert "demo" in toes.disabled_toes()
            toes.set_toe_enabled("demo", True)
            assert "demo" not in toes.disabled_toes()
        finally:
            toes.repo_root = orig_root


def test_buttons_hook_registered():
    class FakeToe:
        manifest = {"name": "fake", "version": "0", "description": ""}

        def activate(self, ctx):
            ctx.on("buttons", lambda: [toes.ButtonDef("fake-btn", "F")])

    toe = toes.Toe("fake", "0", "", "", FakeToe())
    stub = StubBrowser([toe])
    assert stub.toe_contexts[0].call("buttons")[0].id == "fake-btn"


def _strip(html):
    import re
    text = re.sub(r"<[^>]+>", " ", html)
    return re.sub(r"\s+", " ", text).strip()


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
    print(f"\nALL {len(tests)} TOE TESTS PASSED")


if __name__ == "__main__":
    main()
