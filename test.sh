#!/usr/bin/env bash
# Run the FeetBrowser test suite.
#
# The renderer draws into its own framebuffer, so no display, no Tk and no
# toolkit is needed. The JavaScript engine is the Rust extension
# `feetbrowser_engine`, so the suite runs out of the local venv maturin builds
# it into. A few suites step outside all that: test_cocoa.py, test_x11.py and
# test_win32.py open real windows wherever their platform has one and skip
# everywhere else, and test_nav.py and smoke.py reach the network.
#
# On Windows, run test.cmd instead; it runs the same suites in the same order.
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -x .venv/bin/python ]; then
  # Same venv run.sh builds, and for the same reason it is not sealed: the
  # optional image decoders (Pillow, cairosvg) live in the system python, and
  # tests that run without them are not testing what a user runs.
  python3 -m venv --system-site-packages .venv
elif grep -qi '^include-system-site-packages *= *false' .venv/pyvenv.cfg 2>/dev/null; then
  # A venv from before that flag was added is sealed, and a venv is only ever
  # created once, so the tests would keep running without the decoders on
  # every machine that already has one. Re-running venv over it rewrites
  # pyvenv.cfg and leaves what is installed inside untouched.
  python3 -m venv --system-site-packages .venv
fi

# Ensure the Rust JS engine (feetbrowser_engine) is built in the local venv,
# and rebuilt whenever rust/ has moved on since. Importing it successfully is
# not enough. An extension compiled from an older tree runs perfectly well and
# fails the tests that the newer tree added, which reads as "your branch is
# broken" when the truth is "your venv is old" -- and it is the tests of the
# DOM bridge, whose Python and Rust halves have to agree, that go first.
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
.venv/bin/python tests/test_win32.py   # opens real windows on Windows, skips elsewhere
.venv/bin/python tests/test_units.py
.venv/bin/python tests/test_js.py
.venv/bin/python tests/test_shoes.py
.venv/bin/python tests/test_nav.py
.venv/bin/python tests/test_toes.py
.venv/bin/python tests/test_asmblend.py  # raw assembly on Linux/x86-64, Python elsewhere
.venv/bin/python tests/smoke.py
