#!/usr/bin/env bash

set -Eeuo pipefail

# Check whether the operating system is supported.
if [[ ! -f /etc/os-release ]]; then
    echo "Error: Cannot identify your Linux distribution."
    exit 1
fi

. /etc/os-release

if [[ "${ID:-}" != "ubuntu" && "${ID:-}" != "debian" && 
      " ${ID_LIKE:-} " != *" ubuntu "* && 
      " ${ID_LIKE:-} " != *" debian "* ]]; then
    echo "This installer supports Ubuntu and Debian-based distributions."
    exit 1
fi

# Request sudo only when needed.
if [[ $EUID -ne 0 ]]; then
    SUDO="sudo"
else
    SUDO=""
fi

echo "Installing dependencies..."

$SUDO apt-get update
$SUDO apt-get install -y ca-certificates curl python3 ffmpeg

echo "Installing yt-dlp..."

TEMP_FILE="$(mktemp)"
trap 'rm -f "$TEMP_FILE"' EXIT

curl -fL 
    https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp 
    -o "$TEMP_FILE"

$SUDO install -m 755 "$TEMP_FILE" /usr/local/bin/yt-dlp
python3 -m pip install -U yt-dlp

echo "Verifying installation..."

yt-dlp --version
ffmpeg -version >/dev/null


echo "Installing Tkinter..." 
sudo apt install python3-tk
sudo apt update && sudo apt install -y python3-tk
echo "Verifying installation..."
apt show python3-tk

echo "=================================="
echo "Setup completed successfully!"
echo "yt-dlp and FFmpeg are ready to use."
echo "=================================="
