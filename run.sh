#!/usr/bin/env bash
# Launch FeetBrowser. No GUI toolkit needed -- rendering is our own.
# The only outside requirement is a system font; every platform we support
# ships one.
set -euo pipefail
cd "$(dirname "$0")"

exec python3 -m feetbrowser "$@"
