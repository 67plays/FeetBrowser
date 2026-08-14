#!/usr/bin/env bash
# Run the FeetBrowser test suite.
#
# The renderer draws into its own framebuffer, so no display, no Tk and no
# toolkit is needed. The JavaScript engine is the Rust extension
# `feetbrowser_engine`, so the suite runs out of the local venv maturin builds
# it into. Three suites step outside all that: test_cocoa.py and test_x11.py
# open real windows wherever their platform has one and skip everywhere else,
# and test_nav.py and smoke.py reach the network.
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -x .venv/bin/python ]; then
  python3 -m venv .venv
fi

# Ensure the Rust JS engine (feetbrowser_engine) is built in the local venv.
if ! .venv/bin/python -c "import feetbrowser_engine" 2>/dev/null; then
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
.venv/bin/python tests/test_js.py
.venv/bin/python tests/test_shoes.py
.venv/bin/python tests/test_nav.py
.venv/bin/python tests/test_toes.py
.venv/bin/python tests/test_asmblend.py  # raw assembly on Linux/x86-64, Python elsewhere
.venv/bin/python tests/smoke.py
