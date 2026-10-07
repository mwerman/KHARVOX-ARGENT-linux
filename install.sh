#!/usr/bin/env bash
# KHARVOX: ARGENT (DOOM Eternal VR) on Linux via Proton - installer entry point.
#
#   ./install.sh              install / update
#   ./install.sh --check      only check the setup, change nothing
#   ./install.sh --uninstall  remove everything this installer put on the system
#   ./install.sh --help       all options
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if ! command -v python3 >/dev/null 2>&1; then
    echo "install.sh: python3 is required (on Arch/CachyOS: sudo pacman -S python)" >&2
    exit 1
fi

exec python3 "$HERE/tools/argent_tools.py" install "$@"
