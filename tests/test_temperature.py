import contextlib
import copy
import importlib.util
import io
import json
import plistlib
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("battery", ROOT / "update-battery.py")
battery = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(battery)
FIXTURES = json.loads(
    (ROOT / "tests/fixtures/systeminfokit_temperatures.json").read_text()
)
PACK_SOURCE = "AppleSmartBatteryPack.BatteryData.Temperature"


def ioreg_value(value):
    if isinstance(value, dict):
        return "{" + ",".join(
            f'"{key}"={ioreg_value(item)}' for key, item in value.items()
        ) + "}"
    if isinstance(value, bool):
        return "Yes" if value else "No"
    return json.dumps(value)


def primary_text(properties):
    # Render the public JSON excerpts in ioreg's text format (inline dicts).
    return '+-o AppleSmartBattery <class AppleSmartBattery>\n  {\n' + "\n".join(
        f'    "{key}" = {ioreg_value(value)}' for key, value in properties.items()
    ) + "\n  }\n"


def pack_text(value):
    return plistlib.dumps(value).decode("utf-8")


def response(stdout, returncode=0, stderr=""):
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


class TemperatureTests(unittest.TestCase):
    def test_public_measured_values_and_query_counts(self):
        for case in FIXTURES:
            with self.subTest(capture=case["capture"]):
                outputs = [response(primary_text(case["AppleSmartBattery"]))]
                pack = case.get("AppleSmartBatteryPack")
                if pack:
                    outputs.append(response(pack_text([pack])))
                with patch.object(battery.subprocess, "run", side_effect=outputs) as run:
                    text = battery.run_ioreg()
                    value, source = battery.read_battery_temperature_c(text)
                expected = case["expected_temperature_c"]
                if expected is None:
                    self.assertIsNone(value)
                    self.assertEqual(source, "Unavailable")
                else:
                    self.assertAlmostEqual(value, expected)
                    self.assertEqual(source, PACK_SOURCE if pack else "Temperature")
                self.assertEqual(run.call_count, 2 if pack else 1)
                for index, call in enumerate(run.call_args_list):
                    service = "AppleSmartBatteryPack" if index else "AppleSmartBattery"
                    command = [battery.IOREG, "-rn", service, "-l", "-d", "1", "-w", "0"]
                    if index:
                        command.append("-a")
                    self.assertEqual(call.args[0], command)
                    self.assertEqual(call.kwargs["timeout"], 2)

    def test_invalid_primary_and_virtual_only_use_pack(self):
        for raw in (None, 0, 65535, -2001, 10001, True, "3049", "bad", 10**400):
            with self.subTest(raw=raw):
                properties = {"VirtualTemperature": 3829, "BatteryInstalled": True}
                if raw is not None:
                    properties["Temperature"] = raw
                with patch.object(battery, "run_ioreg", return_value=pack_text([
                    {"BatteryData": {"Temperature": 3359}}
                ])) as run:
                    self.assertEqual(
                        battery.read_battery_temperature_c(primary_text(properties)),
                        (33.59, PACK_SOURCE),
                    )
                    run.assert_called_once_with("AppleSmartBatteryPack", archive=True)

    def test_temperature_scope_excludes_other_dictionaries(self):
        text = primary_text({
            "AdapterDetails": {"Temperature": 9000},
            "BatteryData": {"Temperature": 8000},
            "VirtualTemperature": 3829,
            "Temperature": 3049,
        })
        with patch.object(battery, "run_ioreg") as run:
            self.assertEqual(battery.read_battery_temperature_c(text), (30.49, "Temperature"))
            run.assert_not_called()
        self.assertEqual(
            battery.battery_temperature_c(primary_text({"BatteryData": {"Temperature": 8000}})),
            (None, "Unavailable"),
        )

    def test_pack_temperature_is_scoped_to_battery_data(self):
        pack = {
            "Temperature": 9000,
            "AdapterDetails": {"Temperature": 8000},
            "BatteryData": {
                "LifetimeData": {"Temperature": 7000},
                "VirtualTemperature": 6500,
                "Temperature": 2989,
            },
        }
        with patch.object(battery, "run_ioreg", return_value=pack_text([pack])):
            self.assertEqual(battery.read_battery_temperature_c(""), (29.89, PACK_SOURCE))

    def test_invalid_pack_shapes_and_values_are_unavailable(self):
        packs = [[], {}, [1], [{}, {}], [{}], [{"BatteryData": []}],
                 [{"Temperature": 3049}],
                 [{"BatteryData": {"VirtualTemperature": 3359}}],
                 [{"BatteryData": {"LifetimeData": {"Temperature": 3359}}}]]
        for raw in (0, 65535, -2001, 10001, True, "3359", [], {}, float("nan"), float("inf")):
            packs.append([{"BatteryData": {"Temperature": raw}}])
        for pack in packs:
            with self.subTest(pack=pack), patch.object(battery, "run_ioreg", return_value=pack_text(pack)):
                self.assertEqual(battery.read_battery_temperature_c(""), (None, "Unavailable"))

    def test_pack_failures_are_nonfatal(self):
        for output in ("", "not a plist", "<?xml version='1.0'?><plist><dict>",
                       "<?xml version='1.0'?><plist><integer>bad</integer></plist>"):
            with self.subTest(output=output), patch.object(battery, "run_ioreg", return_value=output):
                self.assertEqual(battery.read_battery_temperature_c(""), (None, "Unavailable"))
        for error in (OSError("missing"), subprocess.TimeoutExpired("ioreg", 2),
                      RuntimeError("unavailable"), UnicodeError("invalid encoding")):
            with self.subTest(error=error), patch.object(battery, "run_ioreg", side_effect=error):
                self.assertEqual(battery.read_battery_temperature_c(""), (None, "Unavailable"))

    def test_no_battery_skips_pack_even_with_temperature(self):
        with patch.object(battery, "run_ioreg") as run:
            self.assertEqual(battery.read_battery_temperature_c(primary_text({
                "BatteryInstalled": False, "Temperature": 3049,
            })), (None, "Unavailable"))
            run.assert_not_called()

    def test_valid_bounds_and_no_unit_guessing(self):
        for raw, expected in ((-2000, -20.0), (-100, -1.0), (10000, 100.0), (3049, 30.49)):
            with self.subTest(raw=raw):
                self.assertEqual(battery.battery_temperature_c(primary_text({
                    "Temperature": raw,
                })), (expected, "Temperature"))

    def test_adaptive_sample_preserves_primary_telemetry_and_privacy(self):
        for case in FIXTURES:
            with self.subTest(capture=case["capture"]), tempfile.TemporaryDirectory() as folder:
                primary = copy.deepcopy(case["AppleSmartBattery"])
                primary["Serial"] = "PRIVATE_SENTINEL"
                outputs = [response(primary_text(primary))]
                if "AppleSmartBatteryPack" in case:
                    pack = copy.deepcopy(case["AppleSmartBatteryPack"])
                    # Conflicting pack fields must not contaminate power/state/capacity.
                    pack.update({"Serial": "PRIVATE_SENTINEL", "ExternalConnected": False,
                                 "IsCharging": False, "Voltage": 1})
                    pack["BatteryData"].update({"BatteryPower": 199999, "RemainingCapacity": 1})
                    outputs.append(response(pack_text([pack])))
                out = Path(folder) / "snapshot.json"
                history = Path(folder) / "history.json"
                stdout, stderr = io.StringIO(), io.StringIO()
                with patch.object(battery, "OUT", out), patch.object(battery, "HISTORY", history), \
                     patch.object(battery.subprocess, "run", side_effect=outputs), \
                     patch("sys.argv", ["update-battery.py", "--adaptive-sample"]), \
                     contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                    battery.main()
                state = battery.battery_state(primary_text(primary))
                self.assertEqual(stdout.getvalue(), "5\n" if state == "discharging" else "60\n")
                self.assertEqual(stderr.getvalue(), "")
                snapshot = json.loads(out.read_text())
                self.assertEqual(snapshot["metrics"][-1]["formattedValue"],
                                 battery.format_temp(case["expected_temperature_c"]))
                expected_power = battery.battery_power_w(primary_text(primary))[0]
                self.assertEqual(snapshot["metrics"][0]["formattedValue"], battery.format_watts(expected_power))
                rows = json.loads(history.read_text())
                if rows:
                    self.assertEqual(rows[-1]["state"], state)
                    self.assertAlmostEqual(rows[-1]["powerW"], expected_power, places=4)
                self.assertNotIn("PRIVATE_SENTINEL", out.read_text() + history.read_text())
                self.assertEqual({p.name for p in Path(folder).iterdir()}, {"snapshot.json", "history.json", ".history.json.lock"})

    def test_fallback_failure_keeps_power_sample_and_poll_interval(self):
        failures = [response("", 1, "PRIVATE_SENTINEL"), response(""),
                    response("<broken>"), subprocess.TimeoutExpired("ioreg", 2)]
        for external in (False, True):
            for failure in failures:
                with self.subTest(external=external, failure=failure), tempfile.TemporaryDirectory() as folder:
                    primary = primary_text({"BatteryInstalled": True, "ExternalConnected": external,
                                            "IsCharging": False, "BatteryData": {"BatteryPower": 23908}})
                    out = Path(folder) / "snapshot.json"
                    stdout, stderr = io.StringIO(), io.StringIO()
                    with patch.object(battery, "OUT", out), \
                         patch.object(battery, "HISTORY", Path(folder) / "history.json"), \
                         patch.object(battery.subprocess, "run", side_effect=[response(primary), failure]), \
                         patch("sys.argv", ["update-battery.py", "--adaptive-sample"]), \
                         contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                        battery.main()
                    self.assertEqual(stdout.getvalue(), "60\n" if external else "5\n")
                    self.assertEqual(stderr.getvalue(), "")
                    snapshot = json.loads(out.read_text())
                    self.assertEqual(snapshot["metrics"][0]["formattedValue"], "23.9 W")
                    self.assertEqual(snapshot["metrics"][-1]["formattedValue"], "—")

    def test_diagnose_uses_same_path_without_dumping_private_fields(self):
        case = FIXTURES[4]
        primary = copy.deepcopy(case["AppleSmartBattery"])
        primary["Serial"] = "PRIVATE_SENTINEL"
        pack = copy.deepcopy(case["AppleSmartBatteryPack"])
        pack["Serial"] = "PRIVATE_SENTINEL"
        stdout = io.StringIO()
        with patch.object(battery.subprocess, "run", side_effect=[
            response(primary_text(primary)), response(pack_text([pack]))
        ]), patch("sys.argv", ["update-battery.py", "--diagnose"]), \
             patch.object(battery, "atomic_write_json") as write, contextlib.redirect_stdout(stdout):
            battery.main()
        result = json.loads(stdout.getvalue())
        self.assertEqual(result["TemperatureC"], 33.59)
        self.assertEqual(result["TemperatureSource"], PACK_SOURCE)
        self.assertEqual(result["PowerW"], 23.908)
        self.assertAlmostEqual(result["RemainingEnergyWh"], 17.301162)
        self.assertIsNone(result["TemperatureRaw"])
        self.assertNotIn("PRIVATE_SENTINEL", stdout.getvalue())
        write.assert_not_called()

    def test_unknown_state_uses_safe_interval(self):
        self.assertEqual(battery.poll_interval_seconds(battery.battery_state("")), 60)


if __name__ == "__main__":
    unittest.main()
