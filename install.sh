#!/bin/sh
set -eu

LABEL="dev.runcat.battery-power"
RUNCAT_HOME="${RUNCAT_HOME:-$HOME/.runcat}"
INSTALL_DIR="${RUNCAT_BATTERY_INSTALL_DIR:-$RUNCAT_HOME/runcat-battery-power}"
OUT_FILE="${RUNCAT_OUT_FILE:-$RUNCAT_HOME/battery-power.json}"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)

if [ "$(uname -s)" != "Darwin" ]; then
  echo "This integration is for macOS." >&2
  exit 1
fi

PYTHON_BIN="${PYTHON_BIN:-$(command -v python3 || true)}"
if [ -z "$PYTHON_BIN" ]; then
  cat >&2 <<'MSG'
python3 was not found.
Install Python 3.10 or newer, then run ./install.sh again.
MSG
  exit 1
fi

if ! "$PYTHON_BIN" - <<'PY'
import sys
raise SystemExit(0 if sys.version_info >= (3, 10) else 1)
PY
then
  echo "Python 3.10 or newer is required. Found: $($PYTHON_BIN --version 2>&1)" >&2
  exit 1
fi

if [ ! -x /usr/sbin/ioreg ]; then
  echo "/usr/sbin/ioreg is unavailable on this Mac." >&2
  exit 1
fi

mkdir -p "$INSTALL_DIR" "$RUNCAT_HOME" "$HOME/Library/LaunchAgents"
cp "$SCRIPT_DIR/update-battery.py" "$INSTALL_DIR/update-battery.py"
cp "$SCRIPT_DIR/adaptive-poll.sh" "$INSTALL_DIR/adaptive-poll.sh"
chmod 755 "$INSTALL_DIR/update-battery.py" "$INSTALL_DIR/adaptive-poll.sh"

# Generate the LaunchAgent with plistlib so paths are escaped correctly.
PLIST_PATH="$PLIST" \
LABEL_VALUE="$LABEL" \
INSTALL_DIR_VALUE="$INSTALL_DIR" \
RUNCAT_HOME_VALUE="$RUNCAT_HOME" \
OUT_FILE_VALUE="$OUT_FILE" \
PYTHON_BIN_VALUE="$PYTHON_BIN" \
"$PYTHON_BIN" - <<'PY'
import os
import plistlib
from pathlib import Path

plist_path = Path(os.environ["PLIST_PATH"])
label = os.environ["LABEL_VALUE"]
install_dir = os.environ["INSTALL_DIR_VALUE"]
runcat_home = os.environ["RUNCAT_HOME_VALUE"]
out_file = os.environ["OUT_FILE_VALUE"]
python_bin = os.environ["PYTHON_BIN_VALUE"]

payload = {
    "Label": label,
    "ProgramArguments": ["/bin/sh", f"{install_dir}/adaptive-poll.sh"],
    "EnvironmentVariables": {
        "PYTHON_BIN": python_bin,
        "RUNCAT_HOME": runcat_home,
        "RUNCAT_OUT_FILE": out_file,
        "RUNCAT_BATTERY_SCRIPT": f"{install_dir}/update-battery.py",
    },
    "RunAtLoad": True,
    "KeepAlive": True,
    "ProcessType": "Background",
    "ThrottleInterval": 30,
    "StandardOutPath": f"{install_dir}/stdout.log",
    "StandardErrorPath": f"{install_dir}/stderr.log",
}

with plist_path.open("wb") as f:
    plistlib.dump(payload, f, sort_keys=False)
PY

# Produce an initial valid JSON snapshot before RunCat is pointed at the file.
"$PYTHON_BIN" "$INSTALL_DIR/update-battery.py"

DOMAIN="gui/$(id -u)"

# Replace an older copy if one is already loaded.
launchctl bootout "$DOMAIN" "$PLIST" >/dev/null 2>&1 || \
  launchctl unload "$PLIST" >/dev/null 2>&1 || true

if launchctl bootstrap "$DOMAIN" "$PLIST" >/dev/null 2>&1; then
  launchctl enable "$DOMAIN/$LABEL" >/dev/null 2>&1 || true
  launchctl kickstart -k "$DOMAIN/$LABEL" >/dev/null 2>&1 || true
else
  # Fallback for older launchctl behavior.
  launchctl load "$PLIST"
fi

cat <<MSG
Installed RunCat Battery Power.

Producer:    $INSTALL_DIR/update-battery.py
Output JSON: $OUT_FILE
LaunchAgent: $PLIST

In RunCat Neo:
  Settings -> Metrics -> Custom Metrics -> Add Custom Metrics Source
and select:
  $OUT_FILE

For diagnostics:
  $PYTHON_BIN $INSTALL_DIR/update-battery.py --diagnose
MSG
