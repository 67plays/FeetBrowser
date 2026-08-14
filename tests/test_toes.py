"""Unit tests for the toes engine + ToeHub.

Uses a temporary toes/ dir and a local catalog served via file:// so the
tests are deterministic and offline.

The last section is a different kind of test. Toes in the wild were written
against tkinter, and the raster backend only *imitates* Tk; SURFACE_TOE below
is a fixture that makes exactly the calls the published catalog toes make, so
that the day one of those calls stops behaving like Tk's, this file says so
rather than a user's toolbar quietly going blank.
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from feetbrowser import gui

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
    # A fresh checkout ships barefoot: discover_toes on an empty dir must
    # find nothing (the local toes/ may hold user-installed toes).
    with tempfile.TemporaryDirectory() as tmp:
        toe_list = toes.discover_toes(tmp)
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
    stub = StubBrowser([])
    tab = Tab(700, stub)
    stub.active_tab = tab
    tab.load("toe://hub")
    assert tab.document is not None
    assert "TOEHUB" in display_text(tab)


def test_gallery_empty():
    with tempfile.TemporaryDirectory() as tmp:
        orig_root = toes.repo_root
        toes.repo_root = lambda: tmp
        try:
            stub = StubBrowser([])
            tab = Tab(700, stub)
            stub.active_tab = tab
            tab.load("toe://gallery")
            assert "GALLERY" in display_text(tab)
            assert "No toes installed" in display_text(tab)
        finally:
            toes.repo_root = orig_root


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


# -- the Tk surface toes draw against -------------------------------------

# One fixture toe making every Tk call the published catalog toes make
# between them: winfo_width, create_rectangle/line/text with Tk's option
# spellings, a layout font it measures with, and an attribute stashed on the
# context. Written out longhand instead of pointing at an installed toe
# because toes/ is empty on a fresh checkout, and a contract test that only
# runs on the author's machine is not a contract test.
SURFACE_TOE = '''
from feetbrowser import toes
from feetbrowser.layout import get_font

BAND = "surface-band"

# The glyphs the catalog toolbars label their buttons with. None of them are
# ASCII and none of them are in the browser's default face.
GLYPHS = "\\u2039\\u203a\\u27f3\\u2302\\u2605\\u2606\\u2190\\u2192"


def activate(ctx):
    ctx.on("chrome_bands", lambda: [(BAND, 30)])
    ctx.on("on_chrome_draw", lambda canvas, bands: draw_band(ctx, canvas,
                                                             bands))
    ctx.on("on_draw", lambda canvas, offset: overlay(ctx, canvas, offset))
    ctx.on("on_keypress", lambda e: key(ctx, e))
    ctx.on("buttons", lambda: [toes.ButtonDef("surface", "S")])


def draw_band(ctx, canvas, bands):
    band = next((b for b in bands if b[0] == BAND), None)
    if band is None:
        return
    _id, height, y = band
    width = canvas.winfo_width()
    canvas.create_rectangle(0, y, width, y + height, fill="#c0c0c0", width=0)
    canvas.create_line(0, y + height - 1, width, y + height - 1,
                       fill="#808080")
    canvas.create_text(8, y + height // 2, text=GLYPHS * 6, anchor="w",
                       fill="#00ff00",
                       font=get_font(9, "bold", "roman", "Helvetica"))
    # Painted after the text it overlaps, so it has to cover it.
    canvas.create_rectangle(4, y + 2, 68, y + height - 2,
                            fill="#ffff00", outline="#000")
    ctx.band_width = width


def overlay(ctx, canvas, offset):
    font = get_font(10, "bold", "roman", "Helvetica")
    label = "div#main"
    canvas.create_rectangle(20, offset + 10, 120, offset + 60,
                            outline="#ff0000", width=2)
    canvas.create_rectangle(20, offset + 6, 20 + font.measure(label) + 4,
                            offset + 10, fill="#ff0000", outline="#ff0000")
    canvas.create_text(22, offset + 8, text=label, anchor="w",
                       fill="#ffffff", font=font)


def key(ctx, e):
    ctx.seen = (getattr(e, "char", ""), getattr(e, "keysym", ""))
    return e.keysym == "Escape"
'''

BAND_HEIGHT = 30
CHROME = 60


def _write_toe(folder, source, name="surface"):
    os.makedirs(folder, exist_ok=True)
    with open(os.path.join(folder, "toe.json"), "w") as f:
        f.write('{"name": "%s", "version": "1.0", "entry": "toe.py"}' % name)
    with open(os.path.join(folder, "toe.py"), "w") as f:
        f.write(source)


def _surface_ctx(tmp, warnings):
    """Discover the fixture toe from `tmp` and return its live Context.

    Discovery skips a toe it cannot import with a warning to stderr, and a
    browser that starts clean with a toe missing is the failure mode this
    whole section exists to catch -- so the warnings are collected, not
    trusted to be absent.
    """
    _write_toe(os.path.join(tmp, "surface"), SURFACE_TOE)
    found = _capture(warnings, toes.discover_toes, tmp)
    assert [t.name for t in found] == ["surface"], found
    return StubBrowser(found).toe_contexts[0]


def _capture(sink, func, *args):
    """Run `func`, appending anything it writes to stderr to `sink`."""
    import io
    real, sys.stderr = sys.stderr, io.StringIO()
    try:
        return func(*args)
    finally:
        text, sys.stderr = sys.stderr.getvalue(), real
        if text.strip():
            sink.append(text.strip())


def _canvas(width=800, height=200):
    return gui.Canvas(width=width, height=height, bg="white")


def _scan(canvas, y0, y1, predicate):
    """Count pixels in a horizontal strip that satisfy `predicate`.

    Returns None on a backend that does not hand out its pixels -- Tk keeps
    them inside Tcl, so there the item-level assertions are all we get.
    """
    if not hasattr(canvas, "render"):
        return None
    surface = canvas.render()
    hits = 0
    for y in range(max(0, y0), min(surface.height, y1)):
        row = y * surface.stride
        for x in range(0, surface.width):
            i = row + x * 3
            if predicate(surface.pixels[i], surface.pixels[i + 1],
                         surface.pixels[i + 2]):
                hits += 1
    return hits


def _greenish(r, g, b):
    return g > 140 and r < 140 and b < 140


def _reddish(r, g, b):
    return r > 150 and g < 90 and b < 90


def test_toe_chrome_band_paints_inside_its_own_band():
    warnings = []
    with tempfile.TemporaryDirectory() as tmp:
        ctx = _surface_ctx(tmp, warnings)
        bands = toes.compute_bands([ctx])
        assert bands == [("surface-band", BAND_HEIGHT, 0)], bands
        canvas = _canvas()
        before = len(canvas.find_all())
        _capture(warnings, toes.dispatch, [ctx], "on_chrome_draw", canvas,
                 bands)
        assert not warnings, warnings
        # Four items: background, separator, glyph run, the covering button.
        assert len(canvas.find_all()) - before == 4, canvas.find_all()
        assert ctx.band_width == canvas.winfo_width(), ctx.band_width

        inside = _scan(canvas, 0, BAND_HEIGHT, _greenish)
        if inside is not None:
            # winfo_width has to report the canvas width, or a band that
            # sizes itself to the window draws a zero-width strip and
            # vanishes. Real Tk answers 1 until the canvas is mapped, so
            # this half of the contract is only assertable on our own.
            assert ctx.band_width == 800, ctx.band_width
            assert inside > 50, inside
            below = _scan(canvas, BAND_HEIGHT, 200, _greenish)
            assert below == 0, below


def test_toe_band_items_stack_in_creation_order():
    """The 2003-toolbar toes rely on a later rectangle hiding earlier text.

    Tk's canvas has no z-index: what you draw last wins. A backend that
    sorted by anything else would leave the marquee showing through every
    button on the bar.
    """
    warnings = []
    with tempfile.TemporaryDirectory() as tmp:
        ctx = _surface_ctx(tmp, warnings)
        canvas = _canvas()
        toes.dispatch([ctx], "on_chrome_draw", canvas,
                      toes.compute_bands([ctx]))
        if not hasattr(canvas, "render"):
            return
        surface = canvas.render()
        # (8, 15) is under the glyph run and under the yellow button that
        # was drawn over it.
        i = 15 * surface.stride + 8 * 3
        pixel = (surface.pixels[i], surface.pixels[i + 1],
                 surface.pixels[i + 2])
        assert pixel == (255, 255, 0), pixel


def test_toe_overlay_draws_over_the_page_at_the_chrome_offset():
    warnings = []
    with tempfile.TemporaryDirectory() as tmp:
        ctx = _surface_ctx(tmp, warnings)
        canvas = _canvas()
        _capture(warnings, toes.dispatch, [ctx], "on_draw", canvas, CHROME)
        assert not warnings, warnings
        assert len(canvas.find_all()) == 3, canvas.find_all()
        painted = _scan(canvas, CHROME, 200, _reddish)
        if painted is not None:
            assert painted > 50, painted
            assert _scan(canvas, 0, CHROME, _reddish) == 0


def test_toolbar_glyphs_have_real_widths():
    """Toolbars label their buttons with arrows, stars and a house.

    A face that lacks them measures them at zero and paints nothing, so the
    whole bar comes out blank with no error anywhere.
    """
    from feetbrowser.layout import get_font
    font = get_font(11, "bold", "roman", "Helvetica")
    for glyph in "‹›⟳⌂★☆←→":
        assert font.measure(glyph) > 0.5, (glyph, font.measure(glyph))


def test_toe_keypress_sees_char_and_keysym():
    warnings = []
    with tempfile.TemporaryDirectory() as tmp:
        ctx = _surface_ctx(tmp, warnings)
        window = gui.Tk()
        if not hasattr(window, "dispatch"):
            return  # tkinter delivers its own events; nothing to synthesise
        from feetbrowser.window import Event
        seen = []
        window.bind("<Key>", lambda e: seen.append(
            ctx.call("on_keypress", e)))
        window.dispatch("<Key>", Event(char="j", keysym="j", type="<Key>"))
        window.dispatch("<Key>", Event(char="\x1b", keysym="Escape",
                                       type="<Key>"))
        assert seen == [False, True], seen
        assert ctx.seen == ("\x1b", "Escape"), ctx.seen


def test_toe_after_timer_fires():
    """Bars that animate schedule their next frame with window.after."""
    window = gui.Tk()
    ticks = []
    window.after(0, lambda: ticks.append(1))
    window.update_idletasks()
    assert ticks == [1], ticks


def test_toe_context_accepts_stashed_attributes():
    """Toes keep their per-session state on the context object itself
    (sniff mode, hover boxes, redraw-scheduled flags). Nothing declares
    those up front, so the Context must stay a plain attribute bag."""
    warnings = []
    with tempfile.TemporaryDirectory() as tmp:
        ctx = _surface_ctx(tmp, warnings)
        ctx.sniffing = True
        ctx._redraw_scheduled = False
        assert ctx.sniffing is True
        assert getattr(ctx, "nothing_set_this", None) is None


def main():
    root = gui.Tk(); root.withdraw()
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
