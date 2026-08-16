#!/usr/bin/env bash
# Run the FeetBrowser test suite.
#
# The renderer draws into its own framebuffer, so no display and no toolkit is
# needed. The JavaScript engine is the Rust one: a CPython extension maturin
# builds into the local venv, and the JS suite runs against it like every
# other suite.
#
# Five suites step outside all that: test_cocoa.py, test_x11.py and
# test_win32.py put the browser in a real window wherever their platform has
# one and skip everywhere else, and test_nav.py and smoke.py reach the
# network. The last of those is why CI runs both of them against the offline
# mirror in tests/fixtures instead -- see tests/fixture_server.py.
#
# On Windows, run test.cmd instead; it runs the same suites in the same order.
set -euo pipefail
cd "$(dirname "$0")"

# Those window suites open real windows, and a window's default manners --
# centre itself, raise above everything, take the keyboard -- make the machine
# unusable for as long as the run lasts. QUIET drops exactly those three
# things and nothing else: the windows are still created, mapped, drawn into
# and sent real events, so the suites still prove what they proved before.
# The variable is doormat's, since the windows are, and it is exported rather
# than assigned so it reaches the suites. An existing value wins, so
# `DOORMAT_QUIET=0 ./test.sh` still gets you the windows to watch.
export DOORMAT_QUIET="${DOORMAT_QUIET:-1}"

if [ ! -x .venv/bin/python ]; then
  # Same venv run.sh builds, and for the same reason it is not sealed. That
  # reason used to be the image decoders and is not any more -- we decode our
  # own -- but curl_cffi is still optional and still lives in the system
  # python, and the impersonating fetch in net.py is the one thing a sealed
  # venv would quietly take away from the tests that exercise it.
  python3 -m venv --system-site-packages .venv
elif grep -qi '^include-system-site-packages *= *false' .venv/pyvenv.cfg 2>/dev/null; then
  # A venv from before that flag was added is sealed, and a venv is only ever
  # created once, so those tests would keep running without curl_cffi on every
  # machine that already has one. Re-running venv over it rewrites pyvenv.cfg
  # and leaves what is installed inside untouched.
  python3 -m venv --system-site-packages .venv
fi

# Our own split-out libraries, in the same venv: feetplayer, which is the
# decoders and the container readers, and doormat, which is the windows. Both
# used to live in this tree and are their own repositories now.
# requirements.txt pins each to a commit; installing feetplayer compiles its
# Fortran, which takes a minute, so the pins already installed are compared
# against the pins asked for and nothing is done when they agree. `pip freeze`
# prints a VCS install as the requirement line that produced it, which is why
# the comparison is a plain string match.
#
# Every line has to be checked, not just one of them: a single `grep -qxF`
# against the whole file passes as soon as ANY pin matches, which would leave
# a newly added dependency permanently uninstalled.
have=$(.venv/bin/python -m pip freeze 2>/dev/null || true)
missing=""
while IFS= read -r want; do
  printf '%s\n' "$have" | grep -qxF "$want" || missing=1
done <<EOF
$(grep -v '^[[:space:]]*#' requirements.txt | grep -v '^[[:space:]]*$')
EOF
if [ -n "$missing" ]; then
  .venv/bin/python -m pip install -q -r requirements.txt
fi

# The Rust engine, in the venv the rest of the suite runs from, rebuilt
# whenever rust/ has moved on since. Importing it successfully is not enough.
# An extension compiled from an older tree runs perfectly well and fails the
# tests that the newer tree added, which reads as "your branch is broken" when
# the truth is "your venv is old" -- and it is the tests of the DOM bridge,
# whose Python and Rust halves have to agree, that go first.
engine=$(.venv/bin/python -c "import feetbrowser_engine as e; print(e.__file__)" 2>/dev/null || true)
if [ -z "$engine" ] || [ -n "$(find rust/src rust/Cargo.toml -newer "$engine" 2>/dev/null | head -1)" ]; then
  .venv/bin/pip install -q maturin
  .venv/bin/maturin develop --release --manifest-path rust/Cargo.toml
fi

# The Rust half has 60 tests of its own -- the regexp engine, the CSS
# matcher, the layout reproductions. They ran nowhere: CI only ever did
# `cargo check --all-targets`, which compiles a test without running it. They
# cost hundredths of a second, so they run here too rather than only on a
# machine that happens to type `cargo test` by hand. The tokenizer and tree
# builder are not among them any more; they are `footnote`'s, and it runs
# its own suite against three platforms.
if command -v cargo >/dev/null 2>&1; then
  cargo test -q --manifest-path rust/Cargo.toml
fi

if ! .venv/bin/python -c "import pyflakes" 2>/dev/null; then
  .venv/bin/pip install -q pyflakes
fi

.venv/bin/python -m pyflakes feetbrowser tests

# Every suite below runs behind a deadline. Several of them start HTTP
# servers, open real windows or reach the network, and any of those can stop
# forever rather than fail -- at which point the run says nothing at all. The
# watchdog turns that into every thread's stack and a non-zero exit. See
# tests/watchdog.py; FEETBROWSER_TEST_TIMEOUT overrides the number.
run=".venv/bin/python tests/watchdog.py 900"

$run tests/test_suites.py  # every file below, and nothing missing
$run tests/test_discord.py  # the from-scratch Discord Rich Presence client
$run tests/test_render.py
$run tests/test_cocoa.py   # the browser in a real macOS window, else skips
$run tests/test_x11.py     # the browser in a real X11 window, else skips
$run tests/test_win32.py   # the browser in a real Windows window, else skips
$run tests/test_audio.py   # a <video> element's soundtrack, and the pictures that follow it
$run tests/test_units.py
$run tests/test_release_version.py  # the guard release.yml runs first
$run tests/test_js.py
$run tests/test_shoes.py
$run tests/test_settings.py
$run tests/test_e2e.py     # a fixture page in, its pixels back out
$run tests/test_nav.py
$run tests/test_toes.py
$run tests/test_asmselect.py # the selection nearest-boundary kernel
$run tests/smoke.py


