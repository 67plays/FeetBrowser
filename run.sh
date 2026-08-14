#!/usr/bin/env bash
# Launch FeetBrowser. No GUI toolkit needed -- rendering is our own, and the
# only outside requirement is a system font; every platform we support ships
# one. The JavaScript engine is the Rust extension `feetbrowser_engine`,
# which maturin builds into a local venv the first time you run this -- so
# the first start compiles Rust and says so, and no later one does.
set -euo pipefail
cd "$(dirname "$0")"

# Already importable (someone installed it system-wide)? Just run.
if python3 -c "import feetbrowser_engine" 2>/dev/null; then
  exec python3 -m feetbrowser "$@"
fi

# Otherwise the venv is what runs the browser, so ask the venv -- and not the
# system python -- whether the engine is there.
if [ ! -x .venv/bin/python ] || ! .venv/bin/python -c "import feetbrowser_engine" 2>/dev/null; then
  # Say what is about to happen before it happens. Someone who typed ./run.sh
  # expecting a browser and got several minutes of cargo output has every
  # reason to think they are in the wrong repository.
  if ! command -v cargo >/dev/null 2>&1; then
    cat >&2 <<'NORUST'
FeetBrowser needs a Rust toolchain, and there is not one on this machine.

The JavaScript engine is a Rust extension (see rust/), not Python, so it has
to be compiled before the browser can start. There is no pure-Python
fallback.

Install Rust with rustup -- one command, no root, nothing outside your home
directory:

    curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh

Other ways to install it, including your distribution's packages, are at
https://rustup.rs. Open a new shell afterwards so cargo is on your PATH, then
run this script again.
NORUST
    exit 1
  fi
  cat <<'BUILDING'
FeetBrowser: building the JavaScript engine before the first start.

It is a Rust extension rather than Python, so it has to be compiled. maturin
does it, into a virtualenv in this directory. Expect a minute or two, longer
on a slow machine or a cold cargo cache -- and only this once. Every later
start skips straight past this.

BUILDING
  python3 -m venv .venv
  .venv/bin/pip install -q maturin
  if ! .venv/bin/maturin develop --release --manifest-path rust/Cargo.toml; then
    cat >&2 <<'FAILED'

FeetBrowser: the JavaScript engine did not build.

The compiler's own output is above and says what went wrong. If it is about a
missing linker or C toolchain, that is the system side of a Rust install:
build-essential on Debian and Ubuntu, "xcode-select --install" on macOS.
FAILED
    exit 1
  fi
  echo
  echo "FeetBrowser: engine built. Starting."
fi

exec .venv/bin/python -m feetbrowser "$@"
