#!/usr/bin/env python3
"""Install/verify/remove this producer without touching other RunCat metrics."""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path
import plistlib
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from xml.parsers.expat import ExpatError

ROOT = Path(__file__).resolve().parents[1]
MIN_PYTHON = (3, 10)
LABEL = "dev.runcat.battery-power"
LAUNCHCTL = "/bin/launchctl"
OWNED_SCRIPTS = ("update-battery.py", "adaptive-poll.sh")


def read_plist(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        data = plistlib.loads(path.read_bytes())
    except (OSError, ValueError, plistlib.InvalidFileException, ExpatError) as exc:
        raise RuntimeError("Existing LaunchAgent is unreadable; it was not overwritten") from exc
    if not isinstance(data, dict) or data.get("Label") != LABEL:
        raise RuntimeError("Existing LaunchAgent has an unexpected label/shape")
    return data


def absolute_path(value: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise RuntimeError("Use absolute installation/data paths")
    return path


class Config:
    def __init__(self, bootstrap_python: str | None = None):
        self.plist = Path.home() / "Library/LaunchAgents" / (LABEL + ".plist")
        self.old_plist = read_plist(self.plist)
        old = self.old_plist.get("EnvironmentVariables", {})
        if not isinstance(old, dict):
            raise RuntimeError("Existing LaunchAgent environment is not an object")
        def setting(key, default):
            value = os.environ[key] if key in os.environ else old.get(key, str(default))
            if not isinstance(value, str) or not value.strip() or "\0" in value:
                raise RuntimeError("Setting must be a nonempty string: " + key)
            return value
        self.home = absolute_path(setting("RUNCAT_HOME", Path.home() / ".runcat"))
        old_script = old.get("RUNCAT_BATTERY_SCRIPT")
        default_install = Path(old_script).parent if isinstance(old_script, str) else self.home / "runcat-battery-power"
        self.install = absolute_path(setting("RUNCAT_BATTERY_INSTALL_DIR", default_install))
        self.out = absolute_path(setting("RUNCAT_OUT_FILE", self.home / "battery-power.json"))
        self.history = absolute_path(setting("RUNCAT_BATTERY_HISTORY_FILE", self.home / "battery-power-history.json"))
        selected = setting("PYTHON_BIN", bootstrap_python or sys.executable)
        found = shutil.which(selected)
        if not found:
            raise RuntimeError("Python executable was not found")
        # Keep a stable Homebrew symlink rather than resolving into its Cellar.
        self.python = os.path.abspath(found)
        self.domain = f"gui/{os.getuid()}"
        self.target = self.domain + "/" + LABEL
        paths = self.targets()
        reserved = (Path(self.python), Path('/usr/sbin/ioreg'), Path('/bin/sh'),
                    self.install / 'stdout.log', self.install / 'stderr.log',
                    self.history.with_name('.' + self.history.name + '.lock'))
        if {p.resolve() for p in paths} & {p.resolve() for p in reserved}:
            raise RuntimeError("Installation/data paths conflict with an executable, log or sampling lock")
        if len({p.resolve() for p in paths}) != len(paths):
            raise RuntimeError("Installation, snapshot, history and plist paths must be distinct")
        for path in paths:
            if path.is_symlink() or (path.exists() and not path.is_file()):
                raise RuntimeError("Refusing to replace a symlink or non-file: " + str(path))

    def targets(self):
        return [self.install / name for name in OWNED_SCRIPTS] + [self.plist, self.out, self.history]

    def variables(self):
        return {
            "PYTHON_BIN": self.python,
            "PYTHONDONTWRITEBYTECODE": "1",
            "RUNCAT_HOME": str(self.home),
            "RUNCAT_BATTERY_INSTALL_DIR": str(self.install),
            "RUNCAT_OUT_FILE": str(self.out),
            "RUNCAT_BATTERY_HISTORY_FILE": str(self.history),
            "RUNCAT_BATTERY_SCRIPT": str(self.install / "update-battery.py"),
        }

    def environment(self):
        return {**os.environ, **self.variables()}

    def payload(self):
        return {
            "Label": LABEL,
            "ProgramArguments": ["/bin/sh", str(self.install / "adaptive-poll.sh")],
            "EnvironmentVariables": self.variables(),
            "RunAtLoad": True, "KeepAlive": True, "ProcessType": "Background",
            "ThrottleInterval": 30,
            "StandardOutPath": str(self.install / "stdout.log"),
            "StandardErrorPath": str(self.install / "stderr.log"),
        }


def require_native():
    if sys.platform != "darwin":
        raise RuntimeError("Installation/verification requires macOS")
    if sys.version_info < MIN_PYTHON:
        raise RuntimeError("Python 3.10 or newer is required")
    if not os.access("/usr/sbin/ioreg", os.X_OK):
        raise RuntimeError("/usr/sbin/ioreg is unavailable")


def producer_module():
    spec = importlib.util.spec_from_file_location("battery_install_producer", ROOT / "update-battery.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def launch(*args, check=True):
    result = subprocess.run([LAUNCHCTL, *args], capture_output=True, text=True, timeout=15)
    if check and result.returncode:
        raise RuntimeError("launchctl " + args[0] + " failed")
    return result


def loaded(cfg):
    return launch("print", cfg.target, check=False).returncode == 0


def disabled(cfg):
    result = launch("print-disabled", cfg.domain, check=False)
    return bool(re.search(r'"' + re.escape(LABEL) + r'"\s*=>\s*true', result.stdout))


def stop(cfg):
    if loaded(cfg):
        launch("bootout", cfg.target)
        if loaded(cfg):
            raise RuntimeError("Could not stop the existing battery LaunchAgent")


def start(cfg):
    launch("enable", cfg.target)
    launch("bootstrap", cfg.domain, str(cfg.plist))


def wait_running(cfg):
    deadline = time.monotonic() + 6
    while True:
        result = launch("print", cfg.target, check=False)
        if (result.returncode == 0 and re.search(r'\bstate\s*=\s*running\b', result.stdout)
                and re.search(r'\bpid\s*=\s*[1-9][0-9]*\b', result.stdout)):
            return
        if time.monotonic() >= deadline:
            raise RuntimeError("Battery LaunchAgent is not running; installation logs are in its directory")
        time.sleep(0.2)


def atomic_bytes(path, raw, mode):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix="." + path.name + "-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(raw)
        os.replace(name, path)
    finally:
        try:
            os.unlink(name)
        except FileNotFoundError:
            pass


def snapshot_check(path, since):
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("title") != "Battery Power":
        raise RuntimeError("A valid battery snapshot was not produced")
    metrics = data.get("metrics")
    if not isinstance(metrics, list) or len(metrics) != 5 or not all(isinstance(m, dict) for m in metrics):
        raise RuntimeError("Battery snapshot metrics are invalid")
    power_text = metrics[0].get("formattedValue")
    match = re.fullmatch(r"([0-9]+\.[0-9]) W", power_text) if isinstance(power_text, str) else None
    if not match or not 0 <= float(match.group(1)) <= 200:
        raise RuntimeError("No usable battery power reading; installation was not verified")
    if [m.get("title") for m in metrics[1:]] != ["5m Avg", "5m Peak", "Estimated Runtime", "Temperature"]:
        raise RuntimeError("Unexpected battery snapshot layout")
    if not isinstance(data.get("lastUpdatedDate"), str):
        raise RuntimeError("Snapshot timestamp is invalid")
    stamp = datetime.strptime(data["lastUpdatedDate"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
    if stamp < since - 1:
        raise RuntimeError("The snapshot is stale; no successful live update was verified")
    return data


def live_sample(script, cfg, out=None, history=None):
    env = cfg.environment()
    out = cfg.out if out is None else out
    history = cfg.history if history is None else history
    env.update(RUNCAT_OUT_FILE=str(out), RUNCAT_BATTERY_HISTORY_FILE=str(history))
    since = time.time()
    result = subprocess.run([cfg.python, str(script)], env=env, text=True,
                            capture_output=True, timeout=15)
    if result.returncode:
        raise RuntimeError("Live battery sampling failed; raw telemetry was not logged by the installer")
    return snapshot_check(out, since)


def verify(cfg):
    for name in OWNED_SCRIPTS:
        if (cfg.install / name).read_bytes() != (ROOT / name).read_bytes():
            raise RuntimeError("Installed source does not match this package: " + name)
    payload = read_plist(cfg.plist)
    if payload != cfg.payload():
        raise RuntimeError("LaunchAgent configuration differs from this installation")
    wait_running(cfg)
    data = live_sample(cfg.install / "update-battery.py", cfg)
    print("LOCAL CHECK PASSED")
    print("Output: " + str(cfg.out))
    for metric in data["metrics"]:
        print(metric["title"] + ": " + metric["formattedValue"])
    print("Cadence: battery 5s / charging or AC 60s (unchanged)")
    return data


def backup(cfg, paths, was_loaded, was_disabled):
    cfg.home.mkdir(parents=True, exist_ok=True)
    directory = cfg.home / "backups"
    directory.mkdir(exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix="battery-power-" + time.strftime("%Y%m%d-%H%M%S") + "-", dir=directory))
    saved, records = {}, []
    for index, path in enumerate(paths):
        value = (path.read_bytes(), path.stat().st_mode & 0o777) if path.exists() else None
        saved[path] = value
        filename = str(index) + "-" + path.name
        if value:
            (directory / filename).write_bytes(value[0])
        records.append({"path": str(path), "backup": filename if value else None,
                        "mode": value[1] if value else None})
    (directory / "RESTORE.json").write_text(json.dumps({"files": records, "was_loaded": was_loaded,
        "was_disabled": was_disabled}, indent=2), encoding="utf-8")
    print("Backup: " + str(directory))
    return saved


def restore(cfg, saved, was_loaded, was_disabled, module):
    stop(cfg)
    with module.sample_lock(cfg.history):
        for path, value in saved.items():
            if value is None:
                path.unlink(missing_ok=True)
            else:
                atomic_bytes(path, value[0], value[1])
    if was_loaded:
        start(cfg)
    if was_disabled:
        launch("disable", cfg.target)


def install(cfg):
    # The bootstrap manager may run on a different Python from the saved runtime.
    checked = subprocess.run([cfg.python, "-c", f"import sys; sys.exit(0 if sys.version_info >= {MIN_PYTHON!r} else 1)"],
                             capture_output=True, timeout=5)
    if checked.returncode:
        raise RuntimeError("The selected runtime requires Python 3.10 or newer; nothing was changed")
    module = producer_module()
    # Test hardware before stopping or replacing a working installation. These
    # temporary files are separate from the user's snapshot and history.
    with tempfile.TemporaryDirectory(prefix="runcat-battery-check-") as tmp:
        live_sample(ROOT / "update-battery.py", cfg, Path(tmp) / "snapshot.json", Path(tmp) / "history.json")
    was_loaded, was_disabled = loaded(cfg), disabled(cfg)
    # Stop before copying: no running sampler sees half of an installation.
    saved = None
    try:
        stop(cfg)
        with module.sample_lock(cfg.history):
            saved = backup(cfg, cfg.targets(), was_loaded, was_disabled)
            for name in OWNED_SCRIPTS:
                atomic_bytes(cfg.install / name, (ROOT / name).read_bytes(), 0o755)
            atomic_bytes(cfg.plist, plistlib.dumps(cfg.payload(), sort_keys=False), 0o644)
        live_sample(cfg.install / "update-battery.py", cfg)
        start(cfg)
        verify(cfg)
    except BaseException:
        try:
            if saved is not None:
                restore(cfg, saved, was_loaded, was_disabled, module)
            elif was_loaded and not loaded(cfg):
                start(cfg)
            print("Previous installation state restored.", file=sys.stderr)
        except Exception:
            print("RESTORE INCOMPLETE: retain the backup and inspect the LaunchAgent before retrying.", file=sys.stderr)
        raise


def uninstall(cfg, keep_data):
    module = producer_module()
    was_loaded, was_disabled = loaded(cfg), disabled(cfg)
    paths = [cfg.install / name for name in OWNED_SCRIPTS] + [cfg.plist]
    if not keep_data:
        paths += [cfg.out, cfg.history]
    stop(cfg)
    saved = None
    try:
        with module.sample_lock(cfg.history):
            saved = backup(cfg, paths, was_loaded, was_disabled)
            for path in paths:
                path.unlink(missing_ok=True)
    except BaseException:
        if saved is not None:
            restore(cfg, saved, was_loaded, was_disabled, module)
        elif was_loaded:
            start(cfg)
        raise
    # Logs, backup and the empty lock inode are deliberately retained. Never
    # recursively remove a directory shared with another custom metric.
    print("Uninstalled battery sampler. " + ("Metric/history data kept." if keep_data else "Metric/history data removed; backup kept."))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("install", "verify", "uninstall"))
    parser.add_argument("--bootstrap-python", help=argparse.SUPPRESS)
    parser.add_argument("--keep-data", action="store_true")
    args = parser.parse_args()
    if args.keep_data and args.action != "uninstall":
        parser.error("--keep-data is only valid with uninstall")
    require_native()
    cfg = Config(bootstrap_python=args.bootstrap_python)
    if args.action == "install":
        install(cfg)
    elif args.action == "verify":
        verify(cfg)
    else:
        uninstall(cfg, args.keep_data)


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, RuntimeError, KeyError, subprocess.SubprocessError) as exc:
        print("INSTALL/VERIFY STOPPED: " + (str(exc) if isinstance(exc, RuntimeError) else type(exc).__name__), file=sys.stderr)
        raise SystemExit(1)
