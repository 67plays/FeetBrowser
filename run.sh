#!/usr/bin/env bash
# Launch FeetBrowser. No GUI toolkit needed -- rendering is our own, and the
# only outside requirement is a system font; every platform we support ships
# one.
#
# The JavaScript engine is ours too, written in Zig and built as a plain
# dynamic library the browser loads with ctypes. That means the system
# python3 can run the whole browser: no venv, no extension module, nothing
# tied to a Python version. Set FEETBROWSER_JS=rust to run the Rust engine
# instead, which is a CPython extension and so does need the venv maturin
# builds it into.
set -euo pipefail
cd "$(dirname "$0")"

if [ "${FEETBROWSER_JS:-zig}" = "rust" ]; then
  if python3 -c "import feetbrowser_engine" 2>/dev/null; then
    exec python3 -m feetbrowser "$@"
  fi
  # The venv is what runs the browser, so ask the venv -- and not the system
  # python -- whether the engine is there.
  if [ ! -x .venv/bin/python ] || ! .venv/bin/python -c "import feetbrowser_engine" 2>/dev/null; then
    python3 -m venv .venv
    .venv/bin/pip install -q maturin
    .venv/bin/maturin develop --release --manifest-path rust/Cargo.toml
  fi
  exec .venv/bin/python -m feetbrowser "$@"
fi

# Cheap when nothing changed, and it keeps the library honest about the
# sources next to it.
(cd zig && zig build)
exec python3 -m feetbrowser "$@"
