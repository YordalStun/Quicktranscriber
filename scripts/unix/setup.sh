#!/usr/bin/env bash
# QuickTranscriber - setup and start for macOS and Linux.
#
# Installs a private copy of Python and the libraries QuickTranscriber needs
# into this folder only (runtime/). Nothing is installed anywhere else.
# To uninstall, delete the folder.
#
# Usage: scripts/unix/setup.sh [--no-launch] [--no-gpu]
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
RUNTIME="$ROOT/runtime"
UV_VERSION="0.8.17"
PYTHON_VERSION="3.12"
LAUNCH=1
GPU=1
for arg in "$@"; do
  case "$arg" in
    --no-launch) LAUNCH=0 ;;
    --no-gpu) GPU=0 ;;
  esac
done

step() { printf '\n  \033[36m> %s\033[0m\n' "$1"; }
fail() { printf '\n  \033[31mSetup failed: %s\033[0m\n  Check your internet connection and try again.\n\n' "$1"; exit 1; }

printf '\n  \033[35mQuickTranscriber\033[0m\n  Private meeting transcription - everything stays on this computer.\n'
mkdir -p "$RUNTIME"

UV="$RUNTIME/uv/uv"
if [ ! -x "$UV" ]; then
  step "Downloading the installer (about 20 MB)..."
  case "$(uname -s)-$(uname -m)" in
    Darwin-arm64) target="aarch64-apple-darwin" ;;
    Darwin-x86_64) target="x86_64-apple-darwin" ;;
    Linux-x86_64) target="x86_64-unknown-linux-gnu" ;;
    Linux-aarch64|Linux-arm64) target="aarch64-unknown-linux-gnu" ;;
    *) fail "unsupported system $(uname -s) $(uname -m)" ;;
  esac
  mkdir -p "$RUNTIME/uv"
  curl -fsSL "https://github.com/astral-sh/uv/releases/download/$UV_VERSION/uv-$target.tar.gz" -o "$RUNTIME/uv.tar.gz" || fail "could not download uv"
  tar -xzf "$RUNTIME/uv.tar.gz" -C "$RUNTIME/uv" --strip-components=1 || fail "could not unpack uv"
  rm -f "$RUNTIME/uv.tar.gz"
fi

export UV_CACHE_DIR="$RUNTIME/cache"
export UV_PYTHON_INSTALL_DIR="$RUNTIME/python-installs"
export UV_PYTHON_BIN_DIR="$RUNTIME/python-installs/bin"
export UV_TOOL_DIR="$RUNTIME/tools"
export UV_NO_CONFIG=1
export UV_LINK_MODE=copy
export UV_PYTHON_PREFERENCE=only-managed

VENV="$RUNTIME/venv"
PY="$VENV/bin/python"
if [ ! -x "$PY" ]; then
  step "Installing a private copy of Python $PYTHON_VERSION..."
  "$UV" python install "$PYTHON_VERSION" --no-bin || fail "Python could not be downloaded"
  rm -rf "$VENV"
  "$UV" venv "$VENV" --python "$PYTHON_VERSION" --no-project || fail "could not create the Python environment"
fi

REQ="$ROOT/requirements.txt"
STAMP="$VENV/.requirements-installed"
HASH="$( (command -v shasum >/dev/null && shasum -a 256 "$REQ" || sha256sum "$REQ") | cut -d' ' -f1)"
if [ ! -f "$STAMP" ] || [ "$(cat "$STAMP")" != "$HASH" ]; then
  step "Installing speech recognition and AI libraries (about 400 MB, one time)..."
  "$UV" pip install --python "$PY" -r "$REQ" || fail "the libraries could not be installed"
  echo "$HASH" > "$STAMP"
fi

if [ "$GPU" = 1 ] && [ "$(uname -s)" = "Linux" ] && command -v nvidia-smi >/dev/null 2>&1 && [ ! -f "$VENV/.gpu-installed" ] && [ -z "${QT_NO_GPU:-}" ]; then
  step "NVIDIA graphics card found - adding GPU acceleration (about 1.3 GB, one time)..."
  if "$UV" pip install --python "$PY" -r "$ROOT/requirements-gpu.txt"; then touch "$VENV/.gpu-installed"
  else echo "  GPU acceleration could not be installed - the processor will be used instead."; fi
fi

step "Setup complete."
[ "$LAUNCH" = 1 ] || exit 0
export QT_ROOT="$ROOT" PYTHONUTF8=1
exec "$PY" -m quicktranscriber
