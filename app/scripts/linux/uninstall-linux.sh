#!/usr/bin/env bash
# Removes what install-linux.sh created: the application menu entry and the virtual environment.
# Your data (projects, results, settings) is kept unless you confirm or pass --remove-data.
# The system packages (GTK, WebKit, fonts, git) are not removed: other programs may use them.
# The program folder itself is not deleted; remove it by hand if you want it gone.
set -euo pipefail
cd "$(dirname "$0")/../../.."  # repository root (this script lives in app/scripts/linux)

remove_data=false
[ "${1:-}" = "--remove-data" ] && remove_data=true

if pgrep -f "$PWD/.venv/bin/python.*run_desktop.py" >/dev/null; then
    echo "OptiFuzz is running. Close it and run this again." >&2
    exit 1
fi

entry="${XDG_DATA_HOME:-$HOME/.local/share}/applications/optifuzz.desktop"
if [ -f "$entry" ]; then
    rm -f "$entry"
    command -v update-desktop-database >/dev/null && update-desktop-database "$(dirname "$entry")" || true
    echo "Removed the application menu entry."
fi
if [ -d .venv ]; then
    rm -rf .venv
    echo "Removed the virtual environment (.venv)."
fi

data_dir="${XDG_DATA_HOME:-$HOME/.local/share}/OptiFuzz"
if [ -d "$data_dir" ]; then
    if ! $remove_data && [ -t 0 ]; then
        read -r -p "Also delete your OptiFuzz data (projects, results, settings) in $data_dir ? [y/N] " answer
        case "$answer" in [yYsS]*) remove_data=true ;; esac
    fi
    if $remove_data; then
        rm -rf "$data_dir"
        echo "Removed your data ($data_dir)."
    else
        echo "Kept your data in $data_dir."
    fi
fi

echo
echo "Done. The system packages were not touched. To finish, delete the program folder: $PWD"
