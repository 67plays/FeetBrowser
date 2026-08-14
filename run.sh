#!/usr/bin/env bash
# Launch FeetBrowser. No GUI toolkit needed -- rendering is our own, and the
# only outside requirement is a system font; every platform we support ships
# one. The JavaScript engine is the Rust extension `feetbrowser_engine`,
# which maturin builds into a local venv the first time you run this.
set -euo pipefail
cd "$(dirname "$0")"

# Already importable (someone installed it system-wide)? Just run.
if python3 -c "import feetbrowser_engine" 2>/dev/null; then
  exec python3 -m feetbrowser "$@"
fi

# Otherwise the venv is what runs the browser, so ask the venv -- and not the
# system python -- whether the engine is there.
if [ ! -x .venv/bin/python ] || ! .venv/bin/python -c "import feetbrowser_engine" 2>/dev/null; then
  python3 -m venv .venv
  .venv/bin/pip install -q maturin
  .venv/bin/maturin develop --release --manifest-path rust/Cargo.toml
fi

exec .venv/bin/python -m feetbrowser "$@"
