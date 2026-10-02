#!/bin/sh
set -eu
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PYTHON_BIN="${PYTHON_BIN:-$(command -v python3 || true)}"
if [ -z "$PYTHON_BIN" ]; then
  echo "Python 3.10 or newer is required." >&2
  exit 1
fi
export PYTHON_BIN
exec "$PYTHON_BIN" -B "$SCRIPT_DIR/scripts/manage_install.py" uninstall "$@"
