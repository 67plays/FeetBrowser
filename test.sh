#!/usr/bin/env bash
# Run the FeetBrowser test suite. Uses nix-shell for Tk on NixOS.
# not designed to run on everything
set -euo pipefail
cd "$(dirname "$0")"

# Ensure the Rust JS engine (feetbrowser_engine) is built in the local venv.
if ! python3 -c "import feetbrowser_engine" 2>/dev/null; then
  if [ ! -x .venv/bin/python ]; then
    python3 -m venv .venv
  fi
  .venv/bin/pip install -q maturin
  .venv/bin/maturin develop --release --manifest-path rust/Cargo.toml
fi

run() {
  .venv/bin/python -m pyflakes feetbrowser tests
  .venv/bin/python tests/test_units.py
  .venv/bin/python tests/test_js.py
  .venv/bin/python tests/test_shoes.py
  .venv/bin/python tests/test_nav.py
  .venv/bin/python tests/test_toes.py
  .venv/bin/python tests/test_asmblend.py
  .venv/bin/python tests/smoke.py
}

if .venv/bin/python -c "import tkinter" 2>/dev/null && .venv/bin/python -c "import pyflakes" 2>/dev/null; then
  run
else
  nix-shell -p "python3.withPackages(ps: with ps; [ tkinter pyflakes ])" \
    --run "$(declare -f run); run"
fi