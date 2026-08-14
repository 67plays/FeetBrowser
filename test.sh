#!/usr/bin/env bash
# Run the FeetBrowser test suite.
#
# The renderer draws into its own framebuffer, so no display, no Tk and no
# toolkit is needed. The JavaScript engine is the Rust extension
# `feetbrowser_engine`, so the suite runs out of the local venv maturin builds
# it into. Three suites step outside all that: test_cocoa.py and test_x11.py
# open real windows wherever their platform has one and skip everywhere else,
# and test_nav.py and smoke.py reach the network. The last of those is why CI
# runs both of them against the offline mirror in tests/fixtures instead --
# see tests/fixture_server.py.
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -x .venv/bin/python ]; then
  python3 -m venv .venv
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
.venv/bin/python tests/test_suites.py  # every file below, and nothing missing
.venv/bin/python tests/test_render.py
.venv/bin/python tests/test_cocoa.py   # opens real windows on macOS, skips elsewhere
.venv/bin/python tests/test_x11.py     # opens real windows under X11, skips elsewhere
.venv/bin/python tests/test_units.py
.venv/bin/python tests/test_js.py
.venv/bin/python tests/test_shoes.py
.venv/bin/python tests/test_e2e.py     # a fixture page in, its pixels back out
.venv/bin/python tests/test_nav.py
.venv/bin/python tests/test_toes.py
.venv/bin/python tests/test_asmblend.py  # raw assembly on Linux/x86-64, Python elsewhere
.venv/bin/python tests/smoke.py

# The Go port of the transport layer (net/) is a separate toolchain, so it is
# run where one is installed and skipped where there is not, the same way the
# window suites treat their platforms.
if command -v go >/dev/null 2>&1; then
  go vet ./...
  go test ./...
else
  echo "skipping the Go net tests: no go toolchain on PATH"
fi
