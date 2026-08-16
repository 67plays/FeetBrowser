#!/usr/bin/env bash
# Launch FeetBrowser. No GUI toolkit needed -- rendering is our own, and the
# only outside requirement is a system font; every platform we support ships
# one. The JavaScript engine is the Rust extension `feetbrowser_engine`,
# which maturin builds into a local venv the first time you run this -- so
# the first start compiles Rust and says so, and no later one does.
set -euo pipefail
cd "$(dirname "$0")"

# H.264, AAC and MP3 are decoded by Fortran, in the feetplayer package (see
# requirements.txt), where pip compiles them during the install. They are
# what video and audio run on, so a machine that cannot produce them gets
# told so up front rather than starting a browser that quietly plays
# nothing. The check is on the decoders, not on the compiler: a packaged
# build ships them prebuilt, and a gfortran that is present but builds badly
# fails the same way as one that was never installed -- warm_fortran lets
# each decoder say which it was.
warm_fortran() {
  # Load them here rather than on the first <video>: the stall happens once,
  # at startup, instead of in the middle of a page.
  "$1" -c "import feetplayer.h264 as v, feetplayer.aac as a
v.available(); a.available()" >/dev/null 2>&1 || true
  # Require them. No decoder means no audio or video at all.
  if ! "$1" -c "import sys
import feetplayer.h264 as v, feetplayer.aac as a
sys.exit(0 if (v.available() and a.available()) else 1)" >/dev/null 2>&1; then
    {
      cat <<'NOFORTRAN'
FeetBrowser: no H.264 or AAC decoder is available on this machine.

Audio and video run on the decoders in feetplayer, which are Fortran and
compiled by gfortran when pip installs the package (see requirements.txt).
This machine has neither a working decoder nor a compiler to build one, so
the browser would start and quietly play nothing.

NOFORTRAN
      "$1" -c "import feetplayer.h264 as v, feetplayer.aac as a
print('  H.264:', v.unavailable_reason() or 'no decoder')
print('  AAC:  ', a.unavailable_reason() or 'no decoder')" 2>/dev/null || true
      cat <<'NOFORTRAN2'
Install gfortran:
    Debian/Ubuntu:  sudo apt install gfortran
    Fedora/RHEL:    sudo dnf install gcc-gfortran
    openSUSE:       sudo zypper install gcc-fortran
    Arch:           sudo pacman -S gcc-fortran
    macOS:          brew install gcc
    Windows:        install MinGW-w64, which ships gfortran

then delete .venv/lib/*/site-packages/feetplayer and run this script again,
so pip rebuilds them. Or take a packaged build, which carries the decoders
inside it.
NOFORTRAN2
    } >&2
    exit 1
  fi
}

# The system python runs the browser only if it has both halves of it: the
# Rust engine and the media stack. Before feetplayer was its own repository
# the second half was these files, so having the engine was the whole test;
# now it is a package, and a system python with the engine but no feetplayer
# would give you a browser that plays no video. The venv below installs it.
if python3 -c "import feetbrowser_engine, feetplayer" 2>/dev/null; then
  warm_fortran python3
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

# Our own split-out libraries, each pinned to a commit in requirements.txt,
# into that venv: feetplayer is the decoders, doormat is the windows.
# Installing feetplayer compiles its Fortran, so this is the minute the
# decoders cost, and it is paid once per pin rather than once per start: `pip
# freeze` prints a VCS install as the requirement line that produced it, so
# the pins installed and the pins asked for compare as plain strings. Every
# line is checked -- one `grep -qxF` over the whole file would pass as soon as
# any single pin matched, leaving a newly added dependency uninstalled.
#
# This, and the decoder check after it, come before the engine build below --
# a machine that cannot decode is told why now, not after several minutes of
# cargo. The venv has to exist first, which is the only reason it is created
# here rather than there.
if [ ! -x .venv/bin/python ]; then
  python3 -m venv --system-site-packages .venv
fi
have=$(.venv/bin/python -m pip freeze 2>/dev/null || true)
missing=""
while IFS= read -r want; do
  printf '%s\n' "$have" | grep -qxF "$want" || missing=1
done <<EOF
$(grep -v '^[[:space:]]*#' requirements.txt | grep -v '^[[:space:]]*$')
EOF
if [ -n "$missing" ]; then
  echo "FeetBrowser: installing feetplayer and doormat, the media stack and"
  echo "the windows. feetplayer compiles its Fortran decoders, so expect a"
  echo "minute -- and only when a pin moves."
  echo
  # Not fatal on its own: warm_fortran below is what refuses to start, and it
  # says rather more about why than pip does.
  .venv/bin/python -m pip install -q -r requirements.txt || true
fi

warm_fortran .venv/bin/python

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
