#!/usr/bin/env bash
# Installs everything OptiFuzz needs on Linux: the system packages (window toolkit, fonts, git),
# then a virtual environment with the Python requirements.
#
# System packages are installed with sudo on Debian-family systems (apt), Fedora (dnf) and Arch
# (pacman). Debian-family is the tested path; the Fedora and Arch package names are provided but
# not tested. Any other distribution: install the equivalents by hand, then run this again.
set -euo pipefail
cd "$(dirname "$0")/../.."  # repository root (this script lives in scripts/linux)

if ! command -v python3 >/dev/null; then
    echo "python3 is required (3.11 or newer)." >&2
    exit 1
fi
if ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)'; then
    echo "Python 3.11 or newer is required (dirsearch needs it); found $(python3 --version)." >&2
    echo "Ubuntu 22.04 ships Python 3.10: use Ubuntu 24.04 / Debian 12 or newer, or install a newer Python." >&2
    exit 1
fi

# Window toolkit: GTK 3 + WebKit2GTK 4.1 (pywebview), PyGObject from the distribution, git (dirsearch
# is installed from its repository), and fonts: the UI text uses bundled fonts, but the icon glyphs
# (gear, pause, play, ...) come from system fonts, and minimal installs have none.
if command -v apt-get >/dev/null; then
    echo "Installing system packages with apt (needs sudo)..."
    sudo apt-get update
    sudo apt-get install -y python3-venv python3-gi gir1.2-gtk-3.0 gir1.2-webkit2-4.1 git \
        fonts-dejavu-core fonts-noto-core
elif command -v dnf >/dev/null; then
    echo "Installing system packages with dnf (needs sudo)..."
    sudo dnf install -y python3 python3-gobject gtk3 webkit2gtk4.1 git \
        dejavu-sans-fonts google-noto-sans-fonts
elif command -v pacman >/dev/null; then
    echo "Installing system packages with pacman (needs sudo)..."
    sudo pacman -S --needed --noconfirm python python-gobject gtk3 webkit2gtk-4.1 git \
        ttf-dejavu noto-fonts
else
    echo "Unknown package manager: install the equivalents of these by hand, then run this again:"
    echo "  Python 3.11+ with venv, PyGObject (python3-gi), GTK 3, WebKit2GTK 4.1, git,"
    echo "  and DejaVu + Noto fonts."
fi

# --system-site-packages: PyGObject (gi) is provided by the distribution.
python3 -m venv --system-site-packages .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt

# Application menu entry, so OptiFuzz is found and launched like any installed app.
entry_dir="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
mkdir -p "$entry_dir"
cat > "$entry_dir/optifuzz.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=OptiFuzz
Comment=Web content discovery with dirsearch
Exec="$PWD/scripts/linux/run-linux.sh"
Path=$PWD
Icon=$PWD/frontend/icon-dark.png
Terminal=false
Categories=Network;Security;
EOF
command -v update-desktop-database >/dev/null && update-desktop-database "$entry_dir" || true

echo
echo "Done. Find OptiFuzz in your application menu, or start it with: ./scripts/linux/run-linux.sh"
