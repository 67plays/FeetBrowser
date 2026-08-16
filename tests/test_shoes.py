"""Tests for the Shoes theme manager: palettes, persistence, the about:shoes
picker page, and applying a shoe."""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from feetbrowser import shoes
from doormat.window import Tk
from feetbrowser.browser import (
    _AboutURL, _ShoesURL, _ShoesApplyURL, welcome_html, bookmarks_html,
    history_html,
)


def eq(a, b, msg=""):
    assert a == b, f"{msg}: {a!r} != {b!r}"


def test_every_shoe_defines_all_keys():
    for name in shoes.shoe_names():
        pal = shoes.merge(shoes.resolve(name))
        for key in shoes.SHOE_KEYS:
            assert key in pal and pal[key], \
                f"{name} is missing color for {key!r}"


def test_default_shoe_is_classic():
    eq(shoes.DEFAULT_SHOE, "Classic Sneaker", "default")
    assert shoes.resolve("Classic Sneaker") == shoes.SHOES["Classic Sneaker"]


def test_find_is_case_insensitive():
    eq(shoes.find("Ocean Slipper"), "Ocean Slipper")
    eq(shoes.find("ocean slipper"), "Ocean Slipper")
    eq(shoes.find("nope"), None, "unknown shoe")


def test_load_save_roundtrip():
    tmp = tempfile.mkdtemp()
    original = shoes.SHOES_FILE
    shoes.SHOES_FILE = os.path.join(tmp, "shoes.json")
    try:
        shoes.save("Midnight Boot")
        eq(shoes.load(), "Midnight Boot", "saved shoe is loaded")
        # A missing/empty file falls back to the default.
        os.remove(shoes.SHOES_FILE)
        eq(shoes.load(), shoes.DEFAULT_SHOE, "missing file -> default")
    finally:
        shoes.SHOES_FILE = original


def test_merge_fills_missing_keys():
    partial = {"chrome_bg": "#123456"}
    merged = shoes.merge(partial)
    eq(merged["chrome_bg"], "#123456", "explicit value wins")
    eq(merged["tab_bar"], shoes.SHOES["Classic Sneaker"]["tab_bar"],
       "missing key falls back to default")


def test_shoes_page_lists_all_themes():
    theme = shoes.merge(shoes.resolve(shoes.DEFAULT_SHOE))
    url = _ShoesURL(apply=lambda n: None, theme=theme,
                    active=lambda: "Classic Sneaker")
    _h, body, _ct = url.request()
    for name in shoes.shoe_names():
        assert name in body, f"{name} missing from the picker page"
    assert 'href="about:shoes/Ocean Slipper"' in body, "apply link present"
    assert "in use" in body, "active shoe is highlighted"


def test_shoe_apply_calls_provider():
    theme = shoes.merge(shoes.resolve(shoes.DEFAULT_SHOE))
    applied = []
    url = _ShoesApplyURL("ocean slipper", apply=applied.append, theme=theme)
    _h, body, _ct = url.request()
    eq(applied, ["Ocean Slipper"], "apply called with the canonical name")
    assert "Ocean Slipper on." in body, "confirmation page"


def test_internal_pages_are_themed():
    dark = shoes.merge(shoes.resolve("Midnight Boot"))
    welcome = welcome_html(dark)
    assert dark["page_bg"] in welcome, "welcome page uses themed background"
    assert dark["accent"] in welcome, "welcome page uses themed accent"
    bookmarks = bookmarks_html(["https://example.com"], dark)
    assert dark["link_color"] in bookmarks, "bookmarks page uses themed link"
    history = history_html({"back": [], "current": "", "forward": []}, dark)
    assert dark["page_bg"] in history, "history page uses themed background"


def test_about_resolve_handles_shoes():
    url = _AboutURL(theme=None)
    resolved = url.resolve("about:shoes")
    assert isinstance(resolved, _ShoesURL), "about:shoes resolves to picker"
    applied = url.resolve("about:shoes/Classic Sneaker")
    assert isinstance(applied, _ShoesApplyURL), "apply URL type"
    eq(applied.name, "Classic Sneaker", "apply URL keeps the shoe name")


def test_apply_provider_threads_from_welcome():
    # The welcome page is the entry point: clicking a shoe there must reach
    # the apply provider (regression: it was dropped, so themes never applied
    # when Shoes was opened from the home page).
    calls = []
    welcome = _AboutURL(apply=lambda n: calls.append(n),
                        active=lambda: "Classic Sneaker")
    picker = welcome.resolve("about:shoes")
    assert picker.apply is not None, "apply provider survives resolve"
    apply_url = picker.resolve("about:shoes/Ocean Slipper")
    _h, body, _ct = apply_url.request()
    eq(calls, ["Ocean Slipper"], "welcome-opened picker applies shoes")


def test_apply_page_back_link_does_not_crash():
    # The 'Back to Shoes' link on the apply page resolves against the apply
    # URL itself, which must implement resolve() (regression: AttributeError
    # '_ShoesApplyURL' object has no attribute 'resolve').
    apply_url = _ShoesApplyURL("Midnight Boot")
    back = apply_url.resolve("about:shoes")
    assert isinstance(back, _ShoesURL), "apply page can resolve back links"
    another = apply_url.resolve("about:shoes/Sunset Heel")
    assert isinstance(another, _ShoesApplyURL), "apply page can chain apply"
    eq(another.name, "Sunset Heel")


def main():
    root = Tk(); root.withdraw()
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
    print(f"\nALL {len(tests)} SHOES TESTS PASSED")


if __name__ == "__main__":
    main()