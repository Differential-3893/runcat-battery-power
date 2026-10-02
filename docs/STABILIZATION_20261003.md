# Stabilization — 2026-10-03

Base: `ec5fff1466a973729d0c7130feccc7e4a753c09e` in
`Differential-3893/runcat-battery-power`.

## Corrected behavior

The original source and fixtures were fetched at the pinned revision and their
Git blob hashes were verified before modification. The original 12 temperature
tests passed. Additional synthetic reproductions exposed these failures:

* Six samples at timestamps 700–725 followed by one at 1000 yielded 300 seconds
  of confidence despite a 275-second gap, and a settled runtime of `3h 26m`.
  The revised sampler restarts with one fresh observation and `Calculating…`.
* A history sample with NaN produced `nan W` and a nonstandard JSON `NaN` value.
  A list-valued state or invalid UTF-8 could abort the update. Invalid history,
  future timestamps, clock reversals and duplicate timestamps are now handled
  conservatively; nonfinite values are never serialized into JSON.
* Missing current power retained an old rolling average. It now clears the
  estimator history instead of treating the old observation as current.
* Valid zero current without a BatteryPower field yielded unavailable power.
  It now yields 0 W; a zero instantaneous current is not replaced with an older,
  nonzero Amperage value.
* With a nested adapter Voltage=5000 before top-level Voltage=12000 and current
  -1000 mA, the original fallback reported 5 W rather than 12 W. Numeric parsing
  now checks a whole top-level property, or a direct BatteryData field. Adapter
  and lifetime dictionaries, partial numeric tokens and ambiguous duplicates
  are not used as current battery telemetry.

The rolling average retains its time-weighted trapezoidal definition and now
clips/interpolates the left edge of the 300-second window. It does not count
an interpolated boundary as an additional observation. A real interprocess
advisory lock serializes the whole sample/history/snapshot operation.

The shell loop still launches Python only for individual samples and sleeps
between them. Only 5 or 60 are accepted as producer-supplied delays; a failing
sample retains a 60-second retry delay. Termination also cleans up its sleeper.

## Installation and deployment

The installer probes telemetry in temporary files before replacing a working
installation. It then stops its own LaunchAgent, backs up its own targets,
atomically replaces scripts/plist, and verifies installed source, explicit path
environment, a running job, and a new real telemetry snapshot. A detected
installation error triggers restoration of previous target bytes/modes and
launch state. If restoration itself fails it reports `RESTORE INCOMPLETE`;
backups remain available. This is not a guarantee against power loss or SIGKILL.

Snapshot, history and installation paths are passed consistently to the initial
sample and launchd. Reinstallation without explicit overrides reuses the
existing plist's paths. An old custom history path absent from the old plist
cannot be inferred; pass RUNCAT_BATTERY_HISTORY_FILE once during migration.

Uninstallation supports the existing --keep-data option. It retains backups,
logs and the empty lock inode, and never recursively deletes a shared folder.
No Codex integration files or unrelated custom metric files are targeted.

## Retained decisions

Battery-side power, the five card rows, `bolt.circle`, temperature /100 conversion,
optional pack-only temperature fallback, signed 64-bit current/power conversion,
5/60-second cadence, 60/240-second runtime confidence thresholds, and the default
`~/.runcat/battery-power.json` path remain. Temperature queries still have their
existing two-second timeout and the pack is queried only when needed. No network
request, admin privilege, new dependency, or additional persistent sampler is
introduced. Existing CI discovers the added tests without a workflow change.

A gap longer than three scheduled intervals resets the session (15 seconds on
battery, 180 while charging). That threshold is an explicit continuity policy,
not automatic detection of every sleep/wake or unobserved AC transition.

## Verification and limits

The delivery environment was Linux, Python 3.13.5. The source suite has 48 test
methods, including the existing 12 temperature tests, 100 deterministic random
integration cases, actual subprocess lock/loop tests, and mocked-launchctl
installation/rollback tests using synthetic battery samples. The delivery
publisher has 11 additional offline tests. macOS `ioreg`, GUI launchd, actual
charge/discharge transitions, a full polling cycle, and RunCat rendering were
not run in that environment. The installer performs live checks on the user's
Mac; those do not automatically observe the UI or an entire polling interval.
No CPU, battery-life or hardware-accuracy benchmark is claimed.

These corrections close the reproduced failures. A future change should be
justified by a reproducible defect or an observed schema change, not an arbitrary
request to refactor a working display or guess another temperature unit.
