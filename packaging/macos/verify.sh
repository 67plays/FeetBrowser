#!/usr/bin/env bash
# Check that a built FeetBrowser.app is really self-contained.
#
# Run by build.sh on what it just built, and separately by the workflow on
# the copy taken out of a mounted disk image -- which is the copy a user
# actually gets, and the one where a mistake would matter.
#
#   packaging/macos/verify.sh /path/to/FeetBrowser.app
#
# What it refuses to pass:
#
#   * a Mach-O that loads anything from outside the bundle other than the
#     system frameworks in /System and the system libraries in /usr/lib,
#   * a Mach-O missing either architecture,
#   * a file anywhere in the bundle containing a path under /Users, which is
#     how a build machine's home directory ends up shipped to strangers,
#   * a missing icon, plist key, trust store, engine or interpreter,
#   * an app that cannot decode H.264 -- asked of the app itself, with a
#     stripped PATH, because that failure is invisible from a checkout,
#   * Tcl, Tk or _tkinter, which this project does not use and must not ship.
set -euo pipefail

app="${1:?usage: verify.sh /path/to/FeetBrowser.app}"
app="$(cd "$app" && pwd)"
contents="$app/Contents"
fail=0
note() { printf '  %s\n' "$*"; }
bad() { printf 'FAIL: %s\n' "$*"; fail=1; }

machos() {
  find "$1" -type f -print0 | while IFS= read -r -d '' f; do
    case "$(file -b "$f")" in *Mach-O*) printf '%s\n' "$f" ;; esac
  done
}

echo "== otool -L on every Mach-O in $(basename "$app")"
while IFS= read -r file; do
  rel="${file#"$app"/}"
  archs=$(lipo -archs "$file" 2>/dev/null || echo "?")
  printf '\n%s  [%s]\n' "$rel" "$archs"
  case "$archs" in
    *x86_64*) ;;
    *) bad "$rel is missing x86_64 (has: $archs)" ;;
  esac
  case "$archs" in
    *arm64*) ;;
    *) bad "$rel is missing arm64 (has: $archs)" ;;
  esac
  otool -L "$file" | grep '^	' | awk '{print $1}' | sort -u |
  while read -r dep; do
    printf '    %s\n' "$dep"
  done
  # Anything that is not relative to the loader and not a system path would
  # be resolved on the user's machine, where it does not exist.
  outside=$(otool -L "$file" | grep '^	' | awk '{print $1}' | sort -u |
            grep -v -e '^/System/' -e '^/usr/lib/' \
                    -e '^@loader_path/' -e '^@executable_path/' -e '^@rpath/' \
            || true)
  if [ -n "$outside" ]; then
    bad "$rel loads from outside the bundle:"
    printf '       %s\n' $outside
  fi
done < <(machos "$contents")

echo
echo "== contents"
for want in \
  "MacOS/FeetBrowser" \
  "Info.plist" \
  "PkgInfo" \
  "Resources/FeetBrowser.icns" \
  "Resources/certs/cacert.pem" \
  "Frameworks/Python.framework/Versions/3.13/Python" \
  "Resources/lib/feetbrowser/__main__.py" ; do
  if [ -e "$contents/$want" ]; then note "ok  $want"; else bad "missing $want"; fi
done
engine=$(find "$contents/Resources/lib" -name 'feetbrowser_engine*.so' | head -1)
if [ -n "$engine" ]; then
  note "ok  ${engine#"$app"/}"
else
  bad "missing the feetbrowser_engine extension"
fi

for key in CFBundleIdentifier CFBundleName CFBundleExecutable \
           CFBundleIconFile CFBundleShortVersionString ; do
  value=$(/usr/libexec/PlistBuddy -c "Print :$key" "$contents/Info.plist" \
          2>/dev/null || true)
  if [ -n "$value" ]; then note "ok  $key = $value"; else bad "Info.plist has no $key"; fi
done

echo
echo "== video"
# The regression this exists for is invisible from a checkout. h264.py falls
# back to compiling fortran/ with gfortran, which every developer has and no
# user does, so a bundle that shipped no decoder passes every other check
# here, starts, renders, and only admits it when somebody opens a video --
# by which time it is a download. So the app is asked, in the app.
decoder=$(find "$contents/Resources/lib/feetbrowser" -name '_h264_*' -type f | head -1)
if [ -n "$decoder" ]; then note "ok  ${decoder#"$app"/}"; else bad "no prebuilt H.264 decoder in the bundle"; fi
if [ -d "$contents/Resources/lib/fortran" ]; then
  note "ok  Resources/lib/fortran (the sources the decoder's name is a hash of)"
else
  bad "missing Resources/lib/fortran"
fi
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
vector="$here/../../tests/fixtures/h264/mb1.264"
truth="$here/../../tests/fixtures/h264/mb1.i420.z"
args=()
if [ -f "$vector" ] && [ -f "$truth" ]; then
  args=("$vector" "$truth")
else
  note "no test vectors beside this script; checking the decoder loads, not what it decodes"
fi
# PATH cut back to the system directories: if the bundle could only decode
# because this machine has a gfortran or a Homebrew libgfortran, that is the
# bug, and it must not be able to reach either.
if out=$(env PATH=/usr/bin:/bin:/usr/sbin:/sbin \
             "$contents/MacOS/FeetBrowser" --check-video "${args[@]+"${args[@]}"}" 2>&1); then
  printf '  %s\n' "$out"
else
  bad "--check-video failed inside the app:"
  printf '   %s\n' "$out"
fi

echo
echo "== no toolkit"
leftover=$(find "$contents" \( -name 'Tcl*' -o -name 'Tk*' -o -name '_tkinter*' \
           -o -name 'tkinter' \) -print || true)
if [ -n "$leftover" ]; then bad "Tcl/Tk found:"; printf '   %s\n' $leftover
else note "no Tcl, Tk or _tkinter"; fi

echo
echo "== no build-machine paths"
# grep -l over the whole bundle, binaries included: a home directory left in
# a .pyc or a debug symbol is as much of a leak as one left in a text file.
#
# What is being looked for is *this* machine's home directory and user name,
# not the string /Users. Upstream CPython's own artifacts carry the paths of
# the release manager's own build machine inside _sysconfigdata and in the
# debug sections of lib-dynload, and rewriting
# somebody else's published binaries to hide that would be both futile and
# rude. Those are listed below rather than failed on; anything belonging to
# whoever ran this script is a failure.
leak=0
for needle in "$HOME" "/Users/$(id -un)" "$(cd "$app/../.." && pwd)"; do
  [ -n "$needle" ] || continue
  hits=$(grep -rl --binary-files=text -F "$needle" "$contents" 2>/dev/null || true)
  if [ -n "$hits" ]; then
    bad "these files mention $needle:"
    printf '   %s\n' $hits
    leak=1
  fi
done
[ "$leak" -eq 0 ] && note "no file mentions this machine's home directory"
others=$(grep -rhoa --binary-files=text -E '/Users/[A-Za-z0-9_.-]+' "$contents" \
         2>/dev/null | sort -u || true)
if [ -n "$others" ]; then
  note "paths under /Users that came in with upstream CPython:"
  printf '     %s\n' $others
fi

echo
if [ "$fail" -ne 0 ]; then
  echo "verify.sh: FAILED"
  exit 1
fi
echo "verify.sh: the bundle stands alone"
