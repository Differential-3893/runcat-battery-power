import contextlib
import importlib.util
import json
import os
from pathlib import Path
import plistlib
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("battery_manager", ROOT / "scripts/manage_install.py")
manager = importlib.util.module_from_spec(spec)
spec.loader.exec_module(manager)


class FakeLaunch:
    def __init__(self, loaded=False, disabled=False):
        self.is_loaded = loaded
        self.is_disabled = disabled
        self.fail_bootstraps = 0
        self.calls = []

    def __call__(self, *args, check=True):
        self.calls.append(args)
        rc, output = 0, ""
        if args[0] == "print":
            rc = 0 if self.is_loaded else 1
            output = "state = running\npid = 12345\n" if self.is_loaded else ""
        elif args[0] == "print-disabled":
            output = f'"{manager.LABEL}" => {str(self.is_disabled).lower()}\n'
        elif args[0] == "bootout":
            self.is_loaded = False
        elif args[0] == "bootstrap":
            if self.fail_bootstraps:
                self.fail_bootstraps -= 1
                rc = 1
            else:
                self.is_loaded = True
        elif args[0] == "enable":
            self.is_disabled = False
        elif args[0] == "disable":
            self.is_disabled = True
        if check and rc:
            raise RuntimeError("simulated launchctl failure")
        return subprocess.CompletedProcess([], rc, output, "")


def simulated_live_sample(script, cfg, out=None, history=None):
    """Execute the real producer with synthetic telemetry and real temp files."""
    spec = importlib.util.spec_from_file_location("installed_battery_under_test", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    out = cfg.out if out is None else out
    history = cfg.history if history is None else history
    text = ('"BatteryInstalled" = Yes\n"ExternalConnected" = No\n"IsCharging" = No\n'
            '"Temperature" = 3049\n"Voltage" = 12000\n"InstantAmperage" = -1000\n'
            '"AppleRawCurrentCapacity" = 4000\n')
    with patch.object(module, "OUT", out), patch.object(module, "HISTORY", history), \
         patch.object(module, "run_ioreg", return_value=text):
        module.sample_once()
    return manager.snapshot_check(out, manager.time.time() - 1)


class InstallTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="battery paths with spaces ")
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        env = {k: v for k, v in os.environ.items() if not k.startswith("RUNCAT_") and k != "PYTHON_BIN"}
        env.update(HOME=str(self.home))
        self.stack.enter_context(patch.dict(os.environ, env, clear=True))
        self.launch = FakeLaunch()
        self.stack.enter_context(patch.object(manager, "launch", self.launch))
        self.stack.enter_context(patch.object(manager, "live_sample", side_effect=simulated_live_sample))
        self.stack.enter_context(contextlib.redirect_stdout(__import__("io").StringIO()))
        self.stack.enter_context(contextlib.redirect_stderr(__import__("io").StringIO()))

    def old_install(self, cfg):
        cfg.install.mkdir(parents=True, exist_ok=True)
        cfg.plist.parent.mkdir(parents=True, exist_ok=True)
        for name in manager.OWNED_SCRIPTS:
            (cfg.install / name).write_text("old " + name)
            (cfg.install / name).chmod(0o700)
        cfg.plist.write_bytes(plistlib.dumps(cfg.payload()))
        cfg.out.parent.mkdir(parents=True, exist_ok=True)
        cfg.history.parent.mkdir(parents=True, exist_ok=True)
        cfg.out.write_text('{"old":true}')
        cfg.history.write_text('[]')
        return {p: p.read_bytes() for p in cfg.targets()}

    def test_fresh_install_live_check_and_permissions(self):
        cfg = manager.Config()
        manager.install(cfg)
        self.assertTrue(self.launch.is_loaded)
        self.assertEqual(cfg.payload(), plistlib.loads(cfg.plist.read_bytes()))
        for name in manager.OWNED_SCRIPTS:
            self.assertEqual((cfg.install / name).read_bytes(), (ROOT / name).read_bytes())
            self.assertEqual((cfg.install / name).stat().st_mode & 0o777, 0o755)
        data = json.loads(cfg.out.read_text())
        self.assertEqual(data["metrics"][0]["formattedValue"], "12.0 W")
        self.assertEqual(data["metrics"][-1]["formattedValue"], "30.5 °C")

    def test_custom_paths_persist_and_unrelated_metrics_untouched(self):
        custom = self.home / "custom & quoted ' directory"
        with patch.dict(os.environ, {
            "RUNCAT_HOME": str(custom), "RUNCAT_OUT_FILE": str(custom / "output elsewhere" / "watts.json"),
            "RUNCAT_BATTERY_HISTORY_FILE": str(custom / "history elsewhere" / "samples.json"),
            "RUNCAT_BATTERY_INSTALL_DIR": str(custom / "tools")
        }):
            cfg = manager.Config()
            other = custom / "codex-runcat.json"
            custom.mkdir(parents=True)
            other.write_text("unrelated metric")
            manager.install(cfg)
        # No exported RUNCAT variables required on later repair/verification.
        new = manager.Config()
        self.assertEqual((new.home, new.out, new.history, new.install), (cfg.home, cfg.out, cfg.history, cfg.install))
        self.assertEqual(other.read_text(), "unrelated metric")
        self.assertFalse((self.home / ".runcat" / "battery-power.json").exists())
        self.assertEqual(new.payload()["EnvironmentVariables"]["RUNCAT_BATTERY_HISTORY_FILE"], str(cfg.history))
        manager.verify(new)

    def test_verification_reuses_recorded_python_alias(self):
        cfg = manager.Config()
        alias = self.home / "stable python link"
        alias.symlink_to(sys.executable)
        cfg.python = str(alias)
        self.old_install(cfg)
        recovered = manager.Config()
        self.assertEqual(recovered.python, str(alias))
        self.assertEqual(recovered.payload(), cfg.payload())

    def test_legacy_plist_without_history_setting_migrates(self):
        cfg = manager.Config()
        self.old_install(cfg)
        payload = cfg.payload()
        del payload["EnvironmentVariables"]["RUNCAT_BATTERY_HISTORY_FILE"]
        del payload["EnvironmentVariables"]["RUNCAT_BATTERY_INSTALL_DIR"]
        cfg.plist.write_bytes(plistlib.dumps(payload))
        migrated = manager.Config()
        self.assertEqual(migrated.history, cfg.home / "battery-power-history.json")
        self.assertEqual(migrated.install, cfg.install)
        manager.install(migrated)

    def test_preflight_failure_does_not_modify_or_stop_old_install(self):
        cfg = manager.Config()
        old = self.old_install(cfg)
        self.launch.is_loaded = True
        with patch.object(manager, "live_sample", side_effect=RuntimeError("no telemetry")), self.assertRaises(RuntimeError):
            manager.install(cfg)
        self.assertTrue(self.launch.is_loaded)
        self.assertEqual(self.launch.calls, [])
        for p, raw in old.items():
            self.assertEqual(p.read_bytes(), raw)
        self.assertFalse((cfg.home / "backups").exists())

    def test_bootstrap_failure_restores_old_files_and_running_job(self):
        cfg = manager.Config()
        old = self.old_install(cfg)
        self.launch.is_loaded = True
        self.launch.fail_bootstraps = 1
        with self.assertRaises(RuntimeError):
            manager.install(cfg)
        self.assertTrue(self.launch.is_loaded)
        for p, raw in old.items():
            self.assertEqual(p.read_bytes(), raw)
        self.assertEqual((cfg.install / "update-battery.py").stat().st_mode & 0o777, 0o700)
        self.assertTrue(list((cfg.home / "backups").glob("*/RESTORE.json")))

    def test_failed_fresh_install_restores_absence_and_disabled_flag(self):
        cfg = manager.Config()
        self.launch.is_disabled = True
        self.launch.fail_bootstraps = 1
        with self.assertRaises(RuntimeError):
            manager.install(cfg)
        self.assertFalse(self.launch.is_loaded)
        self.assertTrue(self.launch.is_disabled)
        for path in cfg.targets():
            self.assertFalse(path.exists())

    def test_final_verification_failure_rolls_back(self):
        cfg = manager.Config()
        old = self.old_install(cfg)
        self.launch.is_loaded = True
        with patch.object(manager, "verify", side_effect=RuntimeError("verification failed")), self.assertRaises(RuntimeError):
            manager.install(cfg)
        for p, raw in old.items():
            self.assertEqual(p.read_bytes(), raw)
        self.assertTrue(self.launch.is_loaded)

    def test_invalid_or_symlink_configuration_rejected_before_write(self):
        cfg = manager.Config()
        cfg.plist.parent.mkdir(parents=True)
        cfg.plist.write_text("broken")
        with self.assertRaises(RuntimeError):
            manager.Config()
        cfg.plist.unlink()
        cfg.out.parent.mkdir(parents=True)
        target = self.home / "unrelated.json"
        target.write_text("untouched")
        cfg.out.symlink_to(target)
        with self.assertRaises(RuntimeError):
            manager.Config()
        self.assertEqual(target.read_text(), "untouched")

    def test_conflicting_output_and_history_rejected(self):
        with patch.dict(os.environ, {"RUNCAT_OUT_FILE": str(self.home / "same"),
                                    "RUNCAT_BATTERY_HISTORY_FILE": str(self.home / "same")}):
            with self.assertRaises(RuntimeError):
                manager.Config()

    def test_uninstall_keeps_data_when_requested_and_never_deletes_neighbors(self):
        cfg = manager.Config()
        manager.install(cfg)
        other = cfg.install / "unrelated-user-file"
        other.write_text("untouched")
        data = cfg.out.read_bytes()
        manager.uninstall(cfg, True)
        self.assertFalse(self.launch.is_loaded)
        self.assertFalse(cfg.plist.exists())
        self.assertEqual(cfg.out.read_bytes(), data)
        self.assertTrue(cfg.history.exists())
        self.assertEqual(other.read_text(), "untouched")

    def test_default_uninstall_removes_only_owned_data_with_backup(self):
        cfg = manager.Config()
        manager.install(cfg)
        other = cfg.home / "codex.json"
        other.write_text("untouched")
        manager.uninstall(cfg, False)
        for path in cfg.targets():
            self.assertFalse(path.exists())
        self.assertEqual(other.read_text(), "untouched")
        self.assertTrue(list((cfg.home / "backups").glob("*/RESTORE.json")))

    def test_verify_detects_stale_snapshot_and_changed_script(self):
        cfg = manager.Config()
        manager.install(cfg)
        data = json.loads(cfg.out.read_text())
        data["lastUpdatedDate"] = "2000-01-01T00:00:00Z"
        cfg.out.write_text(json.dumps(data))
        with self.assertRaises(RuntimeError):
            manager.snapshot_check(cfg.out, manager.time.time())
        (cfg.install / "update-battery.py").write_text("changed")
        with self.assertRaises(RuntimeError):
            manager.verify(cfg)

    def test_live_subprocess_environment_is_explicit(self):
        # Undo only live_sample's outer mock to inspect the real subprocess API.
        spec = importlib.util.spec_from_file_location("fresh_manager", ROOT / "scripts/manage_install.py")
        fresh = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(fresh)
        cfg = manager.Config()
        with patch.object(fresh.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "", "")) as run, \
             patch.object(fresh, "snapshot_check", return_value={}):
            fresh.live_sample(ROOT / "update-battery.py", cfg)
        env = run.call_args.kwargs["env"]
        self.assertEqual(env["RUNCAT_HOME"], str(cfg.home))
        self.assertEqual(env["RUNCAT_OUT_FILE"], str(cfg.out))
        self.assertEqual(env["RUNCAT_BATTERY_HISTORY_FILE"], str(cfg.history))
        self.assertEqual(run.call_args.args[0], [cfg.python, str(ROOT / "update-battery.py")])


    def test_output_cannot_replace_python_log_or_lock(self):
        cfg = manager.Config()
        for target in (Path(cfg.python), cfg.install/'stdout.log', cfg.install/'stderr.log',
                       cfg.history.with_name('.'+cfg.history.name+'.lock')):
            with self.subTest(path=target), patch.dict(os.environ,{'RUNCAT_OUT_FILE':str(target)}):
                with self.assertRaises(RuntimeError):manager.Config()

    def test_history_cannot_replace_a_runtime_executable(self):
        cfg = manager.Config()
        with patch.dict(os.environ,{'RUNCAT_BATTERY_HISTORY_FILE':str(cfg.python)}):
            with self.assertRaises(RuntimeError):manager.Config()


if __name__ == "__main__":
    unittest.main()
