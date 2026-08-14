#!/usr/bin/env bash
# Launch FeetBrowser. The JS engine is the Rust extension `feetbrowser_engine`
# (built with maturin into a local venv). On NixOS this pulls a Python with
# Tk on the fly. NixOS is only tested platform
set -euo pipefail
cd "$(dirname "$0")"

# Ensure the Rust JS engine is built and importable.
if ! python3 -c "import feetbrowser_engine" 2>/dev/null; then
  if [ ! -x .venv/bin/python ]; then
    python3 -m venv .venv
  fi
  .venv/bin/pip install -q maturin
  .venv/bin/maturin develop --release --manifest-path rust/Cargo.toml
fi

if .venv/bin/python -c "import tkinter" 2>/dev/null; then
  exec .venv/bin/python -m feetbrowser "$@"
elif command -v nix-shell >/dev/null 2>&1; then
  exec nix-shell -p "python3.withPackages(ps: with ps; [ tkinter ])" \
    --run ".venv/bin/python -m feetbrowser $*"
else
  echo "Need Python with tkinter. Install python3-tk (Debian/Ubuntu)," >&2
  echo "python3-tkinter (Fedora), or tk (Arch), then re-run." >&2
  exit 1
fi