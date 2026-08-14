# Tests

```bash
./test.sh          # builds both JS engines, then pyflakes + unit + JS + pixels + navigation + toe + Go + live smoke
test.cmd           # the same suites in the same order, on Windows
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
  test_win32.py     the Windows window, driven by real messages (Windows only)
  test_units.py     offline unit tests (URL, HTML, CSS, layout, internal pages)
  test_js.py        offline tests for the JS engine + DOM bridge (run twice,
                    once per engine, via FEETBROWSER_JS)
  test_shoes.py     the Shoes theme manager
  test_e2e.py       a fixture page in, its pixels back out
  test_nav.py       click-to-navigate, history, view-source
  download_cases.py downloads, against a local server (run from test_nav.py)
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

`download_cases.py` is the one file here that is not named `test_*.py`, and
that is deliberate: it is a suite of its own — a local HTTP server serving
known lengths, chunked bodies, a connection cut mid-transfer and a shelf of
hostile filenames — but it runs from the end of `test_nav.py` rather than from
a runner, because saving a file is where a navigation ends. Naming it
`test_downloads.py` would put it in front of `test_suites.py` below, which
would then demand a line in `test.sh`, `test.cmd` and the workflow.

`test_suites.py` is the reason a new file in `tests/` cannot be forgotten.
`test.sh`, `test.cmd` and the workflow all name their suites one at a time —
the first two so the order and the comments are readable, the third so a red
job says which suite went red — and this fails if a file in `tests/` is
missing from any of them.

`test.sh` and `test.cmd` run each suite through `tests/watchdog.py`, which
gives it a deadline. Several suites start HTTP servers, open real windows or
reach the network, and any of those can stop forever rather than fail; a run
that hangs reports nothing, and interrupting it prints a traceback from
wherever the interrupt landed rather than from whatever was stuck. The
watchdog arms `faulthandler`'s timer instead, so passing the deadline dumps
every thread's stack — naming the line that hung — and exits non-zero. It is
a timer thread rather than `signal.alarm`, which is what makes it work on
Windows too. `FEETBROWSER_TEST_TIMEOUT` overrides the 900 seconds, and `0`
turns the deadline off for stepping through a suite in a debugger. CI invokes
the suites directly and so runs without it, relying on the job timeout.

The transport layer also has a Go port under `net/`, with its own tests.
`test.sh` runs `go vet ./... && go test ./...` where a Go toolchain is
installed and says so and moves on where there is not; CI always has one.

Nothing here needs a display or a GUI toolkit: the renderer draws into its own
framebuffer, so the whole suite runs headless. `test_render.py` does need at
least one system font, which every platform we support ships.

The exceptions are `test_cocoa.py`, `test_x11.py` and `test_win32.py`, and
deliberately so. They open real windows and feed them real platform events,
because the
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

`test_win32.py` splits the same way, and for the same reason. Its offline
half — DIB stride rounding, the BGRX byte order, virtual-key to keysym
translation, the wheel-delta arithmetic — runs anywhere. Its other half opens
real windows, pumps real messages through the window procedure and reads
pixels back out of GDI, and that half only runs on the `windows-latest` rows
of the matrix. Treat those rows as the verification of anything in `win32.py`:
nothing else executes a line of it, and until they existed nothing ever had.

Two things they do not verify, and it is worth being plain about which. The
runners are headless and run at 96 DPI, so neither the DPI handling nor
`WM_DPICHANGED` is ever exercised; and nothing there drags a window by its
title bar, so the timer that keeps the browser running inside Windows' modal
loops is not exercised either. Those parts are written against the API
documentation and checked by reading.

CI runs the offline suite on every interpreter the engine supports (3.9
through 3.14) and on macOS and Windows as well as Linux, so `test_cocoa.py`
and `test_win32.py` open real windows somewhere rather than only proving
their skips are clean.

One job, `unused-image-libraries`, exists to be the negative. Every other job
runs on a machine with no Pillow and no cairosvg, which means an import of
either would fail there and prove nothing — a browser that cannot reach for a
library and a browser that does not are indistinguishable when the library is
absent. So that job installs both, checks they really are importable, and runs
the suites; `test_units.py` and `test_e2e.py` both assert afterwards that
neither module reached `sys.modules`, the second of them after fetching and
drawing a page with a photograph on it.

The Linux jobs build both engines and run `test_js.py` twice, once against
each, because the point of having two is that they answer the same questions.
The macOS lines run the Rust engine only: Zig 0.14 on that runner image
cannot find the SDK it needs to link against libSystem and fails before it
compiles anything of ours. Locally `test.sh` builds both on macOS perfectly
well, so this is a gap in CI rather than in the engine.

The Windows lines also run the Rust engine only, but for a weaker reason
than the macOS ones: nobody has tried the Zig engine there. It is not known
to fail, it is unbuilt, and `test.cmd` leaves it alone rather than guess.

The Zig engine's own tests are a job of their own. They never cross into
Python, so running them once says as much as running them on all eight
interpreters would, which is the same reason the Rust and Go toolchains have
a job each.
