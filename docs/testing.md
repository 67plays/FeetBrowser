# Tests

```bash
./test.sh          # builds the Rust engine, then pyflakes + unit + JS + navigation + toe + pseudo-toe + live smoke
test.cmd           # the same suites in the same order, on Windows
```

`test.sh` builds the Rust JS engine (`feetbrowser_engine`) into the local
`.venv` with `maturin develop --release` on first use, then runs every suite
from that venv — so a first run needs the Rust toolchain (maturin is
installed into the venv automatically).

Most suites run fully offline: `test_units.py`, `test_js.py` (which serves its
`fetch`/`XMLHttpRequest` cases from a local HTTP server), `test_toes.py` (which
points the hub at a `file://` catalog in a temp dir), and `test_gh_scroll.py`
(which stubs the pseudo-toe's fetches). `test_nav.py` and `smoke.py` load real
sites over the network, so both need connectivity — which is why
[CI](../.github/workflows/ci.yml) runs only the offline suites.

The test files live in `tests/`:

```
tests/
  test_render.py    fonts, rasteriser, image codecs, canvas, event model
  test_cocoa.py     the macOS window, driven by real NSEvents (macOS only)
  test_win32.py     the Windows window, driven by real messages (Windows only)
  test_units.py     offline unit tests (URL, HTML, CSS, layout, internal pages)
  test_js.py        offline tests for the JS engine + DOM bridge
  test_shoes.py     the Shoes theme manager
  test_nav.py       click-to-navigate, history, view-source (needs network)
  test_toes.py      toe engine + ToeHub tests (install/uninstall/toggle)
  test_gh_scroll.py pseudo-toe (gh-scroll) hooks and gh:// navigation
  smoke.py          end-to-end pipeline on real pages (needs network)
  check_screenshot.py  CI's end-to-end check that a --screenshot run drew
```

Nothing here needs a display or a GUI toolkit: the renderer draws into its own
framebuffer, so the whole suite runs headless. `test_render.py` does need at
least one system font, which every platform we support ships.

The exceptions are `test_cocoa.py` and `test_win32.py`, and deliberately so.
They open real windows and feed them real events, because the platform layer
is the one place a mistake is invisible from Python — a stale attribute in the
mouse path once swallowed every click with the browser underneath looking
healthy. Each skips itself with a message anywhere but its own operating
system, and CI runs both jobs so that both skips stay clean as well.

`test_win32.py` is the only thing that can demonstrate the Windows window
works, so treat the `windows-latest` job as the verification of anything in
`win32.py`. Its arithmetic and translation tables are deliberately plain
functions living outside the window, and those are exercised by
`test_units.py` on every platform the suite runs on.
