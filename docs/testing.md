# Tests

```bash
./test.sh          # builds the Rust engine, then pyflakes + unit + JS + navigation + toe + live smoke
test.cmd           # the same suites in the same order, on Windows
```

`test.sh` builds the Rust JS engine (`feetbrowser_engine`) into the local
`.venv` with `maturin develop --release` on first use, then runs every suite
from that venv — so a first run needs the Rust toolchain (maturin is
installed into the venv automatically).

Most suites run fully offline: `test_units.py`, `test_js.py` (which serves its
`fetch`/`XMLHttpRequest` cases from a local HTTP server), and `test_toes.py`
(which points the hub at a `file://` catalog in a temp dir). `test_nav.py` and
`smoke.py` load real sites over the network, so both need connectivity — which
is why [CI](../.github/workflows/ci.yml) runs only the offline suites.

The test files live in `tests/`:

```
tests/
  test_render.py    fonts, rasteriser, image codecs, canvas, event model
  test_cocoa.py     the macOS window, driven by real NSEvents (macOS only)
  test_x11.py       the X11 window, driven by real X events (needs a server)
  x11_shot.py       photographs a real X11 window with XGetImage (CI artifact)
  test_win32.py     the Windows window, driven by real messages (Windows only)
  test_units.py     offline unit tests (URL, HTML, CSS, layout, internal pages)
  test_js.py        offline tests for the JS engine + DOM bridge
  test_shoes.py     the Shoes theme manager
  test_nav.py       click-to-navigate, history, view-source (needs network)
  test_toes.py      toe engine + ToeHub tests (install/uninstall/toggle)
  test_asmblend.py  the assembly span kernels against their Python references
  smoke.py          end-to-end pipeline on real pages (needs network)
  check_screenshot.py  CI's end-to-end check that a --screenshot run drew
```

Nothing here needs a display or a GUI toolkit: the renderer draws into its own
framebuffer, so the whole suite runs headless. `test_render.py` does need at
least one system font, which every platform we support ships.

The exceptions are `test_cocoa.py`, `test_x11.py` and `test_win32.py`, and
deliberately so. They open real windows and feed them real platform events,
because the platform layer is the one place a mistake is invisible from
Python — a stale attribute in the mouse path once swallowed every click with
the browser underneath looking healthy. Each skips itself with a message where
its platform is not there, so the suite is green on all three either way.

`test_x11.py` splits in half. The arithmetic and the lookup tables — scanline
padding, the byte layout a visual's channel masks imply, keysym names, wheel
buttons — are plain functions over plain values, and those tests run
everywhere, including on macOS and Windows. The rest needs a server, and asks
it real questions: XGetGeometry for the window's true size, XSendEvent for
input, and XGetImage to read the frame back off the server and check the
colours arrived in the right order. CI runs that half on Linux under
`xvfb-run`, and `x11_shot.py` uploads the resulting window as a PNG so a human
can see what the Linux build actually drew.

`test_win32.py` splits the same way, and for the same reason. Its offline half
— DIB stride rounding, the BGRA byte order, virtual-key to keysym translation,
the wheel-delta arithmetic — runs on every platform out of `test_units.py` as
well. Its other half opens real windows, pumps real messages through the
window procedure and reads pixels back out of GDI, and that half only ever
runs on the `windows-latest` job. Treat that job as the verification of
anything in `win32.py`: nothing else executes a line of it.
