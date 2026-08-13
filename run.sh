#!/usr/bin/env bash
# Launch FeetBrowser. On NixOS this pulls a Python with Tk on the fly.
# NixOS is only tested platform
set -euo pipefail
cd "$(dirname "$0")"

if python3 -c "import tkinter" 2>/dev/null; then
  exec python3 -m feetbrowser "$@"
elif command -v nix-shell >/dev/null 2>&1; then
  exec nix-shell -p "python3.withPackages(ps: with ps; [ tkinter ])" \
    --run "python3 -m feetbrowser $*"
else
  echo "Need Python with tkinter. Install python3-tk (Debian/Ubuntu)," >&2
  echo "python3-tkinter (Fedora), or tk (Arch), then re-run." >&2
  exit 1
fi
