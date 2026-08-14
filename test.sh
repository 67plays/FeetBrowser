#!/usr/bin/env bash
# Run the FeetBrowser test suite.
#
# Everything here is headless: the renderer draws into its own framebuffer, so
# no display, no Tk, no toolkit. test_nav.py and smoke.py reach the network.
set -euo pipefail
cd "$(dirname "$0")"

run() {
  python3 -m pyflakes feetbrowser tests
  python3 tests/test_render.py
  python3 tests/test_units.py
  python3 tests/test_js.py
  python3 tests/test_nav.py
  python3 tests/test_toes.py
  python3 tests/test_gh_scroll.py
  python3 tests/smoke.py
}

if python3 -c "import pyflakes" 2>/dev/null; then
  run
elif command -v nix-shell >/dev/null 2>&1; then
  nix-shell -p "python3.withPackages(ps: with ps; [ pyflakes ])" \
    --run "$(declare -f run); run"
else
  echo "Need pyflakes: python3 -m pip install pyflakes" >&2
  exit 1
fi
