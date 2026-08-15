#!/usr/bin/env bash
# Launch FeetBrowser. No GUI toolkit needed -- rendering is our own, and the
# only outside requirement is a system font; every platform we support ships
# one. The JavaScript engine is the Rust extension `feetbrowser_engine`,
# which maturin builds into a local venv the first time you run this -- so
# the first start compiles Rust and says so, and no later one does.
set -euo pipefail
cd "$(dirname "$0")"

if python3 -c "import feetbrowser_engine" 2>/dev/null; then
  exec python3 -m feetbrowser "$@"
fi
# Otherwise the venv is what runs the browser, so ask the venv -- and not
# the system python -- whether the engine is there, and whether it still
# matches rust/. A stale extension starts up perfectly happily and then
# misbehaves in ways that look like page bugs rather than build problems,
# so sources newer than the engine count the same as no engine at all.
# Unseal a venv made before the line below grew --system-site-packages. A
# default venv cannot see the system's site-packages, which is where an
# optional package like curl_cffi lives, and a venv is only ever created
# once -- so without this the fix reaches fresh checkouts and nobody who
# already hit the bug. Re-running venv over an existing directory rewrites
# pyvenv.cfg and leaves everything installed in it exactly where it was.
if [ -f .venv/pyvenv.cfg ] &&
   grep -qi '^include-system-site-packages *= *false' .venv/pyvenv.cfg; then
  python3 -m venv --system-site-packages .venv
fi

engine=""
if [ -x .venv/bin/python ]; then
  engine=$(.venv/bin/python -c "import feetbrowser_engine as e; print(e.__file__)" 2>/dev/null || true)
fi
if [ -z "$engine" ]; then
  building=first
elif [ -n "$(find rust/src rust/Cargo.toml -newer "$engine" 2>/dev/null | head -1)" ]; then
  building=again
else
  building=
fi

if [ -n "$building" ]; then
  # Say what is about to happen before it happens. Someone who typed
  # ./run.sh expecting a browser and got several minutes of cargo output
  # has every reason to think they are in the wrong repository.
  if ! command -v cargo >/dev/null 2>&1; then
    cat >&2 <<'NORUST'
FeetBrowser needs a Rust toolchain to run its Rust JavaScript engine, and
there is not one on this machine.

That engine is a Rust extension (see rust/), not Python, so it has to be
compiled before the browser can start.

Install Rust with rustup -- one command, no root, nothing outside your home
directory:

    curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh

Other ways to install it, including your distribution's packages, are at
https://rustup.rs. Open a new shell afterwards so cargo is on your PATH, then
run this script again.
NORUST
    exit 1
  fi
  if [ "$building" = first ]; then
    cat <<'BUILDING'
FeetBrowser: building the Rust JavaScript engine before the first start.

It is a Rust extension rather than Python, so it has to be compiled. maturin
does it, into a virtualenv in this directory. Expect a minute or two, longer
on a slow machine or a cold cargo cache -- and only this once. Every later
start skips straight past this.

BUILDING
  else
    cat <<'REBUILDING'
FeetBrowser: rebuilding the Rust JavaScript engine, which is older than rust/.

Something under rust/ has changed since the engine in this directory was
compiled -- a git pull, most likely. Starting the old one would be quicker and
would go wrong in ways that look like the page's fault, so it gets rebuilt.

REBUILDING
  fi
  # --system-site-packages, because the venv exists to hold one compiled
  # extension and must not hide anything else. Images no longer need this --
  # every format we draw is decoded in that extension -- but net.py will use
  # curl_cffi for the impersonating fetch when the machine has it, and a
  # sealed venv silently takes that away.
  python3 -m venv --system-site-packages .venv
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
