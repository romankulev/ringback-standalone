#!/usr/bin/env bash
# macOS/Linux launcher. run_app.py parses the gitignored .env without sourcing it.
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [ -x "$APP_DIR/.venv/bin/python" ]; then
  PYTHON_BIN="${PYTHON_BIN:-$APP_DIR/.venv/bin/python}"
else
  PYTHON_BIN="${PYTHON_BIN:-$(command -v python3)}"
fi

exec "$PYTHON_BIN" "$APP_DIR/run_app.py" "$@"
