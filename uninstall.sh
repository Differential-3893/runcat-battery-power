#!/bin/sh
set -eu

LABEL="dev.runcat.battery-power"
RUNCAT_HOME="${RUNCAT_HOME:-$HOME/.runcat}"
INSTALL_DIR="${RUNCAT_BATTERY_INSTALL_DIR:-$RUNCAT_HOME/runcat-battery-power}"
OUT_FILE="${RUNCAT_OUT_FILE:-$RUNCAT_HOME/battery-power.json}"
HISTORY_FILE="${RUNCAT_BATTERY_HISTORY_FILE:-$RUNCAT_HOME/battery-power-history.json}"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
KEEP_DATA=0

if [ "${1:-}" = "--keep-data" ]; then
  KEEP_DATA=1
elif [ "$#" -gt 0 ]; then
  echo "Usage: ./uninstall.sh [--keep-data]" >&2
  exit 2
fi

DOMAIN="gui/$(id -u)"
launchctl bootout "$DOMAIN" "$PLIST" >/dev/null 2>&1 || \
  launchctl unload "$PLIST" >/dev/null 2>&1 || true

rm -f "$PLIST"
rm -f "$INSTALL_DIR/update-battery.py" \
      "$INSTALL_DIR/adaptive-poll.sh" \
      "$INSTALL_DIR/stdout.log" \
      "$INSTALL_DIR/stderr.log"
rmdir "$INSTALL_DIR" >/dev/null 2>&1 || true

if [ "$KEEP_DATA" -eq 0 ]; then
  rm -f "$OUT_FILE" "$HISTORY_FILE"
fi

if [ "$KEEP_DATA" -eq 1 ]; then
  echo "Uninstalled. Generated metric/history files were kept."
else
  echo "Uninstalled RunCat Battery Power."
fi
