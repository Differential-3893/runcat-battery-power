#!/bin/sh
# Adaptive RunCat battery sampler.
# - battery/discharging: 5 seconds
# - AC connected or charging: 60 seconds
#
# The shell remains idle between samples; Python is started only when a sample
# is actually due.

set -u

PYTHON_BIN="${PYTHON_BIN:-$(command -v python3 || true)}"
SCRIPT="${RUNCAT_BATTERY_SCRIPT:-${RUNCAT_HOME:-$HOME/.runcat}/runcat-battery-power/update-battery.py}"

if [ -z "$PYTHON_BIN" ]; then
  echo "python3 was not found on PATH." >&2
  exit 1
fi

if [ ! -f "$SCRIPT" ]; then
  echo "Battery producer not found: $SCRIPT" >&2
  exit 1
fi

sleep_pid=""
cleanup() {
  if [ -n "$sleep_pid" ]; then
    kill "$sleep_pid" 2>/dev/null || true
    wait "$sleep_pid" 2>/dev/null || true
  fi
  exit 0
}
trap cleanup INT TERM HUP

while :; do
  if delay=$("$PYTHON_BIN" "$SCRIPT" --adaptive-sample); then
    case "$delay" in
      5|60) ;;
      *) delay=60 ;;
    esac
  else
    # A temporary telemetry failure should not create a hot retry loop.
    delay=60
  fi

  sleep "$delay" &
  sleep_pid=$!
  wait "$sleep_pid"
  sleep_pid=""
done
