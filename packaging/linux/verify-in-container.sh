#!/bin/sh
# The acceptance test, run inside a clean container that is not the one the
# AppImage was built in.
#
# The claim being tested is "a person downloads one file, makes it executable
# and runs it, on a distribution that has no Python". So this refuses to run
# if a python3 is on PATH, and it installs exactly two packages:
#
#   libx11-6   the X client library. x11.py dlopens libX11.so.6 through
#              ctypes and the bundle deliberately does not carry one, so this
#              stands in for what any machine with an X server already has.
#   xvfb       an X server. A user's desktop is the X server; a container has
#              to be given one.
#
# Nothing else. In particular NOT ca-certificates and NOT any font package,
# because the bundled CA fallback and the bundled DejaVu are exactly what
# this is here to prove.
set -eu

SOURCE=${1:?usage: verify-in-container.sh /path/to.AppImage}
# Copied out of the read-only mount first, because what a user has is a file
# in their own downloads directory that they chmod +x themselves.
IMAGE=/tmp/FeetBrowser.AppImage
cp "$SOURCE" "$IMAGE"
OUT=${OUT:-/out}
TESTS=${TESTS:-/verify/tests}
mkdir -p "$OUT"

say() { printf '\n--- %s ---\n' "$1"; }

say "the machine"
cat /etc/os-release | sed -n '1,2p'
ldd --version | head -1
if command -v python3 >/dev/null 2>&1 || command -v python >/dev/null 2>&1; then
  echo "FAIL: this container has a Python; it is not the test we meant to run" >&2
  exit 1
fi
echo "no python on PATH: $(command -v python3 || echo confirmed)"

say "packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq --no-install-recommends libx11-6 xvfb >/dev/null
echo "installed: libx11-6 xvfb"
ls /usr/share/fonts 2>/dev/null | grep . && echo "note: this image has fonts" \
  || echo "no fonts installed on this system"
ls /etc/ssl/certs/ca-certificates.crt 2>/dev/null \
  || echo "no system CA bundle on this system"

say "the one file"
chmod +x "$IMAGE"
if ! "$IMAGE" --version >/dev/null 2>&1; then
  # FUSE is how an AppImage mounts itself, and a container usually has no
  # /dev/fuse. The runtime's own fallback unpacks to a temporary directory
  # instead. A desktop has FUSE; this is a property of the container.
  echo "no FUSE here; using the runtime's extract-and-run fallback"
  export APPIMAGE_EXTRACT_AND_RUN=1
fi
"$IMAGE" --version

say "https, with no system CA store"
# The first https:// fetch is where a bundled Python usually falls over: its
# OpenSSL was compiled with a trust-store path that does not exist here.
"$IMAGE" --screenshot https://example.com "$OUT/https-live.png"
ls -l "$OUT/https-live.png"

say "a local page, with no system fonts"
# Every glyph on this page comes out of the DejaVu faces inside the bundle;
# this container has no font packages at all. The <img> tags in the fixture
# point at /swatch.png, an absolute path that resolves against the filesystem
# root under file:// and so is legitimately missing -- the alt text in the
# screenshot is the correct answer, not a packaging failure. The next step
# covers images properly.
"$IMAGE" --screenshot "file://$TESTS/fixtures/pixels.html" "$OUT/fixture.png"
ls -l "$OUT/fixture.png"

say "images, decoded by the bundled engine"
# Same fixture images, reached by a relative path so they actually load. This
# is the compiled feetbrowser_engine extension doing the work, which is the
# part of the bundle that a stranger's machine cannot supply for itself.
mkdir -p /tmp/page
cp "$TESTS/fixtures/swatch.png" "$TESTS/fixtures/dot.gif" /tmp/page/
cat > /tmp/page/images.html <<'HTML'
<!doctype html><title>Images</title>
<body style="background:#fff">
<img src="swatch.png" alt="PNGSWATCHALT" width="200" height="120">
<img src="dot.gif" alt="GIFDOTALT" width="120" height="120">
HTML
"$IMAGE" --screenshot file:///tmp/page/images.html "$OUT/images.png"
ls -l "$OUT/images.png"

say "video, on a machine with no compiler"
# The one part of the browser that used to be missing from every shipped copy
# and from no checkout. h264.py falls back to compiling fortran/ with
# gfortran, which every developer has and this container does not, so a
# bundle that shipped no decoder would pass every other check above, start,
# render, and only admit it to a user who opened a video. --check-video
# decodes tests/fixtures/h264/mb1.264 inside the bundle and compares the
# result with the picture a reference decoder produced, byte for byte: a
# decoder that loads and returns rubbish is not a decoder.
command -v gfortran >/dev/null 2>&1 \
  && { echo "FAIL: this container has a gfortran; the test would prove nothing" >&2; exit 1; }
"$IMAGE" --check-video "$TESTS/fixtures/h264/mb1.264" "$TESTS/fixtures/h264/mb1.i420.z"

say "a real X11 window"
# tests/x11_shot.py is the repository's own end-to-end window check: it opens
# a genuine window, paints a page into it, reads the pixels back off the X
# server with XGetImage, and fails unless the red, green and blue swatches
# are present and land in that order across the window. Running it through
# the bundle's own interpreter is the point -- sys.path[0] is /verify, which
# has no feetbrowser in it, so the package, the engine, the fonts and the
# interpreter all come out of the AppImage.
Xvfb :99 -screen 0 1600x1200x24 >/dev/null 2>&1 &
XVFB=$!
sleep 2
DISPLAY=:99 FEETBROWSER_DISPLAY=x11 "$IMAGE" --python \
  "$TESTS/x11_shot.py" "$OUT/x11-window.png"
kill "$XVFB" 2>/dev/null || true

say "the applications menu"
"$IMAGE" --install
DESKTOP="$HOME/.local/share/applications/feetbrowser.desktop"
test -s "$DESKTOP" || { echo "FAIL: no .desktop file" >&2; exit 1; }
test -s "$HOME/.local/share/icons/hicolor/256x256/apps/feetbrowser.png" \
  || { echo "FAIL: no icon" >&2; exit 1; }
grep -q '^MimeType=.*x-scheme-handler/https' "$DESKTOP" \
  || { echo "FAIL: not registered for https:// links" >&2; exit 1; }
cat "$DESKTOP"

say "nothing from the build machine is inside the shipped file"
# The build runs under /build and /io, so an absolute /home or /Users path in
# the payload could only have come from the machine that did the build -- a
# wrong path at runtime, and somebody's directory name published with it.
# Checked here rather than only at build time because this is the file people
# actually download, and squashfs is opaque to `strings` until it is unpacked.
cd /tmp
rm -rf /tmp/squashfs-root
"$IMAGE" --appimage-extract >/dev/null
if grep -rla -E '/(home|Users)/[A-Za-z0-9_.-]+/' /tmp/squashfs-root 2>/dev/null \
   | grep -v '/share/feetbrowser/ca/' | head -5 | grep .; then
  echo "FAIL: a build path is baked into the artifact" >&2
  exit 1
fi
echo "no build paths in the payload"
rm -rf /tmp/squashfs-root

say "nothing was written inside the bundle"
# An AppImage is mounted read-only, so anything the browser writes at startup
# has to land in $HOME. If it tried the bundle instead the run above would
# have failed; this is the positive half of that.
ls -a "$HOME" | grep feetbrowser || echo "(nothing yet, which is also fine)"

say "PASS"
