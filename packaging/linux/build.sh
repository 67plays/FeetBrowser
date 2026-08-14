#!/usr/bin/env bash
# Build the Linux AppImage, from anywhere Docker runs.
#
# All this does is put build-appimage.sh inside the container it has to run
# in and hand it the checkout; the build itself is entirely in there. Driving
# the container with `docker run` rather than a workflow-level `container:`
# keeps checkout and artifact upload on the host and puts only the compilers
# inside, which is the shape wheels.yml already uses.
#
#   packaging/linux/build.sh            -> dist/FeetBrowser-<version>-x86_64.AppImage
#
# x86_64 only. An aarch64 AppImage is the same script with a different image
# and a different appimagetool, but on an x86_64 runner it means emulating the
# whole build under qemu, which is slow enough to want its own job and its own
# hardware to be tested on. Not in this change; see README.md.
set -euo pipefail

ROOT=$(cd "$(dirname "$0")/../.." && pwd)
IMAGE="${IMAGE:-quay.io/pypa/manylinux_2_28_x86_64}"

docker run --rm -i \
  --platform linux/amd64 \
  -v "$ROOT:/io" \
  -e PY_VERSION="${PY_VERSION:-3.12.11}" \
  -w /io "$IMAGE" \
  bash /io/packaging/linux/build-appimage.sh

echo
echo "Artifacts in $ROOT/dist:"
ls -lh "$ROOT/dist"
