# RunCat Battery Power

A local battery telemetry producer for [RunCat Neo](https://github.com/runcat-dev/RunCatNeo) custom metrics on macOS.

It reads `AppleSmartBattery` telemetry with `ioreg` (plus `AppleSmartBatteryPack` only when needed for temperature), writes a RunCat-compatible JSON snapshot, and keeps the producer lightweight with adaptive polling.

## What it shows

- **Power** — instantaneous battery-side power in watts
- **5m Avg** — time-weighted rolling average over the current discharge/charge session
- **5m Peak** — peak power in the recent five-minute window
- **Estimated Runtime** — estimated time remaining while discharging
- **Temperature** — battery temperature

While connected to external power, runtime is shown as `On AC`; while charging, it is shown as `Charging`.

## Temperature sources

The producer reads top-level `AppleSmartBattery.Temperature / 100.0`. When that
value is missing or invalid, it queries `AppleSmartBatteryPack` once and reads
exactly `BatteryData.Temperature / 100.0` from its plist output. This covers the
macOS 26 and 27 layouts without an OS-version probe or unit guessing.

`VirtualTemperature` is a distinct reading whose exact sensor/model semantics
are not established by the public evidence; it is retained in diagnostics but
does not override or substitute for `Temperature`. For example, the public
measurements include `Temperature=3049`, `VirtualTemperature=3169`, and another
capture has `3115` versus `3829`. The displayed temperatures are **30.49 °C** and
**31.15 °C**, respectively, following SystemInfoKit's conversion.

An explicitly absent battery skips the pack lookup. A missing service, timeout,
malformed/ambiguous plist, or invalid temperature displays `—` without dropping
the power sample or changing the 5/60-second polling decision. Each `ioreg` call
has a two-second timeout; no retry loop or persistent failure cache is added.
The existing zero/65535 sentinel exclusion and −20 to 100 °C validity range remain.

The pinned upstream implementation, measured-value provenance, and offline test
cases are documented in [tests/fixtures/README.md](tests/fixtures/README.md).

## Runtime confidence

The runtime estimate is deliberately conservative about fresh data:

- first 60 seconds: `Calculating…`
- 60 seconds to about 4 minutes: approximate value such as `~6h 32m`
- after about 4 minutes: value such as `6h 21m`

Switching between battery and external power resets the rolling history so an old discharge session does not contaminate a new one.

## How the estimate works

The producer estimates remaining energy as

```text
remaining energy (Wh) ≈ remaining charge (Ah) × current pack voltage (V)
```

and then computes

```text
estimated runtime (h) = remaining energy (Wh) / 5-minute average power (W)
```

The five-minute average is time-weighted using trapezoidal integration rather than a plain sample mean, so delayed polls do not receive the same weight as regularly spaced samples.

This is a **workload-dependent estimate**, not a promise of actual battery life. Display brightness, CPU/GPU activity, radios, peripherals, background work, temperature, and future workload changes can move the estimate substantially. Multiplying remaining charge by the current pack voltage is also an approximation because pack voltage changes during discharge.

## Polling behavior

The LaunchAgent runs a small shell loop and starts Python only when a sample is due:

- discharging: every **5 seconds**
- AC connected or charging: every **60 seconds**

RunCat Neo itself watches the JSON file for filesystem changes; this producer is responsible for the polling cadence.

## Requirements

- macOS
- RunCat Neo with Custom Metrics support
- Python **3.10+**
- a Mac laptop exposing `AppleSmartBattery` telemetry through `/usr/sbin/ioreg`

Tested on Apple Silicon. Battery telemetry keys can vary across Mac models and macOS releases, so the script includes several fallbacks but cannot guarantee support for every machine.

## Install

Clone the repository and run:

```bash
chmod +x install.sh uninstall.sh
./install.sh
```

The installer:

1. copies the producer to `~/.runcat/runcat-battery-power/`
2. writes an initial `~/.runcat/battery-power.json`
3. installs `~/Library/LaunchAgents/dev.runcat.battery-power.plist`
4. starts the adaptive background sampler

Then open **RunCat Neo → Settings → Metrics → Custom Metrics → Add Custom Metrics Source** and select:

```text
~/.runcat/battery-power.json
```

If the hidden `.runcat` directory is not visible in the file picker, use **Command + Shift + G** and enter the path directly.

RunCat Neo's upstream custom-metrics schema is documented here:

- https://github.com/runcat-dev/RunCatNeo/blob/main/docs/CustomMetricsSchema.md

## Diagnostics

Inspect the battery fields used by the producer:

```bash
python3 ~/.runcat/runcat-battery-power/update-battery.py --diagnose
```

Force one sample and inspect the JSON:

```bash
python3 ~/.runcat/runcat-battery-power/update-battery.py
python3 -m json.tool ~/.runcat/battery-power.json
```

Background logs, normally empty, are stored at:

```text
~/.runcat/runcat-battery-power/stdout.log
~/.runcat/runcat-battery-power/stderr.log
```

## Generated files

```text
~/.runcat/battery-power.json
~/.runcat/battery-power-history.json
```

The JSON snapshot is written atomically so RunCat does not observe a partially written file.

## Uninstall

```bash
./uninstall.sh
```

To remove the LaunchAgent and installed scripts but keep the generated JSON/history files:

```bash
./uninstall.sh --keep-data
```

After uninstalling, remove the Custom Metrics source from RunCat Neo if it is still registered there.

## Privacy

The producer is local-only. It reads battery telemetry from `ioreg` and writes local JSON files. It makes no network requests.

Raw battery/pack dumps are never written to the snapshot or history. Diagnostics
print selected numeric/state fields and the temperature source, not device serials.

## Tests

Run the fixture and subprocess-mock regression tests without macOS hardware or network access:

```bash
python3 -B -m unittest discover -s tests -v
```

## License

MIT
