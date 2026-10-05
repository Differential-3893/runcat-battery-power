#!/usr/bin/env python3
"""One-shot manual commands using the installed LaunchAgent's saved runtime."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import plistlib
import subprocess
import sys
import time
from datetime import datetime, timezone

PROJECT = "runcat-battery-power"
LABEL = "dev.runcat.battery-power"
TITLE = "Battery Power"
MAX_JSON_BYTES = 1024 * 1024
CODEX_DIAGNOSTIC = """import importlib.util,json,sys
try:
    spec=importlib.util.spec_from_file_location('installed_runcat_diagnostic',sys.argv[1])
    if spec is None or spec.loader is None: raise RuntimeError()
    hook=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hook)
    print(json.dumps(hook.fetch_account_data(),ensure_ascii=False,allow_nan=False))
except Exception:
    sys.exit(1)
"""


def required_string(value: object) -> str:
    if not isinstance(value, str) or not value.strip() or '\0' in value:
        raise RuntimeError('Recorded runtime is invalid; reinstall from this repository.')
    return value


def absolute(value: object) -> str:
    value = required_string(value)
    if not Path(value).is_absolute():
        raise RuntimeError('Recorded runtime paths must be absolute.')
    return value


def saved_runtime() -> tuple[list[str], dict[str, str], Path]:
    # Do not construct Config from ambient overrides: manual commands must use
    # precisely the runtime recorded at installation, not the caller's shell.
    path = Path.home() / 'Library/LaunchAgents' / (LABEL + '.plist')
    if path.is_symlink() or not path.is_file():
        raise RuntimeError('A regular installed LaunchAgent was not found; run sh install.sh first.')
    with path.open('rb') as stream:
        raw = stream.read(MAX_JSON_BYTES + 1)
    if len(raw) > MAX_JSON_BYTES:
        raise RuntimeError('Installed LaunchAgent is too large to read safely.')
    config = plistlib.loads(raw)
    if not isinstance(config, dict) or config.get('Label') != LABEL:
        raise RuntimeError('Unexpected installed LaunchAgent.')
    env = config.get('EnvironmentVariables', {})
    args = config.get('ProgramArguments')
    if not isinstance(env, dict) or not isinstance(args, list):
        raise RuntimeError('Invalid installed LaunchAgent runtime.')
    # Discard ambient project overrides and Python injection variables. Nothing
    # is sourced/evaluated, and only supported saved variables are inherited.
    child_env = {k:v for k,v in os.environ.items()
                 if not k.startswith(('RUNCAT_', 'CODEX_'))
                 and k not in ('PYTHON_BIN', 'PYTHONPATH', 'PYTHONHOME')}
    if PROJECT == 'codex-runcat-neo':
        if len(args) != 3 or args[2] != '--refresh':
            raise RuntimeError('Unexpected Codex refresh command.')
        python, script = absolute(args[0]), absolute(args[1])
        if Path(script).name != 'runcat-neo-hook.py':
            raise RuntimeError('Unexpected installed Codex producer.')
        home = absolute(env.get('CODEX_HOME', str(Path(script).parent)))
        if Path(home) != Path(script).parent:
            raise RuntimeError('Recorded CODEX_HOME disagrees with the producer path.')
        child_env['CODEX_HOME'] = home
        for key in ('CODEX_BIN', 'PATH'):
            if key in env:
                child_env[key] = absolute(env[key]) if key == 'CODEX_BIN' else required_string(env[key])
        out = absolute(env.get('RUNCAT_OUT_FILE', str(Path(home) / 'runcat-usage.json')))
        command = [python, script, '--refresh']
    else:
        python = absolute(env.get('PYTHON_BIN'))
        script = absolute(env.get('RUNCAT_BATTERY_SCRIPT'))
        if Path(script).name != 'update-battery.py' or args != ['/bin/sh', str(Path(script).with_name('adaptive-poll.sh'))]:
            raise RuntimeError('Unexpected installed battery producer.')
        home = absolute(env.get('RUNCAT_HOME', str(Path.home() / '.runcat')))
        child_env['RUNCAT_HOME'] = home
        child_env['PYTHON_BIN'] = python
        child_env['RUNCAT_BATTERY_SCRIPT'] = script
        for key in ('RUNCAT_BATTERY_HISTORY_FILE', 'RUNCAT_BATTERY_INSTALL_DIR', 'RUNCAT_BATTERY_HEALTH_FILE'):
            if key in env:
                child_env[key] = absolute(env[key])
        out = absolute(env.get('RUNCAT_OUT_FILE', str(Path(home) / 'battery-power.json')))
        command = [python, script]
    child_env['RUNCAT_OUT_FILE'] = out
    child_env['PYTHONDONTWRITEBYTECODE'] = '1'
    return command, child_env, Path(out)


def snapshot(path: Path, since: float | None = None) -> dict:
    with path.open('rb') as stream:
        raw = stream.read(MAX_JSON_BYTES + 1)
    if len(raw) > MAX_JSON_BYTES:
        raise RuntimeError('Metric snapshot is too large to display.')
    value = json.loads(raw)
    if not isinstance(value, dict) or value.get('title') != TITLE or not isinstance(value.get('metrics'), list):
        raise RuntimeError('Unexpected metric snapshot.')
    if since is not None:
        stamp = datetime.fromisoformat(value['lastUpdatedDate'].replace('Z', '+00:00'))
        if stamp.tzinfo is None or not since - 1 <= stamp.timestamp() <= time.time() + 5:
            raise RuntimeError('The snapshot was not freshly updated.')
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('refresh', 'show', 'diagnose', 'refresh-health'))
    args = parser.parse_args(argv)
    command, env, out = saved_runtime()
    if args.action == 'show':
        result = snapshot(out)  # Last saved snapshot only; no account/sensor query.
    else:
        if args.action == 'refresh-health':
            health = subprocess.run(command + ['--refresh-health', '--force-health'], env=env,
                                    capture_output=True, text=True, timeout=25)
            if health.returncode:
                raise RuntimeError('macOS capacity refresh failed; the last observation is retained with its age. No raw report was logged.')
        if args.action == 'diagnose':
            command = ([command[0], '-B', '-c', CODEX_DIAGNOSTIC, command[1]]
                       if PROJECT == 'codex-runcat-neo' else command + ['--diagnose'])
        since = time.time()
        proc = subprocess.run(command, env=env, capture_output=True, text=True,
                              encoding='utf-8', timeout=20)
        if proc.returncode:
            raise RuntimeError('Installed command failed. Check the selected runtime and account/sensor availability. Raw output was not logged.')
        result = json.loads(proc.stdout) if args.action == 'diagnose' else snapshot(out, since)
        if not isinstance(result, dict):
            raise RuntimeError('Unexpected diagnostic response.')
    print(json.dumps(result, indent=2, ensure_ascii=False, allow_nan=False))
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as exc:
        print('RunCat command failed: ' + (str(exc) if isinstance(exc, RuntimeError)
              else 'could not read or run the installed configuration; raw details omitted'), file=sys.stderr)
        raise SystemExit(1)
