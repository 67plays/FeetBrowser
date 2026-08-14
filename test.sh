#!/usr/bin/env bash
# Run the FeetBrowser test suite.
#
# The renderer draws into its own framebuffer, so no display, no Tk and no
# toolkit is needed. There are two JavaScript engines and the suite builds
# both: the Zig one is a dynamic library loaded with ctypes, the Rust one is
# a CPython extension maturin builds into the local venv. The JS suite then
# runs against each in turn, because two engines behind one contract are only
# worth having if both are held to it.
#
# Three suites step outside all that: test_cocoa.py and test_x11.py open real
# windows wherever their platform has one and skip everywhere else, and
# test_nav.py and smoke.py reach the network.
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -x .venv/bin/python ]; then
  python3 -m venv .venv
fi

# The Zig engine: a compiler and nothing else. `zig build` is a no-op when
# nothing under zig/ has moved, and rebuilds when it has, so the library next
# to the tests is always the one the sources describe. A stale libfeetjs is
# the same trap as a stale extension module below.
(cd zig && zig build)
(cd zig && zig build test)

# The Rust engine, in the venv the rest of the suite runs from, rebuilt
# whenever rust/ has moved on since. Importing it successfully is not enough.
# An extension compiled from an older tree runs perfectly well and fails the
# tests that the newer tree added, which reads as "your branch is broken" when
# the truth is "your venv is old" -- and it is the tests of the DOM bridge,
# whose Python and Rust halves have to agree, that go first.
engine=$(.venv/bin/python -c "import feetbrowser_engine as e; print(e.__file__)" 2>/dev/null || true)
if [ -z "$engine" ] || [ -n "$(find rust/src rust/Cargo.toml -newer "$engine" 2>/dev/null | head -1)" ]; then
  .venv/bin/pip install -q maturin
  .venv/bin/maturin develop --release --manifest-path rust/Cargo.toml
fi

if ! .venv/bin/python -c "import pyflakes" 2>/dev/null; then
  .venv/bin/pip install -q pyflakes
fi

.venv/bin/python -m pyflakes feetbrowser tests
.venv/bin/python tests/test_render.py
.venv/bin/python tests/test_cocoa.py   # opens real windows on macOS, skips elsewhere
.venv/bin/python tests/test_x11.py     # opens real windows under X11, skips elsewhere
.venv/bin/python tests/test_units.py
FEETBROWSER_JS=zig .venv/bin/python tests/test_js.py
FEETBROWSER_JS=rust .venv/bin/python tests/test_js.py
.venv/bin/python tests/test_shoes.py
.venv/bin/python tests/test_nav.py
.venv/bin/python tests/test_toes.py
.venv/bin/python tests/test_asmblend.py  # raw assembly on Linux/x86-64, Python elsewhere
.venv/bin/python tests/smoke.py
