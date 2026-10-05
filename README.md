# RunCat Battery Power

A local battery telemetry producer for [RunCat Neo](https://github.com/runcat-dev/RunCatNeo) custom metrics on macOS.

It reads `AppleSmartBattery` telemetry with `ioreg` (plus `AppleSmartBatteryPack` only when needed for temperature), writes a RunCat-compatible JSON snapshot, and keeps the producer lightweight with adaptive polling.

## What it shows

- **Power** — instantaneous battery-side power in watts
- **5m Avg** — time-weighted rolling average over the current discharge/charge session
- **5m Peak** — peak power in the recent five-minute window
- **Estimated Runtime** — estimated time remaining while discharging
- **Temperature** — battery temperature
- **Maximum Capacity** — macOS-reported maximum capacity (slow cached observation)
- **Cycle Count** — the battery cycle counter from the existing ioreg sample

While connected to external power, runtime is shown as `On AC`; while charging, it is shown as `Charging`.

## Maximum capacity and cycles

The two added text rows use **system-reported maximum capacity**, not
`MaxCapacity=100` or a nominal/design-capacity ratio, and the existing
`AppleSmartBattery.CycleCount`. The menu bar remains watts only; no additional
progress bar is added. Example values (illustrative, not a live reading):

```text
Maximum Capacity: 92%
Cycle Count: 156
```

A separate short-lived `dev.runcat.battery-health` LaunchAgent checks a
`system_profiler SPPowerDataType -json` cache at login and every six hours.
The fast sampler never launches or waits for that query. A failed/overdue
observation is labeled `(cached)`; an absent or expired observation is `—`,
not 100%. Read [the capacity/cycle contract](docs/BATTERY_HEALTH.md) for exact
source keys, freshness, privacy, failure and verification limits.

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

Switching power state resets the rolling history so an old session does not
contaminate a new one. A missing/invalid power observation or a gap longer than
three scheduled intervals also restarts confidence (15 seconds while discharging,
180 seconds while charging). This is a sampling-continuity policy, not a claim
that the script can detect every sleep/wake event. `Calculating…` after a long
pause is intentional, rather than reporting an old estimate as settled.

## How the estimate works

The producer estimates remaining energy as

```text
remaining energy (Wh) ≈ remaining charge (Ah) × current pack voltage (V)
```

and then computes

```text
estimated runtime (h) = remaining energy (Wh) / 5-minute average power (W)
```

The five-minute average uses trapezoidal integration of a continuous session,
clipped to the recent 300-second window. The observation just before the window
is used to interpolate its boundary, without counting that synthetic point as
another sample. Small polling delays are tolerated; long gaps are not integrated
as though the workload had been observed. A valid zero-current observation is
0 W, not missing telemetry. Numeric reads are scoped to top-level properties or
direct `BatteryData` fields, not adapter/lifetime dictionaries with similar keys.

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
sh install.sh
```

The installer:

1. tests a live power sample and a native maximum-capacity query before replacing existing files,
2. stops the old battery LaunchAgent and backs up its target files under
   `~/.runcat/backups/battery-power-.../`,
3. atomically installs the producer and `dev.runcat.battery-power.plist`,
4. validates a native maximum-capacity observation and adds the independent six-hour job/cache,
5. starts the same adaptive sampler and verifies a live sample and both job registrations,
6. restores previous target files and both launch states on detected installation failure.

Success ends with `LOCAL CHECK PASSED`. This checks actual telemetry, installed
source, configuration and a running job; it does not automate observation of the
RunCat UI or a full battery/AC polling cycle. No `sudo` is required.

Existing source registration does not need to be re-added. Custom absolute paths
(`RUNCAT_HOME`, `RUNCAT_OUT_FILE`, `RUNCAT_BATTERY_HISTORY_FILE`,
`RUNCAT_BATTERY_HEALTH_FILE`, and `RUNCAT_BATTERY_INSTALL_DIR`) are passed to both the initial sample and LaunchAgent.
A later reinstall without these variables reuses paths from the existing plist.
`PYTHON_BIN` can select a Python 3.10+ interpreter. A plain `sh install.sh`
also preserves that recorded interpreter; the shell's bootstrap Python does not
replace it. Empty explicit overrides are rejected. See
[runtime preservation](docs/RUNTIME_SETTINGS.md) for the precise contract. A previously unrecorded custom
history path cannot be recovered automatically; supply it once when upgrading.

From this repository, repeat the live verification with:

```bash
python3 -B scripts/manage_install.py verify
```

For an already registered source, keep it. Then open **RunCat Neo → Settings → Metrics → Custom Metrics → Add Custom Metrics Source** and select:

```text
~/.runcat/battery-power.json
```

If the hidden `.runcat` directory is not visible in the file picker, use **Command + Shift + G** and enter the path directly.

RunCat Neo's upstream custom-metrics schema is documented here:

- https://github.com/runcat-dev/RunCatNeo/blob/main/docs/CustomMetricsSchema.md

## Diagnostics and manual refresh

From this repository, use the installed LaunchAgent's **saved Python and paths**:

```sh
python3 -B scripts/run_installed.py diagnose
python3 -B scripts/run_installed.py refresh
python3 -B scripts/run_installed.py show
python3 -B scripts/run_installed.py refresh-health
```

These commands also work with custom output/history/install directories without
repeating environment overrides. `diagnose` prints the installed producer's
selected fields without writing telemetry. `refresh` takes one sample and verifies
a fresh snapshot. `show` only reads the last snapshot (possibly stale); it does
not sample. The helpers do not add a background process or change configuration.
Running `update-battery.py` directly with an arbitrary shell Python bypasses the
saved runtime, so it is not the recommended installed-system check.

Background logs, normally empty, are stored at:

```text
~/.runcat/runcat-battery-power/stdout.log
~/.runcat/runcat-battery-power/stderr.log
```

## Generated files

```text
~/.runcat/battery-power.json
~/.runcat/battery-power-history.json
~/.runcat/battery-health.json
```

The JSON snapshot is written atomically so RunCat does not observe a partially
written file. A shared empty `.battery-power-history.json.lock` file serializes
manual and background samples. The file is not telemetry and should not be
removed while sampling. Invalid/nonfinite history is discarded conservatively;
JSON writes reject NaN and Infinity.

## Uninstall

```bash
sh uninstall.sh
```

To remove both LaunchAgents and installed scripts but keep the generated snapshot/history/health cache:

```bash
sh uninstall.sh --keep-data
```

After uninstalling, remove the Custom Metrics source from RunCat Neo if it is
still registered there. Target files are backed up before removal. Backups,
logs, the empty lock file, and unrelated files are retained; the installer never
recursively removes a shared directory.

## Roll back this health upgrade

Use this version's complete printed backup directory; the rollback validates
all target paths and hashes and first backs up the current state:

```sh
python3 -B scripts/manage_install.py rollback --backup '/absolute/printed/backup'
```

See [restoration details](docs/BATTERY_HEALTH.md). This does not rewrite remote
GitHub commits, remove unrelated metrics, or promise recovery from power loss.

## Privacy

The producer is local-only. It reads battery telemetry from `ioreg`, reads macOS capacity via a bounded infrequent `system_profiler` call, and writes local JSON files. It makes no network requests.

Raw battery/pack dumps are never written to the snapshot or history. Diagnostics
print selected numeric/state fields and the temperature source, not device serials.

## Tests

Run the fixture and subprocess-mock regression tests without macOS hardware or network access:

```bash
python3 -B -m unittest discover -s tests -v
```

The regression suite includes synthetic telemetry, real interprocess lock and
shell-loop tests, and installation/rollback tests with mocked launchctl and
synthetic battery readings. Passing these is not native hardware certification.
See [stabilization notes](docs/STABILIZATION_20261003.md) for scope and limitations.

## CI and maintenance

CI runs the offline suite on Linux and macOS using a full-SHA-pinned checkout,
read-only repository permission, no persisted checkout credentials and a bounded
job timeout. It logs the hosted runner's Python version; it is not a complete
version matrix. Native battery accuracy, timer behavior and RunCat rendering
must still be checked on the installation machine. See the
[release audit](docs/RELEASE_AUDIT_20261003.md) for the current review and the
[runtime contract](docs/RUNTIME_SETTINGS.md) for setting precedence.

Keep requirements, manual commands and the runtime contract synchronized with
installer changes. Future action updates need an upstream SHA/version check;
no automatic update bot or additional background sampler is introduced.

## License

MIT. See [LICENSE](LICENSE).
