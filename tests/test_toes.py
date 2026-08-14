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


def test_unknown_scheme_empty_host_parses():
    u = URL("toehub://")
    assert u.scheme == "toehub"
    assert u.host == ""
    assert str(u) == "toehub://"


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
        with open(os.path.join(demo_dir, "manual.md"), "w") as f:
            f.write("# demo manual\n\nThis is the demo toe's manual.\n\n"
                    "- feature one\n- feature two\n\n```\ncode block\n```\n")
        idx = {"repo": "local", "toes": [
            {"name": "demo", "version": "0.1.0", "description": "demo",
             "files": ["toe.json", "toe.py", "manual.md"]}]}
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
            # manual.md was downloaded with the toe
            assert os.path.isfile(
                os.path.join(tmp, toes.TOES_DIR, "demo", "manual.md"))

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


def test_config_options_declared_and_coerced():
    class FakeToe:
        manifest = {"name": "fake", "version": "0", "description": ""}

        def activate(self, ctx):
            ctx.define_config(
                toes.ConfigOption("dark", "Dark mode", "bool", default=True),
                toes.ConfigOption("step", "Scroll step", "int", default=80),
                toes.ConfigOption("theme", "Theme", "choice",
                                  default="dark",
                                  options=[("dark", "Dark"),
                                           ("light", "Light")]),
            )

    with tempfile.TemporaryDirectory() as tmp:
        orig_root = toes.repo_root
        toes.repo_root = lambda: tmp
        try:
            toe = toes.Toe("fake", "0", "", "", FakeToe())
            stub = StubBrowser([toe])
            ctx = stub.toe_contexts[0]
            # Defaults seeded into settings.
            assert ctx.settings.get("dark") is True
            assert ctx.settings.get("step") == 80
            assert ctx.config_value("dark") is True
            assert ctx.config_value("step") == 80
            assert ctx.config_value("theme") == "dark"
            # Coercion on set.
            ctx.set_config("dark", "false")
            ctx.set_config("step", "120")
            assert ctx.config_value("dark") is False
            assert ctx.config_value("step") == 120
            # config_options lists them sorted by key.
            keys = [k for k, _o in ctx.config_options()]
            assert keys == ["dark", "step", "theme"], keys
        finally:
            toes.repo_root = orig_root


def test_config_page_renders_and_sets():
    from feetbrowser import toehub
    with tempfile.TemporaryDirectory() as tmp:
        demo = os.path.join(tmp, "toes", "demo")
        os.makedirs(demo)
        with open(os.path.join(demo, "toe.json"), "w") as f:
            f.write('{"name": "demo", "entry": "toe.py"}')
        with open(os.path.join(demo, "toe.py"), "w") as f:
            f.write('from feetbrowser import toes\n'
                    'def activate(ctx):\n'
                    '    ctx.define_config(toes.ConfigOption'
                    '("size", "Size", "int", default=16))\n')
        orig_root = toes.repo_root
        toes.repo_root = lambda: tmp
        try:
            browser = StubBrowser()
            # A real-ish context for _find_context.
            toe = toes.Toe("demo", "0", "", demo, demo_mod())
            browser.toe_contexts = [toes.Context(browser, toe.module)]
            browser.toes = [toe]

            from feetbrowser.net import URL
            page = toehub._config_page(URL("toehub://config/demo"),
                                       "demo", browser)
            stripped = _strip(page[1])
            assert "demo" in stripped
            assert "Size" in stripped
            # Set an option.
            toehub._config_page(
                URL("toehub://config/demo/set/size/24"), "demo", browser)
            ctx = browser.toe_contexts[0]
            assert ctx.config_value("size") == 24
        finally:
            toes.repo_root = orig_root


def test_config_set_via_handle_route():
    """The set route must apply through the real handle() path (regression:
    toehub://config/<name>/set/<key>/<value> used to fail with '<path> is
    not installed' because the whole path was treated as the toe name)."""
    from feetbrowser import toehub
    with tempfile.TemporaryDirectory() as tmp:
        demo = os.path.join(tmp, "toes", "demo")
        os.makedirs(demo)
        with open(os.path.join(demo, "toe.json"), "w") as f:
            f.write('{"name": "demo", "entry": "toe.py"}')
        with open(os.path.join(demo, "toe.py"), "w") as f:
            f.write('from feetbrowser import toes\n'
                    'def activate(ctx):\n'
                    '    ctx.define_config(toes.ConfigOption'
                    '("size", "Size", "int", default=16))\n')
        orig_root = toes.repo_root
        toes.repo_root = lambda: tmp
        try:
            browser = StubBrowser()
            toe = toes.Toe("demo", "0", "", demo, demo_mod())
            browser.toe_contexts = [toes.Context(browser, toe.module)]
            browser.toes = [toe]
            tab = Tab(700, browser)
            browser.active_tab = tab

            resp = toehub.handle(
                URL("toehub://config/demo/set/size/24"), tab)
            assert resp is not None
            ctx = browser.toe_contexts[0]
            assert ctx.config_value("size") == 24, (
                ctx.config_value("size"))
            assert "is not installed" not in resp[1]
        finally:
            toes.repo_root = orig_root


def test_config_set_via_query_param():
    """The form submit (str/int inputs) must apply through the query param
    route: toehub://config/<name>/set/<key>?value=<v>."""
    from feetbrowser import toehub
    with tempfile.TemporaryDirectory() as tmp:
        demo = os.path.join(tmp, "toes", "demo")
        os.makedirs(demo)
        with open(os.path.join(demo, "toe.json"), "w") as f:
            f.write('{"name": "demo", "entry": "toe.py"}')
        with open(os.path.join(demo, "toe.py"), "w") as f:
            f.write('from feetbrowser import toes\n'
                    'def activate(ctx):\n'
                    '    ctx.define_config(toes.ConfigOption'
                    '("size", "Size", "int", default=16))\n')
        orig_root = toes.repo_root
        toes.repo_root = lambda: tmp
        try:
            browser = StubBrowser()
            toe = toes.Toe("demo", "0", "", demo, demo_mod())
            browser.toe_contexts = [toes.Context(browser, toe.module)]
            browser.toes = [toe]
            tab = Tab(700, browser)
            browser.active_tab = tab

            resp = toehub.handle(
                URL("toehub://config/demo/set/size?value=32"), tab)
            assert resp is not None
            ctx = browser.toe_contexts[0]
            assert ctx.config_value("size") == 32, ctx.config_value("size")
        finally:
            toes.repo_root = orig_root


def demo_mod():
    import types
    m = types.ModuleType("toe_demo")
    m.manifest = {"name": "demo"}
    m.activate = demo_activate
    return m


def demo_activate(ctx):
    from feetbrowser import toes as _toes
    ctx.define_config(_toes.ConfigOption("size", "Size", "int", default=16))


def test_manual_renders_for_installed_toe():
    from feetbrowser import toehub
    with tempfile.TemporaryDirectory() as tmp:
        demo = os.path.join(tmp, "toes", "demo")
        os.makedirs(demo)
        with open(os.path.join(demo, "toe.json"), "w") as f:
            f.write('{"name": "demo", "entry": "toe.py"}')
        with open(os.path.join(demo, "toe.py"), "w") as f:
            f.write("def activate(ctx):\n    pass\n")
        with open(os.path.join(demo, "manual.md"), "w") as f:
            f.write("# Demo\n\nWhat it does.\n\n- one\n- two\n")
        orig_root = toes.repo_root
        toes.repo_root = lambda: tmp
        try:
            html = toehub.manual_toe("demo")
            stripped = _strip(html)
            assert "demo" in stripped
            assert "What it does" in stripped
            assert "one" in stripped and "two" in stripped
            assert "back to the hub" in stripped
        finally:
            toes.repo_root = orig_root


def test_manual_falls_back_without_file():
    from feetbrowser import toehub
    with tempfile.TemporaryDirectory() as tmp:
        demo = os.path.join(tmp, "toes", "demo")
        os.makedirs(demo)
        with open(os.path.join(demo, "toe.json"), "w") as f:
            f.write('{"name": "demo", "description": "just a demo",'
                    ' "entry": "toe.py"}')
        with open(os.path.join(demo, "toe.py"), "w") as f:
            f.write("def activate(ctx):\n    pass\n")
        orig_root = toes.repo_root
        toes.repo_root = lambda: tmp
        try:
            html = toehub.manual_toe("demo")
            stripped = _strip(html)
            assert "just a demo" in stripped
            assert "manual" in stripped.lower()
        finally:
            toes.repo_root = orig_root


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
