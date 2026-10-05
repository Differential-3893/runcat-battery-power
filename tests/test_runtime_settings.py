"""Real shell entrypoints + real installer; only native telemetry/launchd mocked."""
from __future__ import annotations
import json
import os
from pathlib import Path
import plistlib
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class RuntimeSettingsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='battery shell 한글 ')
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.layout = self.home / 'entrypoints'
        (self.layout / 'scripts').mkdir(parents=True)
        for name in ('install.sh', 'uninstall.sh'):
            shutil.copyfile(ROOT / name, self.layout / name)
        # Execute the production Config, install, verify, uninstall. Reuse the
        # existing synthetic sensor and fake launchctl, persisting launch state
        # between subprocesses. No real user launchctl or ioreg is accessed.
        runner = '''import json, os, sys\nfrom pathlib import Path\nfrom unittest.mock import patch
sys.path.insert(0, TESTS)
from test_install import manager, FakeLaunch, simulated_live_sample, simulated_live_health
state = Path(os.environ['HOME']) / 'fake-launch.json'
old = json.loads(state.read_text()) if state.exists() else {}
launch = FakeLaunch(old.get('loaded',False), old.get('disabled',False))
launch.health_loaded=old.get('health_loaded',False)
launch.health_disabled=old.get('health_disabled',False)
try:
    with patch.object(manager, 'require_native'), patch.object(manager, 'launch', launch), patch.object(manager, 'live_sample', side_effect=simulated_live_sample), patch.object(manager, 'live_health', side_effect=simulated_live_health):
        manager.main()
        (state.parent / 'resolved-python.txt').write_text(manager.Config().python)
finally:
    state.write_text(json.dumps({'loaded': launch.is_loaded, 'disabled': launch.is_disabled, 'health_loaded':launch.health_loaded, 'health_disabled':launch.health_disabled}))
'''.replace('TESTS', repr(str(ROOT / 'tests')))
        (self.layout / 'scripts/manage_install.py').write_text(runner)
        self.bin = self.home / 'first bin'
        self.bin.mkdir()
        (self.bin / 'python3').symlink_to(sys.executable)
        self.alias = self.home / "python custom '& 한글"
        self.alias.symlink_to(sys.executable)
        self.env = {k:v for k,v in os.environ.items() if not k.startswith('RUNCAT_') and k not in ('PYTHON_BIN', 'PYTHONPATH', 'PYTHONHOME')}
        self.env.update(HOME=str(self.home), PATH=str(self.bin)+os.pathsep+os.environ.get('PATH',os.defpath), PYTHONDONTWRITEBYTECODE='1')
        self.plist = self.home / 'Library/LaunchAgents/dev.runcat.battery-power.plist'

    def run_setup(self, env=None, uninstall=False, expected=0):
        args = ['sh', str(self.layout / ('uninstall.sh' if uninstall else 'install.sh'))]
        if uninstall: args.append('--keep-data')
        r = subprocess.run(args, env=env or self.env, capture_output=True, text=True, timeout=15)
        self.assertEqual(r.returncode, expected, r.stdout+r.stderr)
        return r

    def payload(self):
        return plistlib.loads(self.plist.read_bytes())

    def changed_shell(self):
        other = self.home / 'second bin'
        other.mkdir(exist_ok=True)
        if not (other / 'python3').exists(): (other / 'python3').symlink_to(sys.executable)
        return {**self.env, 'PATH': str(other)+os.pathsep+self.env['PATH']}

    def test_plain_shell_reinstall_keeps_saved_python_and_all_paths(self):
        custom = self.home / "custom & quoted ' directory"
        env = {**self.env, 'PYTHON_BIN': str(self.alias), 'RUNCAT_HOME': str(custom),
               'RUNCAT_OUT_FILE': str(custom / 'out/metric.json'),
               'RUNCAT_BATTERY_HISTORY_FILE': str(custom / 'state/history.json'),
               'RUNCAT_BATTERY_INSTALL_DIR': str(custom / 'bin')}
        self.run_setup(env)
        before = self.payload()
        self.run_setup(self.changed_shell())
        self.assertEqual(self.payload(), before)
        self.assertEqual(self.payload()['EnvironmentVariables']['PYTHON_BIN'], str(self.alias))

    def test_explicit_python_override_still_wins(self):
        self.run_setup()
        before = self.payload()
        self.run_setup({**self.env, 'PYTHON_BIN': str(self.alias)})
        after = self.payload()
        self.assertEqual(after['EnvironmentVariables']['PYTHON_BIN'], str(self.alias))
        for key in ('RUNCAT_HOME', 'RUNCAT_OUT_FILE', 'RUNCAT_BATTERY_HISTORY_FILE', 'RUNCAT_BATTERY_INSTALL_DIR'):
            self.assertEqual(after['EnvironmentVariables'][key], before['EnvironmentVariables'][key])

    def test_output_override_does_not_reset_saved_python(self):
        self.run_setup({**self.env, 'PYTHON_BIN': str(self.alias)})
        output = self.home / 'new output.json'
        self.run_setup({**self.changed_shell(), 'RUNCAT_OUT_FILE': str(output)})
        env = self.payload()['EnvironmentVariables']
        self.assertEqual(env['PYTHON_BIN'], str(self.alias))
        self.assertEqual(env['RUNCAT_OUT_FILE'], str(output))
        self.assertTrue(output.exists())

    def test_empty_overrides_fail_without_changing_existing_plist(self):
        self.run_setup()
        before = self.plist.read_bytes()
        for key in ('PYTHON_BIN', 'RUNCAT_HOME', 'RUNCAT_OUT_FILE', 'RUNCAT_BATTERY_HISTORY_FILE', 'RUNCAT_BATTERY_INSTALL_DIR'):
            with self.subTest(key=key):
                self.run_setup({**self.env, key:''}, expected=1)
                self.assertEqual(self.plist.read_bytes(), before)

    def test_missing_saved_python_stops_without_silent_fallback(self):
        self.run_setup({**self.env, 'PYTHON_BIN': str(self.alias)})
        before = self.plist.read_bytes()
        self.alias.unlink()
        self.run_setup(self.changed_shell(), expected=1)
        self.assertEqual(self.plist.read_bytes(), before)

    def test_uninstall_entrypoint_does_not_inject_bootstrap_python(self):
        # Inspect the real Config immediately on entry, before uninstall removes
        # its plist. The original wrapper incorrectly injects PATH's Python.
        self.run_setup({**self.env, 'PYTHON_BIN': str(self.alias)})
        runner = self.layout / 'scripts/manage_install.py'
        text = runner.read_text()
        text = text.replace('manager.main()', "(state.parent / 'before-uninstall.json').write_text(json.dumps(manager.Config().variables()))\n        manager.main()")
        runner.write_text(text)
        self.run_setup(self.changed_shell(), uninstall=True)
        before = json.loads((self.home / 'before-uninstall.json').read_text())
        self.assertEqual(before['PYTHON_BIN'], str(self.alias))
        self.assertFalse(self.plist.exists())
        self.assertTrue(Path(before['RUNCAT_OUT_FILE']).exists())

    def test_repeated_plain_reinstall_is_configuration_idempotent(self):
        self.run_setup({**self.env, 'PYTHON_BIN': str(self.alias)})
        before = self.payload()
        self.run_setup(self.changed_shell())
        self.run_setup(self.changed_shell())
        self.assertEqual(self.payload(), before)


if __name__ == '__main__':
    unittest.main()
