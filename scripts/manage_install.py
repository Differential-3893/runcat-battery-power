#!/usr/bin/env python3
"""Install/verify/remove this producer without touching other RunCat metrics."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import plistlib
import re
import shutil
import subprocess
import stat
import sys
import tempfile
import time
from datetime import datetime, timezone
from xml.parsers.expat import ExpatError

ROOT = Path(__file__).resolve().parents[1]
MIN_PYTHON = (3, 10)
LABEL = "dev.runcat.battery-power"
HEALTH_LABEL = "dev.runcat.battery-health"
HEALTH_INTERVAL = 21600
LAUNCHCTL = "/bin/launchctl"
OWNED_SCRIPTS = ("update-battery.py", "adaptive-poll.sh")


def read_plist(path: Path, label: str = LABEL) -> dict:
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise RuntimeError("LaunchAgent is not a regular file")
    if not path.exists():
        return {}
    try:
        data = plistlib.loads(path.read_bytes())
    except (OSError, ValueError, plistlib.InvalidFileException, ExpatError) as exc:
        raise RuntimeError("Existing LaunchAgent is unreadable; it was not overwritten") from exc
    if not isinstance(data, dict) or data.get("Label") != label:
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
        self.health_plist = self.plist.with_name(HEALTH_LABEL + ".plist")
        self.old_health_plist = read_plist(self.health_plist, HEALTH_LABEL)
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
        self.health = absolute_path(setting("RUNCAT_BATTERY_HEALTH_FILE", self.home / "battery-health.json"))
        selected = setting("PYTHON_BIN", bootstrap_python or sys.executable)
        found = shutil.which(selected)
        if not found:
            raise RuntimeError("Python executable was not found")
        # Keep a stable Homebrew symlink rather than resolving into its Cellar.
        self.python = os.path.abspath(found)
        self.domain = f"gui/{os.getuid()}"
        self.target = self.domain + "/" + LABEL
        self.health_target = self.domain + "/" + HEALTH_LABEL
        paths = self.targets()
        reserved = (Path(self.python), Path('/usr/sbin/ioreg'), Path('/bin/sh'),
                    self.install / 'stdout.log', self.install / 'stderr.log',
                    self.install / "health.stdout.log", self.install / "health.stderr.log",
                    Path("/usr/sbin/system_profiler"),
                    self.plist.with_name("." + self.plist.name + ".lock"),
                    self.health.with_name("." + self.health.name + ".lock"),
                    self.history.with_name('.' + self.history.name + '.lock'))
        if {p.resolve() for p in paths} & {p.resolve() for p in reserved}:
            raise RuntimeError("Installation/data paths conflict with an executable, log or sampling lock")
        if len({p.resolve() for p in paths}) != len(paths):
            raise RuntimeError("Installation, snapshot, history and plist paths must be distinct")
        for path in paths:
            if path.is_symlink() or any(parent.is_symlink() for parent in path.parents
                   if parent != Path.home() and parent.is_relative_to(Path.home())) or (path.exists() and not path.is_file()):
                raise RuntimeError("Refusing to replace a symlink or non-file: " + str(path))

    def targets(self):
        return [self.install / name for name in OWNED_SCRIPTS] + [self.plist, self.out, self.history, self.health_plist, self.health]

    def variables(self):
        return {
            "PYTHON_BIN": self.python,
            "PYTHONDONTWRITEBYTECODE": "1",
            "RUNCAT_HOME": str(self.home),
            "RUNCAT_BATTERY_INSTALL_DIR": str(self.install),
            "RUNCAT_OUT_FILE": str(self.out),
            "RUNCAT_BATTERY_HISTORY_FILE": str(self.history),
            "RUNCAT_BATTERY_HEALTH_FILE": str(self.health),
            "RUNCAT_BATTERY_SCRIPT": str(self.install / "update-battery.py"),
        }

    def environment(self):
        clean = {k: v for k, v in os.environ.items() if not k.startswith("RUNCAT_")
                 and k not in ("PYTHON_BIN", "PYTHONPATH", "PYTHONHOME")}
        return {**clean, **self.variables()}

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


    def health_payload(self):
        return {"Label": HEALTH_LABEL,
                "ProgramArguments": [self.python, str(self.install / "update-battery.py"), "--refresh-health"],
                "EnvironmentVariables": self.variables(), "RunAtLoad": True,
                "StartInterval": HEALTH_INTERVAL, "ProcessType": "Background",
                "StandardOutPath": str(self.install / "health.stdout.log"),
                "StandardErrorPath": str(self.install / "health.stderr.log")}


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


def loaded(cfg, health=False):
    return launch("print", cfg.health_target if health else cfg.target, check=False).returncode == 0


def disabled(cfg, health=False):
    result = launch("print-disabled", cfg.domain, check=False)
    return bool(re.search(r'"' + re.escape(HEALTH_LABEL if health else LABEL) + r'"\s*=>\s*true', result.stdout))


def stop(cfg, health=False):
    if loaded(cfg, health):
        launch("bootout", cfg.health_target if health else cfg.target)
        if loaded(cfg, health):
            raise RuntimeError("Could not stop the existing battery LaunchAgent")


def start(cfg, health=False):
    launch("enable", cfg.health_target if health else cfg.target)
    launch("bootstrap", cfg.domain, str(cfg.health_plist if health else cfg.plist))


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
    with path.open("rb") as stream:
        raw = stream.read(1024 * 1024 + 1)
    if len(raw) > 1024 * 1024:
        raise RuntimeError("Battery snapshot is too large")
    data = json.loads(raw)
    if not isinstance(data, dict) or data.get("title") != "Battery Power":
        raise RuntimeError("A valid battery snapshot was not produced")
    metrics = data.get("metrics")
    if not isinstance(metrics, list) or len(metrics) != 7 or not all(isinstance(m, dict) for m in metrics):
        raise RuntimeError("Battery snapshot metrics are invalid")
    power_text = metrics[0].get("formattedValue")
    match = re.fullmatch(r"([0-9]+\.[0-9]) W", power_text) if isinstance(power_text, str) else None
    if not match or not 0 <= float(match.group(1)) <= 200:
        raise RuntimeError("No usable battery power reading; installation was not verified")
    if [m.get("title") for m in metrics[1:]] != ["5m Avg", "5m Peak", "Estimated Runtime", "Temperature", "Maximum Capacity", "Cycle Count"]:
        raise RuntimeError("Unexpected battery snapshot layout")
    if not isinstance(data.get("lastUpdatedDate"), str):
        raise RuntimeError("Snapshot timestamp is invalid")
    stamp = datetime.strptime(data["lastUpdatedDate"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
    if not since - 1 <= stamp <= time.time() + 5:
        raise RuntimeError("The snapshot is stale; no successful live update was verified")
    for index, pattern in ((5, r"(?:[1-9][0-9]?|100)%(?: \(cached\))?|—"),
                           (6, r"[0-9]{1,6}|—")):
        value = metrics[index].get("formattedValue")
        if not isinstance(value, str) or re.fullmatch(pattern, value) is None:
            raise RuntimeError("Invalid health/cycle row")
    return data


def snapshot_handle(path, missing_ok=False):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        if missing_ok:
            return None
        raise RuntimeError("No new battery snapshot was produced") from None
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise RuntimeError("Battery snapshot is not a regular file")
    return fd


def live_sample(script, cfg, out=None, history=None):
    env = cfg.environment()
    out = cfg.out if out is None else out
    history = cfg.history if history is None else history
    env.update(RUNCAT_OUT_FILE=str(out), RUNCAT_BATTERY_HISTORY_FILE=str(history))
    before = snapshot_handle(out, missing_ok=True)
    try:
        since = time.time()
        result = subprocess.run([cfg.python, str(script)], env=env, text=True,
                                capture_output=True, timeout=15)
        if result.returncode:
            raise RuntimeError("Live battery sampling failed; raw telemetry was not logged by the installer")
        after = snapshot_handle(out)
        try:
            if before is not None:
                a, b = os.fstat(before), os.fstat(after)
                if (a.st_dev, a.st_ino) == (b.st_dev, b.st_ino):
                    raise RuntimeError("No atomic snapshot replacement occurred during this check")
        finally:
            os.close(after)
        return snapshot_check(out, since)
    finally:
        if before is not None:
            os.close(before)


def live_health(cfg):
    """Probe once with the saved Python, without changing the cache or snapshot."""
    result = subprocess.run([cfg.python, "-B", str(ROOT / "update-battery.py"), "--health-probe"],
                            env=cfg.environment(), capture_output=True, text=True, timeout=25)
    if result.returncode or len(result.stdout) > 2048:
        raise RuntimeError("macOS Maximum Capacity query failed; no raw report was logged. Previous installation was not replaced.")
    value = json.loads(result.stdout)
    if not isinstance(value, dict) or set(value) != {"maximumCapacityPercent", "cycleCount"}:
        raise RuntimeError("Invalid health probe")
    if type(value["maximumCapacityPercent"]) is not int or not 1 <= value["maximumCapacityPercent"] <= 100:
        raise RuntimeError("macOS Maximum Capacity was unavailable")
    if value["cycleCount"] is not None and (type(value["cycleCount"]) is not int or not 0 <= value["cycleCount"] <= 100000):
        raise RuntimeError("macOS cycle count was invalid")
    return producer_module().health_record(value)


def verify(cfg):
    for name in OWNED_SCRIPTS:
        if (cfg.install / name).read_bytes() != (ROOT / name).read_bytes():
            raise RuntimeError("Installed source does not match this package: " + name)
    payload = read_plist(cfg.plist)
    if payload != cfg.payload():
        raise RuntimeError("LaunchAgent configuration differs from this installation")
    if read_plist(cfg.health_plist, HEALTH_LABEL) != cfg.health_payload() or not loaded(cfg, True):
        raise RuntimeError("Six-hour capacity LaunchAgent is not registered with the saved runtime")
    wait_running(cfg)
    data = live_sample(cfg.install / "update-battery.py", cfg)
    record = producer_module().load_health_cache(cfg.health)
    if record is None or record["maximumCapacityPercent"] is None or data["metrics"][5]["formattedValue"] == "—":
        raise RuntimeError("No usable macOS capacity observation; run the installed refresh-health command")
    if data["metrics"][6]["formattedValue"] == "—":
        raise RuntimeError("No usable CycleCount observation")
    print("LOCAL CHECK PASSED")
    print("Output: " + str(cfg.out))
    for metric in data["metrics"]:
        print(metric["title"] + ": " + metric["formattedValue"])
    print("Cadence: battery 5s / charging or AC 60s (unchanged)")
    print("Maximum Capacity: macOS system_profiler; six-hour cache, independent of the fast sampler.")
    print("This verifies manual samples and registration, not a full timer cycle or the RunCat UI.")
    return data


def backup(cfg, paths, was_loaded, was_disabled, health_state=(False, False)):
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
            atomic_bytes(directory / filename, value[0], 0o600)
        records.append({"path": str(path), "backup": filename if value else None,
                        "mode": value[1] if value else None,
                        "sha256": hashlib.sha256(value[0]).hexdigest() if value else None})
    atomic_bytes(directory / "RESTORE.json", json.dumps({"version": 2, "files": records, "was_loaded": was_loaded,
        "was_disabled": was_disabled, "health_loaded": health_state[0], "health_disabled": health_state[1]}, indent=2).encode(), 0o600)
    print("Backup: " + str(directory))
    return saved


def restore(cfg, saved, was_loaded, was_disabled, module, health_state=(False, False)):
    stop(cfg, True)
    stop(cfg)
    with module.sample_lock(cfg.history), module.sample_lock(cfg.health):
        for path, value in saved.items():
            if value is None:
                path.unlink(missing_ok=True)
            else:
                atomic_bytes(path, value[0], value[1])
    if was_loaded:
        start(cfg)
    if was_disabled:
        launch("disable", cfg.target)
    if health_state[0]:
        start(cfg, True)
    if health_state[1]:
        launch("disable", cfg.health_target)


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
    health_record = live_health(cfg)
    was_loaded, was_disabled = loaded(cfg), disabled(cfg)
    health_state = loaded(cfg, True), disabled(cfg, True)
    # Stop before copying: no running sampler sees half of an installation.
    saved = None
    try:
        stop(cfg, True)
        stop(cfg)
        with module.sample_lock(cfg.history), module.sample_lock(cfg.health):
            saved = backup(cfg, cfg.targets(), was_loaded, was_disabled, health_state)
            for name in OWNED_SCRIPTS:
                atomic_bytes(cfg.install / name, (ROOT / name).read_bytes(), 0o755)
            atomic_bytes(cfg.plist, plistlib.dumps(cfg.payload(), sort_keys=False), 0o644)
            atomic_bytes(cfg.health_plist, plistlib.dumps(cfg.health_payload(), sort_keys=False), 0o644)
            atomic_bytes(cfg.health, json.dumps(health_record, allow_nan=False).encode(), 0o600)
        live_sample(cfg.install / "update-battery.py", cfg)
        start(cfg)
        start(cfg, True)
        verify(cfg)
    except BaseException:
        try:
            if saved is not None:
                restore(cfg, saved, was_loaded, was_disabled, module, health_state)
            elif was_loaded and not loaded(cfg):
                start(cfg)
            if health_state[0] and not loaded(cfg, True):
                start(cfg, True)
            print("Previous installation state restored.", file=sys.stderr)
        except Exception:
            print("RESTORE INCOMPLETE: retain the backup and inspect the LaunchAgent before retrying.", file=sys.stderr)
        raise


def uninstall(cfg, keep_data):
    module = producer_module()
    was_loaded, was_disabled = loaded(cfg), disabled(cfg)
    health_state = loaded(cfg, True), disabled(cfg, True)
    paths = [cfg.install / name for name in OWNED_SCRIPTS] + [cfg.plist, cfg.health_plist]
    if not keep_data:
        paths += [cfg.out, cfg.history, cfg.health]
    saved = None
    try:
        stop(cfg, True)
        stop(cfg)
        with module.sample_lock(cfg.history), module.sample_lock(cfg.health):
            saved = backup(cfg, paths, was_loaded, was_disabled, health_state)
            for path in paths:
                path.unlink(missing_ok=True)
    except BaseException:
        if saved is not None:
            restore(cfg, saved, was_loaded, was_disabled, module, health_state)
        else:
            if was_loaded and not loaded(cfg):
                start(cfg)
            if health_state[0] and not loaded(cfg, True):
                start(cfg, True)
        raise
    # Logs, backup and the empty lock inode are deliberately retained. Never
    # recursively remove a directory shared with another custom metric.
    print("Uninstalled battery sampler. " + ("Metric/history data kept." if keep_data else "Metric/history data removed; backup kept."))


def rollback(cfg, directory):
    """Restore only this installer's complete, verified, fixed-target backup."""
    directory = Path(directory).expanduser().absolute()
    if not directory.is_dir() or directory.is_symlink():
        raise RuntimeError("A regular backup directory is required")
    records_path = directory / "RESTORE.json"
    if records_path.is_symlink():
        raise RuntimeError("Backup manifest cannot be a symlink")
    raw = records_path.read_bytes()
    if len(raw) > 64 * 1024:
        raise RuntimeError("Backup manifest is too large")
    records = json.loads(raw)
    keys = ("was_loaded", "was_disabled", "health_loaded", "health_disabled")
    if not isinstance(records, dict) or records.get("version") != 2 or any(type(records.get(k)) is not bool for k in keys):
        raise RuntimeError("Unsupported backup format")
    files = records.get("files")
    if not isinstance(files, list) or len(files) != len(cfg.targets()):
        raise RuntimeError("Rollback requires a complete installation backup")
    expected = set(cfg.targets())
    saved = {}
    for entry in files:
        if not isinstance(entry, dict):
            raise RuntimeError("Invalid backup record")
        path = Path(entry["path"])
        if path not in expected or path in saved:
            raise RuntimeError("Backup targets differ from the installed paths")
        name = entry.get("backup")
        if name is None:
            saved[path] = None
        else:
            if not isinstance(name, str) or Path(name).name != name or name in (".", ".."):
                raise RuntimeError("Backup path escapes its directory")
            source = directory / name
            if source.is_symlink() or not source.is_file():
                raise RuntimeError("Backup file is unavailable")
            content = source.read_bytes()
            if hashlib.sha256(content).hexdigest() != entry.get("sha256"):
                raise RuntimeError("Backup checksum differs; nothing was restored")
            mode = entry.get("mode")
            if type(mode) is not int or not 0 <= mode <= 0o777:
                raise RuntimeError("Invalid backup file permissions")
            saved[path] = content, mode
    module = producer_module()
    current_loaded, current_disabled = loaded(cfg), disabled(cfg)
    current_health = loaded(cfg, True), disabled(cfg, True)
    current = None
    try:
        stop(cfg, True)
        stop(cfg)
        with module.sample_lock(cfg.history), module.sample_lock(cfg.health):
            current = backup(cfg, cfg.targets(), current_loaded, current_disabled, current_health)
        restore(cfg, saved, records["was_loaded"], records["was_disabled"], module,
                (records["health_loaded"], records["health_disabled"]))
    except BaseException:
        if current is not None:
            restore(cfg, current, current_loaded, current_disabled, module, current_health)
        else:
            if current_loaded and not loaded(cfg):
                start(cfg)
            if current_health[0] and not loaded(cfg, True):
                start(cfg, True)
        raise
    print("ROLLBACK COMPLETE: previous owned files and registered jobs restored.")
    print("Empty lock files and backup/log directories were retained. Other RunCat metrics were not changed.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("install", "verify", "uninstall", "rollback"))
    parser.add_argument("--bootstrap-python", help=argparse.SUPPRESS)
    parser.add_argument("--keep-data", action="store_true")
    parser.add_argument("--backup", help="Complete backup directory, only for rollback")
    args = parser.parse_args()
    if args.keep_data and args.action != "uninstall":
        parser.error("--keep-data is only valid with uninstall")
    if (args.action == "rollback") != (args.backup is not None):
        parser.error("rollback requires --backup; other actions do not accept it")
    require_native()
    cfg = Config(bootstrap_python=args.bootstrap_python)
    if args.action == "verify":
        verify(cfg)
    else:
        with producer_module().sample_lock(cfg.plist, timeout=0.0):
            if args.action == "install":
                install(cfg)
            elif args.action == "rollback":
                rollback(cfg, args.backup)
            else:
                uninstall(cfg, args.keep_data)


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, RuntimeError, KeyError, subprocess.SubprocessError) as exc:
        print("INSTALL/VERIFY STOPPED: " + (str(exc) if isinstance(exc, RuntimeError) else type(exc).__name__), file=sys.stderr)
        raise SystemExit(1)
