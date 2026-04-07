#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

PYTHON_BIN="${PYTHON_BIN:-python3}"
APP_NAME="${APP_NAME:-file_server_web}"
ENTRY_FILE="${ENTRY_FILE:-web_server.py}"

echo "[build] using python: $PYTHON_BIN"
"$PYTHON_BIN" --version

if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  echo "[build] python not found: $PYTHON_BIN" >&2
  exit 1
fi

"$PYTHON_BIN" -m pip install --upgrade pip
"$PYTHON_BIN" -m pip install -r requirements.txt pyinstaller

"$PYTHON_BIN" -m PyInstaller \
  --noconfirm \
  --clean \
  --onefile \
  --name "$APP_NAME" \
  "$ENTRY_FILE"

echo "[build] done: $ROOT_DIR/dist/$APP_NAME"
