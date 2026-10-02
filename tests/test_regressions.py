"""Regression cases for real defects reproduced on the pinned baseline."""
import contextlib
import importlib.util
import json
import math
import os
from pathlib import Path
import random
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("battery_regression", ROOT / "update-battery.py")
battery = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(battery)


def row(t, p=10, state="discharging"):
    return {"timestamp": t, "powerW": p, "state": state}


class RegressionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.out = Path(self.tmp.name) / "snapshot.json"
        self.history = Path(self.tmp.name) / "history.json"
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(battery, "OUT", self.out))
        self.stack.enter_context(patch.object(battery, "HISTORY", self.history))

    def save(self, rows):
        self.history.write_text(json.dumps(rows))

    def test_sleep_gap_restarts_runtime_warmup(self):
        self.save([row(t) for t in range(700, 730, 5)])
        history = battery.update_history(1000, 20, "discharging")
        self.assertEqual(history, [row(1000, 20)])
        avg, peak, coverage, count = battery.five_minute_stats(history, 1000, "discharging")
        self.assertEqual((avg, peak, coverage, count), (20, 20, 0, 1))
        self.assertEqual(battery.format_runtime(50 / avg, "discharging", coverage, count), "Calculating…")

    def test_per_state_gap_tolerance(self):
        for state, gap in (("discharging", 15), ("charging", 180)):
            with self.subTest(state=state):
                self.save([row(1000 - gap, 10, state)])
                self.assertEqual(len(battery.update_history(1000, 20, state)), 2)
                self.save([row(999 - gap, 10, state)])
                self.assertEqual(len(battery.update_history(1000, 20, state)), 1)

    def test_nonfinite_corrupt_and_future_history(self):
        bad_rows = [row(995, float("nan")), row(995, float("inf")), row(995, -1),
                    row(995, 200), row(float("inf")), row(995, 10, []),
                    row(995, True), row(True), row(10**400), row(995, 10**400),
                    row(1005), "bad", None, {}, row(995, 10, "ac")]
        for bad in bad_rows:
            with self.subTest(bad=bad):
                self.save([row(990), bad])
                data = battery.update_history(1000, 20, "discharging")
                self.assertEqual(data, [row(1000, 20)])
                self.assertNotIn("NaN", self.history.read_text())
                self.assertNotIn("Infinity", self.history.read_text())

    def test_bad_history_file_recovers(self):
        for raw in (b'\xff', b'{"not":"a list"}', b'[', b'[' * 1100):
            with self.subTest(raw=raw[:20]):
                self.history.write_bytes(raw)
                self.assertEqual(battery.update_history(1000, 20, "discharging"), [row(1000, 20)])
        self.history.write_bytes(b' ' * (battery.MAX_HISTORY_BYTES + 1))
        self.assertEqual(battery.load_history(), [])

    def test_missing_power_clears_estimate_history(self):
        for power in (None, float("nan"), float("inf"), True, -1, 200):
            self.save([row(995)])
            self.assertEqual(battery.update_history(1000, power, "discharging"), [])

    def test_power_state_transition_does_not_mix_sessions(self):
        self.save([row(t) for t in range(800, 1000, 5)])
        self.assertEqual(battery.update_history(1000, 10, "ac"), [])
        self.assertEqual(battery.update_history(1005, 12, "discharging"), [row(1005, 12)])
        self.assertEqual(battery.update_history(1010, 50, "charging"), [row(1010, 50, "charging")])

    def test_duplicate_time_and_clock_rewind(self):
        self.save([row(995), row(1000, 12)])
        self.assertEqual(battery.update_history(1000, 20, "discharging"), [row(995), row(1000, 20)])
        self.save([row(980), row(985), row(970)])
        self.assertEqual(battery.update_history(975, 20, "discharging"), [row(970), row(975, 20)])

    def test_clipped_trapezoid_at_five_minute_boundary(self):
        # p(t)=t/10. The precise average over [300,600] is 45 W.
        samples = [row(t, t / 10) for t in range(295, 606, 10)]
        samples = [p for p in samples if p["timestamp"] <= 595] + [row(600, 60)]
        avg, peak, coverage, count = battery.five_minute_stats(samples, 600, "discharging")
        self.assertAlmostEqual(avg, 45)
        self.assertEqual((peak, coverage, count), (60, 300, 31))

    def test_random_weighted_average_matches_independent_clipped_integrals(self):
        rng = random.Random(3893)
        for _ in range(100):
            samples, t = [], 0.0
            while t < 600:
                samples.append((t, rng.uniform(0, 150)))
                t += rng.uniform(1, 14)
            samples.append((600, rng.uniform(0, 150)))
            area = 0.0
            for (t0, p0), (t1, p1) in zip(samples, samples[1:]):
                left, right = max(300, t0), min(600, t1)
                if left < right:
                    midpoint = (left + right) / 2
                    pm = p0 + (p1 - p0) * (midpoint - t0) / (t1 - t0)
                    area += pm * (right - left)
            avg, peak, coverage, count = battery.five_minute_stats([row(t, p) for t, p in samples], 600, "discharging")
            self.assertAlmostEqual(avg, area / 300, places=10)
            self.assertEqual(coverage, 300)
            self.assertEqual(count, sum(t >= 300 for t, _ in samples))
            self.assertTrue(0 <= avg <= peak < 200)

    def test_stats_do_not_accept_unobserved_future_or_stale_periods(self):
        self.assertEqual(battery.five_minute_stats([row(995), row(1010)], 1000, "discharging"), (None, None, 0, 0))
        self.assertEqual(battery.five_minute_stats([row(900)], 1000, "discharging"), (None, None, 0, 0))
        self.assertEqual(battery.five_minute_stats([row(995)], 1000, "ac"), (None, None, 0, 0))

    def test_zero_current_is_valid_and_not_replaced_with_old_amperage(self):
        for amperage in (0, -1000):
            text = f'"Voltage" = 12000\n"InstantAmperage" = 0\n"Amperage" = {amperage}\n'
            self.assertEqual(battery.battery_power_w(text), (0.0, "Voltage×Current"))

    def test_scoped_fields_do_not_read_adapter_or_lifetime_values(self):
        text = ('"AdapterDetails" = {"Voltage"=5000,"IsCharging"=Yes}\n'
                '"Voltage" = 12000\n"InstantAmperage" = -1000\n'
                '"ExternalConnected" = No\n"IsCharging" = No\n')
        self.assertEqual(battery.battery_power_w(text), (12.0, "Voltage×Current"))
        self.assertEqual(battery.battery_state(text), "discharging")
        self.assertIsNone(battery.raw_number('"AdapterDetails" = {"RemainingCapacity"=5000}', "RemainingCapacity"))
        text = ('"BatteryData" = {"LifetimeData"={"BatteryPower"=99000},'
                '"Comment"="text, with {braces}","BatteryPower"=23000}\n')
        self.assertEqual(battery.battery_power_w(text), (23.0, "BatteryPower"))

    def test_numeric_and_boolean_parsers_require_whole_values(self):
        for value in ("1.2", "1oops", "true", '"12"', "9" * 5000):
            self.assertIsNone(battery.raw_number(f'"Voltage" = {value}', "Voltage"))
        for value in ("10", "trueish", '"Yes"'):
            self.assertIsNone(battery.raw_bool(f'"IsCharging" = {value}', "IsCharging"))
        self.assertIsNone(battery.raw_number('"Voltage" = 1\n"Voltage" = 2', "Voltage"))
        self.assertIsNone(battery.dictionary_literal('{"BatteryPower"=1,"BatteryPower"=2}', "BatteryPower"))

    def test_unsigned_sign_and_units_are_unchanged(self):
        raw = (1 << 64) - 23908
        self.assertEqual(battery.battery_power_w(f'"BatteryData" = {{"BatteryPower"={raw}}}'), (23.908, "BatteryPower"))
        self.assertEqual(battery.signed_64(1 << 63), -(1 << 63))
        self.assertIsNone(battery.signed_64(1 << 64))
        self.assertEqual(battery.battery_power_w('"BatteryPower" = 0'), (0, "BatteryPower"))

    def test_capacity_fallback_and_bounded_corrupt_values(self):
        cases = [
            ('"AppleRawCurrentCapacity" = 4000', 48),
            ('"BatteryData" = {"RemainingCapacity"=4000}', 48),
            ('"CurrentCapacity" = 4000\n"MaxCapacity" = 5000', 48),
            ('"NominalChargeCapacity" = 5000\n"StateOfCharge" = 50', 30),
        ]
        for props, expected in cases:
            self.assertAlmostEqual(battery.battery_remaining_energy_wh('"Voltage" = 12000\n' + props)[0], expected)
        self.assertIsNone(battery.battery_remaining_energy_wh('"Voltage" = ' + '9' * 5000)[0])

    def test_runtime_confidence_and_labels(self):
        self.assertEqual(battery.format_runtime(6.5, "discharging", 59, 12), "Calculating…")
        self.assertEqual(battery.format_runtime(6.5, "discharging", 120, 25), "~6h 30m")
        self.assertEqual(battery.format_runtime(6.5, "discharging", 240, 49), "6h 30m")
        self.assertEqual(battery.format_runtime(None, "charging", 0, 0), "Charging")
        self.assertEqual(battery.format_runtime(None, "ac", 0, 0), "On AC")
        self.assertEqual(battery.format_runtime(float("nan"), "discharging", 240, 49), "—")

    def test_atomic_write_failure_preserves_old_file(self):
        self.out.write_bytes(b'old snapshot')
        with self.assertRaises(ValueError):
            battery.atomic_write_json(self.out, {"bad": float("nan")})
        self.assertEqual(self.out.read_bytes(), b'old snapshot')
        self.assertEqual(list(Path(self.tmp.name).iterdir()), [self.out])
        with patch.object(battery.os, "replace", side_effect=OSError("disk error")):
            with self.assertRaises(OSError):
                battery.atomic_write_json(self.out, {"good": True})
        self.assertEqual(self.out.read_bytes(), b'old snapshot')

    def test_ioreg_failure_preserves_snapshot_without_logging_raw_stderr(self):
        self.out.write_bytes(b'old snapshot')
        with patch.object(battery, "run_ioreg", side_effect=RuntimeError("PRIVATE_SENTINEL")):
            with self.assertRaises(RuntimeError):
                battery.sample_once()
        self.assertEqual(self.out.read_bytes(), b'old snapshot')
        with patch.object(battery.subprocess, "run", return_value=subprocess.CompletedProcess([], 1, "", "PRIVATE_SENTINEL")):
            with self.assertRaisesRegex(RuntimeError, '^Unable to read AppleSmartBattery telemetry$'):
                battery.run_ioreg()

    def test_two_process_lock_excludes_concurrent_writer_and_releases(self):
        code = '''import importlib.util,sys\nfrom pathlib import Path\ns=importlib.util.spec_from_file_location("b",sys.argv[1]);b=importlib.util.module_from_spec(s);s.loader.exec_module(b)\ntry:\n with b.sample_lock(Path(sys.argv[2]), timeout=0.15): print("ACQUIRED")\nexcept TimeoutError: print("BUSY")\n'''
        command = [sys.executable, "-B", "-c", code, str(ROOT / "update-battery.py"), str(self.history)]
        with battery.sample_lock():
            result = subprocess.run(command, text=True, capture_output=True, timeout=5)
            self.assertEqual(result.stdout.strip(), "BUSY")
        result = subprocess.run(command, text=True, capture_output=True, timeout=5)
        self.assertEqual(result.stdout.strip(), "ACQUIRED")

    def test_identical_output_and_history_paths_are_rejected(self):
        with patch.object(battery, "OUT", self.history), self.assertRaises(ValueError):
            battery.sample_once()

    def test_lock_symlink_is_not_followed(self):
        victim = Path(self.tmp.name) / "unrelated.txt"
        victim.write_text("do not touch")
        self.history.with_name("." + self.history.name + ".lock").symlink_to(victim)
        with self.assertRaises(OSError):
            with battery.sample_lock():
                pass
        self.assertEqual(victim.read_text(), "do not touch")


class PollLoopTests(unittest.TestCase):
    def test_delay_is_allowlisted_and_term_exits(self):
        for output, status, expected in (("5", 0, "5"), ("60", 0, "60"), ("0", 0, "60"),
                                         ("999999999999999999999999", 0, "60"), ("5\n5", 0, "60"), ("", 1, "60")):
            with self.subTest(output=output), tempfile.TemporaryDirectory() as td:
                tmp = Path(td)
                stub = tmp / "python stub"
                stub.write_text(f'#!{sys.executable}\nimport sys\nprint({output!r})\nsys.exit({status})\n')
                stub.chmod(0o755)
                sleeper = tmp / "sleep"
                sleeper.write_text(f'#!{sys.executable}\nimport os,signal,sys\nfrom pathlib import Path\nPath(os.environ["DELAY_LOG"]).write_text(sys.argv[1])\nos.kill(os.getppid(),signal.SIGTERM)\n')
                sleeper.chmod(0o755)
                log = tmp / "delay.txt"
                env = {**os.environ, "PYTHON_BIN": str(stub), "RUNCAT_BATTERY_SCRIPT": str(stub),
                       "PATH": str(tmp) + os.pathsep + os.environ["PATH"], "DELAY_LOG": str(log)}
                result = subprocess.run(["/bin/sh", str(ROOT / "adaptive-poll.sh")], env=env,
                                        capture_output=True, text=True, timeout=5)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(log.read_text(), expected)


if __name__ == "__main__":
    unittest.main()
