"""Tests for the pseudo-toe toe (pseudo-site GitHub repo hub).

The toe's network layer (_gh_get) is patched with canned GitHub API JSON so
the tests are deterministic and offline.
"""
import base64
import importlib.util
import os
import re
import sys
import tempfile
import tkinter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from feetbrowser import toes
from feetbrowser.browser import Tab
from feetbrowser.net import URL

TOE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "toes", "pseudo-toe")
TOE_PATH = os.path.join(TOE_DIR, "toe.py")


def _b64(s):
    return base64.b64encode(s.encode()).decode()


FAKE = {
    "/repos/torvalds/linux": {
        "full_name": "torvalds/linux",
        "description": "Linux kernel source tree",
        "stargazers_count": 180000,
        "forks_count": 52000,
        "language": "C",
        "default_branch": "master",
    },
    "/repos/torvalds/linux/readme": {
        "name": "README.md", "encoding": "base64",
        "content": _b64("# Linux\n\nThe Linux **kernel**.\n"),
    },
    "/repos/torvalds/linux/contents": [
        {"name": "Documentation", "type": "dir",
         "path": "torvalds/linux/Documentation"},
        {"name": "Makefile", "type": "file",
         "path": "torvalds/linux/Makefile", "size": 300},
    ],
    "/repos/torvalds/linux/contents/Documentation": [
        {"name": "process", "type": "dir",
         "path": "torvalds/linux/Documentation/process"},
    ],
    "/repos/torvalds/linux/contents/Makefile": {
        "name": "Makefile", "type": "file", "encoding": "base64",
        "content": _b64("all:\n\techo hi\n"), "size": 300,
    },
    "/search/repositories?q=tiny%20python": {
        "items": [
            {"full_name": "a/tinypy", "stargazers_count": 5,
             "language": "Python", "description": "a tiny python"},
        ],
    },
    "/users/torvalds/repos?sort=updated&per_page=50": [
        {"name": "linux", "full_name": "torvalds/linux",
         "stargazers_count": 180000, "language": "C",
         "description": "Linux kernel source tree"},
    ],
}


def load_toe(tmp):
    """Load the real toe module, pointed at a temp settings dir."""
    spec = importlib.util.spec_from_file_location("toe_pseudo_toe", TOE_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.manifest = {"name": "pseudo-toe", "version": "0.1.0",
                    "description": ""}
    mod.folder = tmp
    mod._gh_get = lambda ctx, p: FAKE.get(p)
    return mod


class StubBrowser:
    """Minimal Browser stand-in with a single toe context."""

    def __init__(self, toe_module):
        self.tabs = []
        self.active_tab = None
        self.toe_contexts = [toes.Context(self, toe_module)]
        self.draw_calls = 0

    def reload_toes(self):
        self.draw_calls += 1

    def draw(self):
        pass


def setup():
    tmp = tempfile.mkdtemp()
    mod = load_toe(tmp)
    stub = StubBrowser(mod)
    ctx = stub.toe_contexts[0]
    ctx.enabled = True  # the toe under test; ignore the local disabled list
    return mod, ctx, stub, tmp


def body(url):
    """Serve `url` through the toe's handle hook; return (html, ctx)."""
    _mod, ctx, _stub, _tmp = setup()
    resp = ctx.call("handle", URL(url), None)
    return resp[1], ctx


def strip(html):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html)).strip()


def display_text(tab):
    return " ".join(
        c.text for c in tab.display_list if type(c).__name__ == "DrawText")


def test_manifest_discovered():
    found = toes.discover_toes()
    assert any(t.name == "pseudo-toe" for t in found), found


def test_hub_page():
    html, _ctx = body("gh://hub")
    text = strip(html)
    assert "GH·SCROLL" in text
    assert "FEATURED" in text
    assert "torvalds/linux" in text
    assert 'name="q"' in html
    assert 'name="owner_repo"' in html


def test_repo_page():
    html, _ctx = body("gh://browse/torvalds/linux")
    text = strip(html)
    assert "torvalds/linux" in text
    assert "★ 180000" in text
    assert "forks 52000" in text
    assert "README" in text
    assert "Documentation/" in text
    assert "Makefile" in text
    assert "Linux kernel" in text


def test_dir_page():
    html, _ctx = body("gh://browse/torvalds/linux/Documentation")
    text = strip(html)
    assert "DIRECTORY" in text
    assert "process/" in text
    assert "repo root" in text


def test_file_page():
    html, _ctx = body("gh://browse/torvalds/linux/Makefile")
    text = strip(html)
    assert "TEXT" in text
    assert "300B" in text
    assert "echo hi" in text
    assert "<pre>" in html


def test_search_page():
    html, _ctx = body("gh://search/?q=tiny+python")
    text = strip(html)
    assert "1 results" in text
    assert "a/tinypy" in text
    assert "a tiny python" in text


def test_search_page_blank():
    html, _ctx = body("gh://search/")
    assert "type a query" in strip(html)


def test_user_page():
    html, _ctx = body("gh://user/torvalds")
    text = strip(html)
    assert "PUBLIC REPOS" in text
    assert "linux" in text
    assert "★ 180000" in text


def test_jump_to_repo_form():
    html, _ctx = body("gh://browse/?owner_repo=torvalds%2Flinux")
    assert "★ 180000" in strip(html)


def test_jump_to_repo_full_url():
    q = "https%3A%2F%2Fgithub.com%2Ftorvalds%2Flinux"
    html, _ctx = body(f"gh://browse/?owner_repo={q}")
    assert "★ 180000" in strip(html)


def test_github_repo_link_intercepted():
    html, _ctx = body("https://github.com/torvalds/linux")
    assert "★ 180000" in strip(html)
    html, _ctx = body("https://github.com/torvalds/linux/tree/master/Documentation")
    assert "process/" in strip(html)
    html, _ctx = body("https://github.com/torvalds/linux/blob/master/Makefile")
    assert "echo hi" in strip(html)


def test_non_repo_github_links_fall_through():
    _mod, ctx, _stub, _tmp = setup()
    for u in ("https://github.com/features",
              "https://github.com/torvalds/linux/issues/1",
              "https://raw.githubusercontent.com/torvalds/linux/master/README"):
        assert ctx.call("handle", URL(u), None) is None, u


def test_intercept_can_be_disabled():
    _mod, ctx, _stub, _tmp = setup()
    ctx.set_config("intercept_github", "false")
    assert ctx.call("handle", URL("https://github.com/torvalds/linux"),
                    None) is None


def test_unknown_gh_host_falls_back_to_hub():
    html, _ctx = body("gh://wat")
    assert "GH·SCROLL" in strip(html)


def test_error_page_for_missing_repo():
    html, _ctx = body("gh://browse/nope/missing")
    text = strip(html)
    assert "GH·ERROR" in text
    assert "nope/missing" in text


def test_toolbar_button():
    _mod, ctx, _stub, _tmp = setup()
    buttons = ctx.call("buttons")
    assert any(b.id == "pseudo-toe" and b.glyph == "GH" for b in buttons)


def test_recent_tracking_and_settings():
    _mod, ctx, _stub, tmp = setup()
    ctx.call("handle", URL("gh://browse/torvalds/linux"), None)
    assert ctx.settings.get("recent") == ["torvalds/linux"]
    assert os.path.isfile(os.path.join(tmp, "settings.json"))


def test_full_tab_load():
    _mod, ctx, stub, _tmp = setup()
    tab = Tab(700, stub)
    stub.active_tab = tab
    tab.load("gh://browse/torvalds/linux")
    text = display_text(tab)
    assert "torvalds" in text
    assert "★ 180000" in text
    assert "README" in text


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
    print(f"\nALL {len(tests)} PSEUDO-TOE TESTS PASSED")


if __name__ == "__main__":
    main()