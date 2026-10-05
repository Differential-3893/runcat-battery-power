# macOS maximum capacity and cycle count

This addition is based on `069be97659a4d96e348c6ea93de7bcedcacb5c8d`.
It extends the existing five-row card to seven rows. The wattage menu item,
battery-side power calculation, temperature selection, runtime confidence and
5/60-second adaptive polling loop are retained.

## What each number means

**Maximum Capacity** is the percentage reported by macOS System Information,
not a newly calculated ratio. The slow worker runs only:

```sh
/usr/sbin/system_profiler SPPowerDataType -json -timeout 15
```

It finds the unique `sppower_battery_health_info` inside `SPPowerDataType`, reads
`sppower_battery_health_maximum_capacity`, and keeps a typed integer percent.
It accepts the direct report shape and narrowly grouped `_items` shapes, not
arbitrary nested dictionaries. Missing or ambiguous health information is a
failure, not 100%. English `grep` output and translated display labels are not
used as the interface. The JSON layout is an observed macOS tool interface,
not an Apple promise that every future version will preserve those keys.

The motivating Mac observation had `MaxCapacity=100`,
`NominalChargeCapacity=5430`, `FullChargeCapacity=5280`, `DesignCapacity=6075`,
and a system-reported maximum capacity of 92%. The two simple ratios are about
89.4% and 86.9%; neither should be relabeled as the system's 92%. Tests use a
synthetic report reproducing this numeric distinction. The software does **not**
hard-code 92, and does not identify which particular RunCat build caused the
user's separate built-in 100% display.

**Cycle Count** is read from the same `AppleSmartBattery` observation already
used by the fast sampler: top-level `CycleCount`, with the existing direct
`BatteryData` fallback. It is an integer observation, not a battery-life score.
Zero is valid; missing, boolean, negative or implausibly large values display
`—`. It adds no `ioreg` subprocess. A profiler cycle count is retained only as
an optional stale-cache guard, not as the fast card's cycle source.

Source cross-checks for the observed profiler JSON fields:

- [Mole's battery reader](https://github.com/tw93/Mole/blob/d929d156acd6e93b8a3a50b59f3e379057001b68/cmd/status/metrics_battery.go)
- [Raycast battery-health integration](https://github.com/raycast/extensions/blob/2a329b9368078f672dd4ef6324a16d676ed04437/extensions/battery-health/src/index.tsx)

These are field/layout references, not dependencies; their implementations are
not bundled or executed. The new reader uses only Python's standard library.
Actual compatibility with the user's current report is checked at installation.

## Two independent sampling rates

The existing `dev.runcat.battery-power` job still runs `adaptive-poll.sh` with
5 seconds between samples on battery and 60 seconds on AC/charging. The
interval is the loop's delay, not a hard real-time sampling guarantee.

A second label, **`dev.runcat.battery-health`**, invokes the same installed
Python/producer with `--refresh-health`. It has `RunAtLoad` and a 21,600-second
`StartInterval`, and **no KeepAlive**. It exits after the check; it is not a
second permanently running sampler. There is no HTTP service, network access,
root helper or private-API dependency.

Installation probes maximum capacity once before replacing the existing
installation and seeds the cache with that successful observation. Scheduled
checks within **five minutes** of the last attempt reuse the record (including
a failed-attempt cooldown), avoiding an immediate duplicate probe from RunAtLoad.
Otherwise login and six-hour scheduled checks query macOS again. The short
cooldown does not defer a six-hour timer by another entire six hours merely
because a manual refresh or login occurred earlier in the day. Explicit
`refresh-health` bypasses the cooldown.

Sleep, logout and launchd scheduling can change actual observation spacing;
it is not a promise of hard real-time execution at exactly six hours. Overdue
data is visibly marked, rather than being declared fresh.

The slow worker has an independent advisory lock and a bounded 20-second query
budget plus process cleanup, with a 512 KiB output cap. The fast sampler never
acquires that lock and never starts/waits for the slow query. It reads only a
bounded 16 KiB cache. A slow or failed profiler therefore cannot directly stall
the sampling critical section or manufacture a discharge-history gap. This
is call-path separation, not a measured claim of zero OS scheduling impact.

## Cache truthfulness and privacy

Default cache: `~/.runcat/battery-health.json` (mode 0600). It contains only:
version, source label, percent, optional cycle count, successful observation
time, last-attempt time and last-attempt success. Raw profiler reports, battery
serial numbers and arbitrary condition strings are never saved or logged.

- Up to six hours plus five minutes of scheduling allowance: `92%`.
- Last refresh failed, or observation overdue: `92% (cached)`.
- No valid observation, a future/invalid record, more than seven days old, or
  a currently lower cycle count suggesting a different battery: `—`.
- `BatteryInstalled=No`: both added rows are unavailable.

The seven-day expiry is a conservative display policy, not a physical battery
model. A failed refresh retains the previous successful value and **its original
observation time**; it cannot turn old health into a new observation. Repeated
failures are not retried every five seconds. A corrupt cache is ignored; a
subsequent scheduled or manual successful read replaces it. Cache/output/lock
path collisions are rejected. Symlink/special-file caches are not followed.

The cycle-decrease guard is not a complete battery identity check. A replacement
with the same or higher count cannot be identified from these two scalars; after
service, explicitly refresh health. No serial-based tracking is introduced.

`lastUpdatedDate` in the RunCat card is the fast snapshot's write time, not the
health observation time. `run_installed.py diagnose` separately reports
`MaximumCapacitySource`, `HealthObservedAt`, `HealthAgeSeconds`, `HealthCached`
and `CycleCount` without starting a slow query.

## Manual commands

Run from the repository, using the recorded runtime:

```sh
python3 -B scripts/run_installed.py refresh-health
python3 -B scripts/run_installed.py diagnose
python3 -B scripts/manage_install.py verify
```

`refresh-health` forces one slow read and then one normal sample so the card
updates immediately. It does not change the installed timer. `refresh` alone
updates the normal card using the current cache; `show` does no sampling.

## Installation, preservation and restoration

`sh install.sh` keeps saved Python/output/history/install paths unless explicitly
overridden, and adds `RUNCAT_BATTERY_HEALTH_FILE`. A missing or invalid native
health probe fails **before** replacing the old scripts/jobs. The initial power
probe uses temporary output/history files. The live verifier requires a usable
capacity observation, a cycle count, matching installed source/configuration,
a running fast sampler, and a registered six-hour job. It requires actual
atomic output replacement during a manual sample, even within the same second.

Both job states and all owned target files are backed up before replacement.
Detected failures restore both registered/disabled states and target bytes;
a restoration error is reported, not claimed successful. Installation/removal
CLI commands serialize using a setup lock. This is not power-loss/SIGKILL
atomicity. Empty locks, private backups and logs are retained for diagnosis.

For a successful installation that must be reversed, use the exact `Backup:`
directory printed by this version:

```sh
python3 -B scripts/manage_install.py rollback --backup '/absolute/printed/backup'
```

This accepts only a complete version-2 manifest with the current fixed target
paths and matching file hashes. It first backs up the current state. Corrupt,
foreign-target, incomplete or older-format backups are rejected before stopping
jobs. After custom path changes, use the matching installation/backup context;
this is not a general-purpose file restore command.

Uninstall removes both jobs. `--keep-data` keeps snapshot/history/health cache;
default uninstall backs up and removes those three data files. The integration
never recursively deletes `.runcat` or alters Codex/FX sources.

## Verification boundaries

Offline tests use synthetic native readings, real temporary files and child
processes, and separately modeled launchd jobs. A real `system_profiler` query,
launchd registration and card write are verified only on the installation Mac.
RunCat UI truncation, timer cycles, sleep/resume, and energy cost are not
certified by a green unit-test run. The scalar result can differ from a GUI
view taken at a different time; the reader displays the system report, not a
reverse-engineered or calibrated estimate of Apple's internal algorithm.

No optimization of existing power/temperature/runtime arithmetic is claimed.
Avoiding high-rate `system_profiler` is the principal efficiency decision.
