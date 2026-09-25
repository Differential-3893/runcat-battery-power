#!/usr/bin/env python3

"""Live battery power + temperature metrics for RunCat Neo on macOS.



The producer reports battery-side power, not per-app or wall power.



Observed AppleSmartBattery conventions vary across hardware/macOS:

- BatteryPower can be a signed 64-bit milliwatt value printed by ioreg as

  an unsigned decimal. The display uses its magnitude and charging state is

  determined separately.

- InstantAmperage/Amperage can likewise be signed values printed unsigned.

- VirtualTemperature is centi-degrees Celsius.

- A top-level Temperature reading is traditionally deci-Kelvin on macOS.



The script prefers BatteryPower and falls back to Voltage × Current.

"""



from __future__ import annotations



import argparse

import json

import math

import os

import re

import subprocess

import tempfile

import time

from datetime import datetime, timezone

from pathlib import Path

from typing import Any



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



NUMBER_RE_TEMPLATE = r'"{key}"\s*=\s*(-?\d+)'

BOOL_RE_TEMPLATE = r'"{key}"\s*=\s*(Yes|No|true|false|0|1)'





def run_ioreg() -> str:

    proc = subprocess.run(

        [IOREG, "-rn", "AppleSmartBattery", "-l"],

        capture_output=True,

        text=True,

        encoding="utf-8",

        timeout=2,

        check=False,

    )

    if proc.returncode != 0 or not proc.stdout.strip():

        detail = proc.stderr.strip() or f"exit status {proc.returncode}"

        raise RuntimeError(f"Unable to read AppleSmartBattery: {detail}")

    return proc.stdout





def raw_number(text: str, key: str) -> int | None:

    pattern = NUMBER_RE_TEMPLATE.format(key=re.escape(key))

    match = re.search(pattern, text)

    if not match:

        return None

    try:

        return int(match.group(1))

    except ValueError:

        return None





def raw_bool(text: str, key: str) -> bool | None:

    pattern = BOOL_RE_TEMPLATE.format(key=re.escape(key))

    match = re.search(pattern, text, re.IGNORECASE)

    if not match:

        return None

    return match.group(1).lower() in {"yes", "true", "1"}





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

    if current_ma in (None, 0):

        current_ma = signed_64(raw_number(text, "Amperage"))



    if voltage_mv and voltage_mv > 0 and current_ma not in (None, 0):

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





def battery_temperature_c(text: str) -> tuple[float | None, str]:

    """Return battery temperature in Celsius.



    Prefer VirtualTemperature because AppleSmartBattery publishes it in

    centi-degrees Celsius. If unavailable, interpret Temperature using the

    traditional macOS SmartBattery deci-Kelvin convention.

    """



    virtual_raw = raw_number(text, "VirtualTemperature")

    if virtual_raw is not None and virtual_raw not in (0, 65535):

        value = plausible_celsius(virtual_raw / 100.0)

        if value is not None:

            return value, "VirtualTemperature"



    temperature_raw = raw_number(text, "Temperature")

    if temperature_raw is not None and temperature_raw not in (0, 65535):

        deci_kelvin_c = (temperature_raw / 10.0) - 273.15

        value = plausible_celsius(deci_kelvin_c)

        if value is not None:

            return value, "Temperature"



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
    if remaining_energy_wh is None or average_w is None:
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

            json.dump(value, f, ensure_ascii=False)

        os.replace(temp_path, path)

    except Exception:

        try:

            os.unlink(temp_path)

        except OSError:

            pass

        raise





def load_history() -> list[dict[str, Any]]:

    try:

        data = json.loads(HISTORY.read_text(encoding="utf-8"))

    except (OSError, json.JSONDecodeError):

        return []

    if not isinstance(data, list):

        return []

    return [item for item in data if isinstance(item, dict)]





def update_history(

    now: float,

    power_w: float | None,

    state: str,

) -> list[dict[str, Any]]:

    history = load_history()



    retained = []

    cutoff = now - HISTORY_RETENTION_SECONDS

    for item in history:

        timestamp = item.get("timestamp")

        power = item.get("powerW")

        mode = item.get("state")

        if (

            isinstance(timestamp, (int, float))

            and timestamp >= cutoff

            and isinstance(power, (int, float))

            and mode in {"charging", "discharging"}

        ):

            retained.append(

                {

                    "timestamp": float(timestamp),

                    "powerW": float(power),

                    "state": mode,

                }

            )



    # A rolling average must belong to one continuous power-state session.
    # Otherwise a brief AC/charging interval could mix an older discharge run
    # into a newly started one.  Reset as soon as a state transition is seen.
    if retained and retained[-1].get("state") != state:

        retained = []



    if power_w is not None and state in {"charging", "discharging"}:

        retained.append(

            {

                "timestamp": now,

                "powerW": round(float(power_w), 4),

                "state": state,

            }

        )



    atomic_write_json(HISTORY, retained)

    return retained



def five_minute_stats(

    history: list[dict[str, Any]],

    now: float,

    state: str,

) -> tuple[float | None, float | None, float, int]:

    """Return time-weighted average, peak, covered seconds, and sample count.

    With perfectly regular 5-second polling, the time-weighted average is
    essentially the ordinary sample mean.  Time weighting is more correct when
    polling is delayed, the machine is busy, or the script is run manually.
    """

    if state not in {"charging", "discharging"}:

        return None, None, 0.0, 0



    cutoff = now - HISTORY_WINDOW_SECONDS

    relevant: list[tuple[float, float]] = []

    for item in history:

        timestamp = item.get("timestamp")

        power = item.get("powerW")

        if (

            item.get("state") == state

            and isinstance(timestamp, (int, float))

            and float(timestamp) >= cutoff

            and isinstance(power, (int, float))

        ):

            relevant.append((float(timestamp), float(power)))



    if not relevant:

        return None, None, 0.0, 0



    relevant.sort(key=lambda item: item[0])

    first_timestamp = relevant[0][0]

    coverage_seconds = max(0.0, min(HISTORY_WINDOW_SECONDS, now - first_timestamp))

    sample_count = len(relevant)

    peak = max(power for _timestamp, power in relevant)



    if sample_count == 1 or coverage_seconds <= 0.0:

        return relevant[-1][1], peak, coverage_seconds, sample_count



    # Trapezoidal integration over the observed samples, then hold the newest
    # sample constant from its timestamp to `now`.
    area_watt_seconds = 0.0

    for (t0, p0), (t1, p1) in zip(relevant, relevant[1:]):

        dt = max(0.0, t1 - t0)

        area_watt_seconds += 0.5 * (p0 + p1) * dt



    last_timestamp, last_power = relevant[-1]

    area_watt_seconds += last_power * max(0.0, now - last_timestamp)



    observed_seconds = max(0.0, now - first_timestamp)

    if observed_seconds <= 0.0:

        average = sum(power for _timestamp, power in relevant) / sample_count

    else:

        average = area_watt_seconds / observed_seconds



    return average, peak, coverage_seconds, sample_count



def format_watts(value: float | None) -> str:

    return "—" if value is None else f"{value:.1f} W"





def format_temp(value: float | None) -> str:

    return "—" if value is None else f"{value:.1f} °C"





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

    if value is None:
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

    temperature, temperature_source = battery_temperature_c(text)

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

        "TemperatureRaw": raw_number(text, "Temperature"),

        "VirtualTemperatureRaw": raw_number(text, "VirtualTemperature"),

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





def sample_once() -> str:

    text = run_ioreg()

    power, _power_source = battery_power_w(text)

    temperature, _temperature_source = battery_temperature_c(text)

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

    main()
