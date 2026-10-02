#!/usr/bin/env python3

"""Live battery power + temperature metrics for RunCat Neo on macOS.



The producer reports battery-side power, not per-app or wall power.



Observed AppleSmartBattery conventions vary across hardware/macOS:

- BatteryPower can be a signed 64-bit milliwatt value printed by ioreg as

  an unsigned decimal. The display uses its magnitude and charging state is

  determined separately.

- InstantAmperage/Amperage can likewise be signed values printed unsigned.

- Temperature is centi-degrees Celsius on the measured macOS 15/26/27 devices.

- macOS 27 exposes it in AppleSmartBatteryPack.BatteryData instead.

- VirtualTemperature is a distinct, undocumented reading, not a substitute.



The script prefers BatteryPower and falls back to Voltage × Current.

"""



from __future__ import annotations



import argparse
import fcntl
import stat
import sys
from contextlib import contextmanager

import json

import math

import os

import plistlib

import re

import subprocess

import tempfile

import time

from datetime import datetime, timezone

from pathlib import Path

from typing import Any

from xml.parsers.expat import ExpatError



RUNCAT_HOME = Path(os.environ.get("RUNCAT_HOME", str(Path.home() / ".runcat")))

OUT = Path(

    os.environ.get(

        "RUNCAT_OUT_FILE",

        str(RUNCAT_HOME / "battery-power.json"),

    )

)

HISTORY = Path(

    os.environ.get(

        "RUNCAT_BATTERY_HISTORY_FILE",

        str(RUNCAT_HOME / "battery-power-history.json"),

    )

)



IOREG = "/usr/sbin/ioreg"

HISTORY_WINDOW_SECONDS = 300

HISTORY_RETENTION_SECONDS = 600

BATTERY_POLL_INTERVAL_SECONDS = 5

AC_POLL_INTERVAL_SECONDS = 60

# Runtime confidence: do not present a fresh estimate as fully settled.
RUNTIME_WARMUP_SECONDS = 60
RUNTIME_STABLE_SECONDS = 240
RUNTIME_MIN_SAMPLES = 6
MAX_HISTORY_BYTES = 2_000_000
ACTIVE_STATES = ("charging", "discharging")



NUMBER_RE_TEMPLATE = r'"{key}"\s*=\s*(-?\d+)'

BOOL_RE_TEMPLATE = r'"{key}"\s*=\s*(Yes|No|true|false|0|1)'





def run_ioreg(service: str = "AppleSmartBattery", *, archive: bool = False) -> str:

    # Depth 1 excludes child services; unlimited width avoids truncated values.
    command = [IOREG, "-rn", service, "-l", "-d", "1", "-w", "0"]
    if archive:
        command.append("-a")

    proc = subprocess.run(

        command,

        capture_output=True,

        text=True,

        encoding="utf-8",

        timeout=2,

        check=False,

    )

    if proc.returncode != 0 or not proc.stdout.strip():

        raise RuntimeError(f"Unable to read {service} telemetry")

    return proc.stdout





def property_literal(text: str, key: str) -> str | None:
    """Read one whole top-level ioreg property, not a nested lookalike."""
    values = re.findall(rf'^[ \t|]*"{re.escape(key)}"[ \t]*=[ \t]*([^\n]*)$',
                        text, re.MULTILINE)
    return values[0].strip() if len(values) == 1 else None


def dictionary_literal(text: str, key: str) -> str | None:
    """Find a direct key in an inline ioreg dictionary; skip nested values.

    ioreg dictionaries are not JSON. Split only outside quoted strings and
    balanced collections instead of guessing units or matching every subtree.
    """
    if not text.startswith("{") or not text.endswith("}"):
        return None
    parts, start, depth, quoted, escaped = [], 1, 0, False, False
    for i in range(1, len(text) - 1):
        char = text[i]
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
        elif char in "{([":
            depth += 1
        elif char in "})]":
            depth -= 1
            if depth < 0:
                return None
        elif char == "," and depth == 0:
            parts.append(text[start:i])
            start = i + 1
    if depth or quoted:
        return None
    parts.append(text[start:-1])
    values = []
    for part in parts:
        match = re.fullmatch(rf'\s*"{re.escape(key)}"\s*=\s*(.*?)\s*', part)
        if match:
            values.append(match.group(1))
    return values[0] if len(values) == 1 else None


def integer_literal(value: str | None) -> int | None:
    # All numeric telemetry consumed here fits in a signed/unsigned 64-bit
    # ioreg integer. Bound before int() to tolerate corrupt oversized fields.
    if value is None or not re.fullmatch(r'-?\d{1,20}', value):
        return None
    number = int(value)
    return number if -(1 << 63) <= number < (1 << 64) else None


def raw_number(text: str, key: str) -> int | None:
    value = property_literal(text, key)
    if value is not None:
        return integer_literal(value)
    # Preserve BatteryData fallbacks without accidentally reading adapter or
    # lifetime statistics. Temperature deliberately has its own source policy.
    nested = property_literal(text, "BatteryData")
    return integer_literal(dictionary_literal(nested, key)) if nested else None


def raw_bool(text: str, key: str) -> bool | None:
    value = property_literal(text, key)
    if value is None:
        return None
    if value.lower() in ("yes", "true", "1"):
        return True
    if value.lower() in ("no", "false", "0"):
        return False
    return None


def signed_64(value: int | None) -> int | None:

    if value is None:

        return None

    if -(1 << 63) <= value <= (1 << 63) - 1:

        return value

    if 0 <= value <= (1 << 64) - 1:

        return value - (1 << 64)

    return None





def sane_power_magnitude_w(value: float | None) -> float | None:

    if value is None or not math.isfinite(value):

        return None

    value = abs(value)

    if 0.0 <= value < 200.0:

        return value

    return None





def battery_power_w(text: str) -> tuple[float | None, str]:

    """Return battery-power magnitude in watts and the source used."""



    telemetry_mw = signed_64(raw_number(text, "BatteryPower"))

    if telemetry_mw is not None:

        power = sane_power_magnitude_w(telemetry_mw / 1000.0)

        if power is not None:

            return power, "BatteryPower"



    voltage_mv = raw_number(text, "AppleRawBatteryVoltage")

    if not voltage_mv or voltage_mv <= 0:

        voltage_mv = raw_number(text, "Voltage")



    current_ma = signed_64(raw_number(text, "InstantAmperage"))

    if current_ma is None:

        current_ma = signed_64(raw_number(text, "Amperage"))



    if voltage_mv and voltage_mv > 0 and current_ma is not None:

        power = sane_power_magnitude_w((voltage_mv * current_ma) / 1_000_000.0)

        if power is not None:

            return power, "Voltage×Current"



    return None, "Unavailable"





def plausible_celsius(value: float | None) -> float | None:

    if value is None or not math.isfinite(value):

        return None

    if -20.0 <= value <= 100.0:

        return value

    return None





def top_level_number(text: str, key: str) -> int | None:
    return integer_literal(property_literal(text, key))


def centi_celsius(raw: Any) -> float | None:
    # Preserve the existing zero/65535 sentinel policy. Check bounds before
    # conversion so even a corrupt, oversized integer cannot overflow.
    if type(raw) not in (int, float) or raw == 0 or not -2000 <= raw <= 10000:
        return None
    return plausible_celsius(raw / 100.0)


def battery_temperature_c(text: str) -> tuple[float | None, str]:
    """Parse AppleSmartBattery.Temperature in centi-Celsius, without I/O."""
    if raw_bool(text, "BatteryInstalled") is False:
        return None, "Unavailable"
    value = centi_celsius(top_level_number(text, "Temperature"))
    return (value, "Temperature") if value is not None else (None, "Unavailable")


def read_battery_temperature_c(text: str) -> tuple[float | None, str]:
    """Query the pack only when the primary temperature is unavailable."""
    temperature = battery_temperature_c(text)
    if temperature[0] is not None or raw_bool(text, "BatteryInstalled") is False:
        return temperature

    try:
        pack_text = run_ioreg("AppleSmartBatteryPack", archive=True)
        packs = plistlib.loads(pack_text.encode("utf-8"))
    except (OSError, subprocess.SubprocessError, RuntimeError, ValueError, ExpatError):
        # An optional temperature lookup must not lose the power sample or
        # change its adaptive interval. Never print or persist the raw dump.
        return None, "Unavailable"

    # Do not guess which pack to use if the service layout is ambiguous.
    if not isinstance(packs, list) or len(packs) != 1 or not isinstance(packs[0], dict):
        return None, "Unavailable"
    battery_data = packs[0].get("BatteryData")
    if not isinstance(battery_data, dict):
        return None, "Unavailable"
    value = centi_celsius(battery_data.get("Temperature"))
    if value is not None:
        return value, "AppleSmartBatteryPack.BatteryData.Temperature"
    return None, "Unavailable"





def battery_state(text: str) -> str:

    external = raw_bool(text, "ExternalConnected")

    charging = raw_bool(text, "IsCharging")



    if charging is True:

        return "charging"

    if external is False:

        return "discharging"

    if external is True:

        return "ac"

    return "unknown"





def battery_remaining_energy_wh(text: str) -> tuple[float | None, str]:

    """Estimate remaining battery energy in Wh from charge and pack voltage.

    AppleRawCurrentCapacity is preferred when available. On newer macOS
    versions the equivalent value may only appear as RemainingCapacity inside
    BatteryData, so the parser also accepts that form. Final fallbacks derive
    charge from a full-charge capacity and state of charge.
    """

    voltage_mv = raw_number(text, "AppleRawBatteryVoltage")
    if not voltage_mv or voltage_mv <= 0:
        voltage_mv = raw_number(text, "Voltage")

    if not voltage_mv or voltage_mv <= 0:
        return None, "Unavailable"

    capacity_mah: float | None = None
    capacity_source = "Unavailable"

    raw_current = raw_number(text, "AppleRawCurrentCapacity")
    if raw_current is not None and raw_current > 0:
        capacity_mah = float(raw_current)
        capacity_source = "AppleRawCurrentCapacity"

    if capacity_mah is None:
        remaining = raw_number(text, "RemainingCapacity")
        if remaining is not None and remaining > 0:
            capacity_mah = float(remaining)
            capacity_source = "BatteryData.RemainingCapacity"

    current = raw_number(text, "CurrentCapacity")
    maximum = raw_number(text, "MaxCapacity")

    # Older machines may expose CurrentCapacity/MaxCapacity directly in mAh.
    if (
        capacity_mah is None
        and current is not None
        and current > 0
        and maximum is not None
        and maximum > 1000
    ):
        capacity_mah = float(current)
        capacity_source = "CurrentCapacity"

    if capacity_mah is None:
        full_mah: float | None = None
        full_source = ""
        for key in (
            "NominalChargeCapacity",
            "FullChargeCapacity",
            "AppleRawMaxCapacity",
            "DesignCapacity",
        ):
            candidate = raw_number(text, key)
            if candidate is not None and candidate > 1000:
                full_mah = float(candidate)
                full_source = key
                break

        soc: float | None = None
        soc_raw = raw_number(text, "StateOfCharge")
        if soc_raw is not None and 0 <= soc_raw <= 100:
            soc = float(soc_raw)
        elif (
            current is not None
            and maximum is not None
            and maximum > 0
            and maximum <= 100
            and 0 <= current <= maximum
        ):
            soc = 100.0 * current / maximum

        if full_mah is not None and soc is not None:
            capacity_mah = full_mah * soc / 100.0
            capacity_source = f"{full_source}×StateOfCharge"

    if capacity_mah is None:
        return None, "Unavailable"

    energy_wh = (capacity_mah * float(voltage_mv)) / 1_000_000.0
    if not math.isfinite(energy_wh) or not (0.0 < energy_wh < 200.0):
        return None, "Unavailable"

    return energy_wh, capacity_source


def estimated_runtime_hours(
    state: str,
    remaining_energy_wh: float | None,
    average_w: float | None,
) -> float | None:

    """Estimate time to empty from remaining Wh and the rolling power average."""

    if state != "discharging":
        return None
    if not finite_number(remaining_energy_wh) or not finite_number(average_w):
        return None
    if not math.isfinite(average_w) or average_w < 0.5:
        return None

    hours = remaining_energy_wh / average_w
    if not math.isfinite(hours) or hours <= 0.0:
        return None
    return hours


def atomic_write_json(path: Path, value: Any) -> None:

    path.parent.mkdir(parents=True, exist_ok=True)

    fd, temp_path = tempfile.mkstemp(prefix=f".{path.name}-", dir=str(path.parent))

    try:

        with os.fdopen(fd, "w", encoding="utf-8") as f:

            json.dump(value, f, ensure_ascii=False, allow_nan=False)

        os.replace(temp_path, path)

    except Exception:

        try:

            os.unlink(temp_path)

        except OSError:

            pass

        raise





def finite_number(value: Any) -> bool:
    if type(value) not in (int, float):
        return False
    try:
        return math.isfinite(value)
    except (OverflowError, ValueError):
        return False


def load_history() -> list[dict[str, Any]]:
    try:
        with HISTORY.open("rb") as stream:
            raw = stream.read(MAX_HISTORY_BYTES + 1)
        if len(raw) > MAX_HISTORY_BYTES:
            return []
        data = json.loads(raw.decode("utf-8"))
    except (OSError, ValueError, RecursionError):
        return []
    return data if isinstance(data, list) else []


def max_sample_gap(state: str) -> int:
    # Tolerate ordinary scheduling jitter; a gap longer than three scheduled
    # intervals is not evidence of continuous observation (e.g. after sleep).
    return 3 * poll_interval_seconds(state)


def continuous_history(history: list, now: float, state: str) -> list[dict[str, Any]]:
    if state not in ACTIVE_STATES or not finite_number(now):
        return []
    retained: list[dict[str, Any]] = []
    cutoff = now - HISTORY_RETENTION_SECONDS
    for item in history:
        if not isinstance(item, dict):
            retained = []
            continue
        timestamp, power = item.get("timestamp"), item.get("powerW")
        if (not finite_number(timestamp) or not finite_number(power)
                or not 0 <= power < 200 or item.get("state") != state
                or timestamp > now):
            retained = []
            continue
        if timestamp < cutoff:
            continue
        if retained:
            delta = timestamp - retained[-1]["timestamp"]
            if delta < 0 or delta > max_sample_gap(state):
                retained = []
            elif delta == 0:
                retained.pop()  # One timestamp is one sample, not confidence.
        retained.append({"timestamp": float(timestamp), "powerW": float(power), "state": state})
    if retained and now - retained[-1]["timestamp"] > max_sample_gap(state):
        return []
    return retained


def update_history(now: float, power_w: float | None, state: str) -> list[dict[str, Any]]:
    retained = continuous_history(load_history(), now, state)
    if (finite_number(now) and finite_number(power_w) and 0 <= power_w < 200
            and state in ACTIVE_STATES):
        if retained and retained[-1]["timestamp"] == now:
            retained.pop()
        retained.append({"timestamp": float(now), "powerW": round(float(power_w), 4), "state": state})
    else:
        # An unavailable reading must not turn an old observation into a fresh
        # average/runtime. Keep the normal snapshot with unavailable fields.
        retained = []
    atomic_write_json(HISTORY, retained)
    return retained


def five_minute_stats(history: list[dict[str, Any]], now: float, state: str
                      ) -> tuple[float | None, float | None, float, int]:
    """Integrate the continuous session, clipped to the last 300 seconds.

    Use the sample immediately before the boundary for interpolation, but do
    not count the interpolated boundary as another real observation.
    """
    valid = continuous_history(history, now, state)
    if not valid:
        return None, None, 0.0, 0
    points = [(item["timestamp"], item["powerW"]) for item in valid]
    cutoff = now - HISTORY_WINDOW_SECONDS
    count = sum(t >= cutoff for t, _ in points)
    i = 0
    while i + 1 < len(points) and points[i + 1][0] <= cutoff:
        i += 1
    points = points[i:]
    if points[0][0] < cutoff:
        t0, p0 = points[0]
        if len(points) > 1:
            t1, p1 = points[1]
            p0 += (p1 - p0) * (cutoff - t0) / (t1 - t0)
        points[0] = (cutoff, p0)
    coverage = max(0.0, now - points[0][0])
    peak = max(power for _, power in points)
    if coverage <= 0:
        return points[-1][1], peak, 0.0, count
    area = sum(0.5 * (p0 + p1) * (t1 - t0)
               for (t0, p0), (t1, p1) in zip(points, points[1:]))
    area += points[-1][1] * (now - points[-1][0])
    return area / coverage, peak, coverage, count


def format_watts(value: float | None) -> str:

    return "—" if not finite_number(value) else f"{value:.1f} W"





def format_temp(value: float | None) -> str:

    return "—" if not finite_number(value) else f"{value:.1f} °C"





def format_runtime(
    value: float | None,
    state: str,
    coverage_seconds: float,
    sample_count: int,
) -> str:

    if state == "charging":
        return "Charging"
    if state == "ac":
        return "On AC"
    if state != "discharging":
        return "—"

    # During the first minute, there is too little history to show even an
    # approximate runtime.  After that, prefix the estimate with '~' until
    # roughly 80% of the five-minute window has accumulated.
    if (
        coverage_seconds < RUNTIME_WARMUP_SECONDS
        or sample_count < RUNTIME_MIN_SAMPLES
    ):
        return "Calculating…"

    if not finite_number(value) or value <= 0:
        return "—"

    total_minutes = max(1, int(round(value * 60.0)))
    hours, minutes = divmod(total_minutes, 60)
    if hours:
        formatted = f"{hours}h {minutes:02d}m"
    else:
        formatted = f"{minutes}m"

    if coverage_seconds < RUNTIME_STABLE_SECONDS:
        return f"~{formatted}"
    return formatted



def build_snapshot(

    power_w: float | None,

    temperature_c: float | None,

    state: str,

    average_w: float | None,

    peak_w: float | None,

    runtime_hours: float | None,

    runtime_coverage_seconds: float,

    runtime_sample_count: int,

) -> dict[str, Any]:

    if state == "charging":

        first_title = "Charging"

        menu_prefix = "+"

    elif state == "ac":

        first_title = "Battery Power"

        menu_prefix = ""

    else:

        first_title = "Power"

        menu_prefix = ""



    metrics_bar = (

        f"{menu_prefix}{power_w:.1f} W"

        if power_w is not None

        else "—"

    )



    return {

        "title": "Battery Power",

        "symbol": "bolt.circle",

        "metricsBarValue": metrics_bar,

        "metrics": [

            {

                "title": first_title,

                "formattedValue": format_watts(power_w),

            },

            {

                "title": "5m Avg",

                "formattedValue": format_watts(average_w),

            },

            {

                "title": "5m Peak",

                "formattedValue": format_watts(peak_w),

            },

            {

                "title": "Estimated Runtime",

                "formattedValue": format_runtime(
                    runtime_hours,
                    state,
                    runtime_coverage_seconds,
                    runtime_sample_count,
                ),

            },

            {

                "title": "Temperature",

                "formattedValue": format_temp(temperature_c),

            },

        ],

        "lastUpdatedDate": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),

    }





def diagnostic(text: str) -> dict[str, Any]:

    power, power_source = battery_power_w(text)

    temperature, temperature_source = read_battery_temperature_c(text)

    remaining_energy, remaining_energy_source = battery_remaining_energy_wh(text)



    return {

        "BatteryPowerRawUnsigned": raw_number(text, "BatteryPower"),

        "BatteryPowerRawSigned": signed_64(raw_number(text, "BatteryPower")),

        "SystemLoadRaw": raw_number(text, "SystemLoad"),

        "InstantAmperageRawUnsigned": raw_number(text, "InstantAmperage"),

        "InstantAmperageRawSigned": signed_64(raw_number(text, "InstantAmperage")),

        "AmperageRawSigned": signed_64(raw_number(text, "Amperage")),

        "VoltageRaw": raw_number(text, "Voltage"),

        "AppleRawBatteryVoltageRaw": raw_number(text, "AppleRawBatteryVoltage"),

        "AppleRawCurrentCapacityRaw": raw_number(text, "AppleRawCurrentCapacity"),

        "RemainingCapacityRaw": raw_number(text, "RemainingCapacity"),

        "CurrentCapacityRaw": raw_number(text, "CurrentCapacity"),

        "MaxCapacityRaw": raw_number(text, "MaxCapacity"),

        "NominalChargeCapacityRaw": raw_number(text, "NominalChargeCapacity"),

        "FullChargeCapacityRaw": raw_number(text, "FullChargeCapacity"),

        "StateOfChargeRaw": raw_number(text, "StateOfCharge"),

        "TemperatureRaw": top_level_number(text, "Temperature"),

        "VirtualTemperatureRaw": top_level_number(text, "VirtualTemperature"),

        "ExternalConnected": raw_bool(text, "ExternalConnected"),

        "IsCharging": raw_bool(text, "IsCharging"),

        "PowerW": power,

        "PowerSource": power_source,

        "TemperatureC": temperature,

        "TemperatureSource": temperature_source,

        "RemainingEnergyWh": remaining_energy,

        "RemainingEnergySource": remaining_energy_source,

        "State": battery_state(text),

    }





def poll_interval_seconds(state: str) -> int:

    """Adaptive cadence: fast on battery, slow whenever external power is present."""

    if state == "discharging":

        return BATTERY_POLL_INTERVAL_SECONDS

    return AC_POLL_INTERVAL_SECONDS





def _sample_once_unlocked() -> str:

    text = run_ioreg()

    power, _power_source = battery_power_w(text)

    temperature, _temperature_source = read_battery_temperature_c(text)

    state = battery_state(text)



    now = time.time()

    history = update_history(now, power, state)

    average, peak, coverage_seconds, sample_count = five_minute_stats(
        history, now, state
    )

    remaining_energy, _remaining_energy_source = battery_remaining_energy_wh(text)

    runtime = estimated_runtime_hours(state, remaining_energy, average)



    snapshot = build_snapshot(

        power_w=power,

        temperature_c=temperature,

        state=state,

        average_w=average,

        peak_w=peak,

        runtime_hours=runtime,

        runtime_coverage_seconds=coverage_seconds,

        runtime_sample_count=sample_count,

    )

    atomic_write_json(OUT, snapshot)

    return state






@contextmanager
def sample_lock(path: Path | None = None, timeout: float = 6.0):
    history_path = HISTORY if path is None else path
    lock_path = history_path.with_name("." + history_path.name + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(lock_path, flags, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise RuntimeError("Sampling lock is not a regular file")
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError("Another battery sample is still running")
                time.sleep(0.05)
        yield
    finally:
        os.close(fd)  # Releases the advisory lock, including on exceptions.


def sample_once() -> str:
    if OUT.expanduser().resolve() == HISTORY.expanduser().resolve():
        raise ValueError("Snapshot and history paths must differ")
    with sample_lock():
        return _sample_once_unlocked()


def main() -> None:

    parser = argparse.ArgumentParser()

    parser.add_argument(

        "--diagnose",

        action="store_true",

        help="print only the battery fields used by this script",

    )

    parser.add_argument(

        "--adaptive-sample",

        action="store_true",

        help="sample once and print the number of seconds until the next sample",

    )

    args = parser.parse_args()



    if args.diagnose:

        text = run_ioreg()

        print(json.dumps(diagnostic(text), indent=2, ensure_ascii=False))

        return



    state = sample_once()

    if args.adaptive_sample:

        print(poll_interval_seconds(state))





if __name__ == "__main__":

    try:
        main()
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
        # No raw ioreg stderr, plist, device serials or traceback in launchd logs.
        print("Battery sample failed. Check --diagnose; raw telemetry is not logged.", file=sys.stderr)
        raise SystemExit(1)
