#!/usr/bin/env bash
# Run the acceptance test on a built AppImage, in a clean container.
#
#   packaging/linux/verify.sh dist/FeetBrowser-0.6.0-x86_64.AppImage
#
# debian:stable-slim on purpose: a different distribution family from the one
# the bundle was built in (AlmaLinux), a newer glibc, no Python, no fonts and
# no CA certificates. If it runs there it is not merely running on the machine
# that built it.
set -euo pipefail

ROOT=$(cd "$(dirname "$0")/../.." && pwd)
IMAGE_PATH=${1:-}
if [ -z "$IMAGE_PATH" ]; then
  IMAGE_PATH=$(ls "$ROOT"/dist/*.AppImage 2>/dev/null | head -1)
fi
[ -n "$IMAGE_PATH" ] || { echo "no AppImage; run build.sh first" >&2; exit 1; }
IMAGE_PATH=$(cd "$(dirname "$IMAGE_PATH")" && pwd)/$(basename "$IMAGE_PATH")

OUTDIR="${OUTDIR:-$ROOT/dist/verify}"
mkdir -p "$OUTDIR"

# --device /dev/fuse lets the AppImage mount itself the way it does on a
# desktop. Where the host cannot offer that, the script inside falls back to
# the runtime's extract-and-run mode and says so.
FUSE=()
if [ -e /dev/fuse ]; then
  FUSE=(--device /dev/fuse --cap-add SYS_ADMIN --security-opt apparmor=unconfined)
else
  echo "no /dev/fuse on this host: the AppImage will use extract-and-run inside"
fi

docker run --rm -i \
  --platform linux/amd64 \
  ${FUSE[@]+"${FUSE[@]}"} \
  -v "$IMAGE_PATH:/app.AppImage:ro" \
  -v "$ROOT/tests:/verify/tests:ro" \
  -v "$ROOT/packaging/linux:/verify/packaging:ro" \
  -v "$OUTDIR:/out" \
  debian:stable-slim \
  sh /verify/packaging/verify-in-container.sh /app.AppImage

echo
echo "screenshots in $OUTDIR:"
ls -lh "$OUTDIR"
