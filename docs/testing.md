# Tests

```bash
./test.sh          # builds both JS engines, then pyflakes + unit + JS + pixels + navigation + toe + Go + live smoke
```

`test.sh` builds the Zig JS engine and runs its own tests — the VM suite, the
parser suite and the regex suite, 500-odd cases that never cross into Python —
then builds the Rust JS engine (`feetbrowser_engine`) into the local `.venv`
with `maturin develop --release` on first use and runs every Python suite from
that venv. A first run therefore needs both a Zig compiler and a Rust
toolchain (maturin is installed into the venv automatically).

Most suites run fully offline: `test_units.py`, `test_js.py` (which serves its
`fetch`/`XMLHttpRequest` cases from a local HTTP server), and `test_toes.py`
(which points the hub at a `file://` catalog in a temp dir). `test_nav.py` and
`smoke.py` load real sites over the network, so both need connectivity when
run the way `test.sh` runs them; [CI](../.github/workflows/ci.yml) points them
at `tests/fixtures` instead, so a pull request neither depends on a third
party being up nor sends them traffic.

The test files live in `tests/`:

```
tests/
  test_suites.py    every file below is run by test.sh and by CI, or this fails
  test_render.py    fonts, rasteriser, image codecs, canvas, event model
  test_cocoa.py     the macOS window, driven by real NSEvents (macOS only)
  test_x11.py       the X11 window, driven by real X events (needs a server)
  x11_shot.py       photographs a real X11 window with XGetImage (CI artifact)
  test_units.py     offline unit tests (URL, HTML, CSS, layout, internal pages)
  test_js.py        offline tests for the JS engine + DOM bridge (run twice,
                    once per engine, via FEETBROWSER_JS)
  test_shoes.py     the Shoes theme manager
  test_e2e.py       a fixture page in, its pixels back out
  test_nav.py       click-to-navigate, history, view-source
  test_toes.py      toe engine + ToeHub tests (install/uninstall/toggle)
  test_asmblend.py  the assembly span kernels against their Python references
  smoke.py          end-to-end pipeline over a real socket
  fixture_server.py serves tests/fixtures over HTTP on loopback
  fixtures/         the pages the three end-to-end suites load
```

`test_e2e.py` is the one that looks at the screen. It fetches a page carrying
text, a PNG, a GIF, a background colour and a border, renders it to a PNG, and
then counts the colours in that PNG: each of those five things has a shade of
its own, so a layer that stops drawing takes a colour off the picture and the
test says which. It exists because `<img>` once stopped drawing anything at
all, on every page, and the suite had nothing that could tell.

`test_suites.py` is the reason a new file in `tests/` cannot be forgotten.
`test.sh` and the workflow both name their suites one at a time — one so the
order and the comments are readable, the other so a red job says which suite
went red — and this fails if a file in `tests/` is missing from either.

The transport layer also has a Go port under `net/`, with its own tests.
`test.sh` runs `go vet ./... && go test ./...` where a Go toolchain is
installed and says so and moves on where there is not; CI always has one.

Nothing here needs a display or a GUI toolkit: the renderer draws into its own
framebuffer, so the whole suite runs headless. `test_render.py` does need at
least one system font, which every platform we support ships.

The exceptions are `test_cocoa.py` and `test_x11.py`, and deliberately so.
They open real windows and feed them real platform events, because the
platform layer is the one place a mistake is invisible from Python — a stale
attribute in the mouse path once swallowed every click with the browser
underneath looking healthy. Each skips itself with a message where its
platform is not there, so the suite is green on all three either way.

`test_x11.py` splits in half. The arithmetic and the lookup tables — scanline
padding, the byte layout a visual's channel masks imply, keysym names, wheel
buttons — are plain functions over plain values, and those tests run
everywhere, including on macOS and Windows. The rest needs a server, and asks
it real questions: XGetGeometry for the window's true size, XSendEvent for
input, and XGetImage to read the frame back off the server and check the
colours arrived in the right order. CI runs that half on Linux under
`xvfb-run`, and `x11_shot.py` uploads the resulting window as a PNG so a human
can see what the Linux build actually drew — after checking that the three
colour swatches on that page came back present and in order, which is what a
wrong channel mask or byte order permutes.

CI runs the offline suite on every interpreter the engine supports (3.9
through 3.14) and on macOS as well as Linux, so `test_cocoa.py` opens real
NSWindows somewhere rather than only proving its skip is clean. Pillow and
cairosvg stay optional: one job installs them to cover the JPEG/WebP/SVG
branches, and every other job runs without them.

The Linux jobs build both engines and run `test_js.py` twice, once against
each, because the point of having two is that they answer the same questions.
The macOS lines run the Rust engine only: Zig 0.14 on that runner image
cannot find the SDK it needs to link against libSystem and fails before it
compiles anything of ours. Locally `test.sh` builds both on macOS perfectly
well, so this is a gap in CI rather than in the engine.

The Zig engine's own tests are a job of their own. They never cross into
Python, so running them once says as much as running them on all eight
interpreters would, which is the same reason the Rust and Go toolchains have
a job each.
