#!/usr/bin/env bash
set -Eeuo pipefail

echo "=================================="
echo " Media Grabber Linux Installer"
echo "=================================="

PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

if [[ ! -f "$PROJECT_DIR/app.py" ]]; then
    echo "Error: app.py was not found beside this installer." >&2
    exit 1
fi

if [[ ! -f /etc/os-release ]]; then
    echo "Error: Cannot identify this Linux distribution." >&2
    exit 1
fi

. /etc/os-release
case " ${ID:-} ${ID_LIKE:-} " in
    *" debian "*|*" ubuntu "*) ;;
    *)
        echo "Error: This installer supports Debian and Ubuntu-based systems." >&2
        exit 1
        ;;
esac

if ! command -v apt-get >/dev/null 2>&1; then
    echo "Error: apt-get is required to install system dependencies." >&2
    exit 1
fi

SUDO=()
if [[ "$EUID" -ne 0 ]]; then
    if ! command -v sudo >/dev/null 2>&1; then
        echo "Error: Run as root or install sudo first." >&2
        exit 1
    fi
    SUDO=(sudo)
fi

echo "[1/4] Installing system dependencies..."
"${SUDO[@]}" apt-get update
"${SUDO[@]}" apt-get install -y \
    ca-certificates \
    ffmpeg \
    python3 \
    python3-pip \
    python3-tk \
    python3-venv

echo "[2/4] Creating Python virtual environment..."
python3 -m venv "$PROJECT_DIR/.venv"
PYTHON="$PROJECT_DIR/.venv/bin/python"

echo "[3/4] Installing Python dependencies..."
if ! "$PYTHON" -m pip --version >/dev/null 2>&1; then
    "$PYTHON" -m ensurepip --upgrade
fi
"$PYTHON" -m pip install --upgrade pip yt-dlp

echo "[4/4] Verifying dependencies..."
"$PYTHON" -c 'import tkinter, yt_dlp; print(f"Tkinter: {tkinter.TkVersion}"); print(f"yt-dlp: {yt_dlp.version.__version__}")'
ffmpeg -version >/dev/null

echo
echo "Installation completed."
echo "Launch Media Grabber with:"
printf '  "%s" "%s/app.py"\n' "$PYTHON" "$PROJECT_DIR"