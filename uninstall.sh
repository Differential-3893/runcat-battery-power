#!/bin/sh
set -eu
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
# Bootstrap the manager without manufacturing an explicit runtime override.
# The manager prefers a caller override, then the recorded Python, then default.
if [ "${PYTHON_BIN+set}" = set ]; then
  if [ -z "$PYTHON_BIN" ]; then
    echo "PYTHON_BIN must not be empty." >&2
    exit 1
  fi
  bootstrap_python="$PYTHON_BIN"
else
  bootstrap_python=$(command -v python3 || true)
fi
if [ -z "$bootstrap_python" ]; then
  echo "python3 was not found on PATH." >&2
  exit 1
fi
exec "$bootstrap_python" -B "$SCRIPT_DIR/scripts/manage_install.py" uninstall --bootstrap-python "$bootstrap_python" "$@"
