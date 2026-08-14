#!/usr/bin/env bash
# Run the FeetBrowser test suite.
#
# The renderer draws into its own framebuffer, so no display, no Tk and no
# toolkit is needed. There are two JavaScript engines and the suite builds
# both: the Zig one is a dynamic library loaded with ctypes and is the
# default, the Rust one is a CPython extension maturin builds into the local
# venv. The JS suite then runs against each in turn, because two engines
# behind one contract are only worth having if both are held to it.
#
# Two suites step outside all that: test_cocoa.py opens real windows on macOS
# (and skips everywhere else), and test_nav.py and smoke.py reach the network.
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -x .venv/bin/python ]; then
  python3 -m venv .venv
fi

# The Zig engine: a compiler and nothing else.
(cd zig && zig build)
(cd zig && zig build test)

# The Rust engine, in the venv the rest of the suite runs from.
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
.venv/bin/python tests/test_units.py
FEETBROWSER_JS=zig .venv/bin/python tests/test_js.py
FEETBROWSER_JS=rust .venv/bin/python tests/test_js.py
.venv/bin/python tests/test_shoes.py
.venv/bin/python tests/test_nav.py
.venv/bin/python tests/test_toes.py
.venv/bin/python tests/test_gh_scroll.py
.venv/bin/python tests/smoke.py
