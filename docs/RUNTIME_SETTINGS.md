# Reinstallation and runtime settings

This change follows base commit `ccc3dc10aaa100f6eb0763d641496b542e27f49a`.
It does not change battery telemetry, temperature conversion, history,
estimation, the metric layout or the 5/60-second adaptive cadence.

## Shared preservation rule

For each supported setting, a nonempty explicit environment override wins;
otherwise reuse the recorded installation value; use the documented default
only on a fresh installation or when that field was never recorded. An empty
explicit value is an error, not a request to forget the previous value. To
return to a default, explicitly pass that default path. Keep executable symlink
paths (for example Homebrew bin/opt aliases), rather than resolving into Cellar.

The Python used to bootstrap the installer is not automatically a request to
change the installed runtime Python. Both shell entrypoints and direct manager
invocation follow this rule. The shell passes its bootstrap path as a separate
fallback-only argument, preserving its stable alias on fresh installations
without overriding a recorded runtime. If a saved executable has disappeared, stop before
replacing the installation rather than silently choosing a different executable.
An explicit executable override can repair that situation. Installation checks
the version of the selected runtime, not just the bootstrap interpreter.

The contract covers the named settings below, not arbitrary manual plist/wrapper
edits, polling-policy changes, or preservation of unknown environment variables.
Generated wrappers and LaunchAgents are regenerated from the supported settings.
Changing a path does not move or delete data at the old location. Each setting is
independent; a home-directory override does not reset separately saved paths.
Never source/eval an installed shell wrapper to recover settings.

## Verification

`python3 -B -m unittest discover -s tests -v`

The additional runtime-setting tests execute the real `sh install.sh` and
`sh uninstall.sh` entrypoints, with temporary HOME directories and different
bootstrap/runtime Python aliases. macOS telemetry and launchctl are synthetic
or mocked in these tests; they do not replace a native local check. The tests
cover clean-shell reinstallation, explicit overrides, quoted/non-ASCII paths,
empty overrides, saved-executable removal and repeatability. Existing producer,
privacy and installation rollback tests remain in place.

## Battery settings

The existing LaunchAgent environment records `RUNCAT_HOME`, `RUNCAT_OUT_FILE`,
`RUNCAT_BATTERY_HISTORY_FILE`, `RUNCAT_BATTERY_INSTALL_DIR` and `PYTHON_BIN`.
These remain the supported overrides. Fresh defaults are `~/.runcat`,
`<RUNCAT_HOME>/battery-power.json`, `<RUNCAT_HOME>/battery-power-history.json`,
`<RUNCAT_HOME>/runcat-battery-power`, and the bootstrap Python (3.10+).
Legacy installation directories can still be recovered from
`RUNCAT_BATTERY_SCRIPT`. A legacy custom history path that was never stored in
the plist must still be supplied once; it cannot be inferred.

The concrete defect was in the shell wrapper, not in the manager's recorded
path precedence. The old shell always exported PYTHON_BIN, so a normal
reinstall could replace the saved runtime with the current PATH's Python.
The new wrapper uses a separate, unexported bootstrap variable. Empty values
are rejected consistently instead of falling through to old/default values.
The entrypoint tests exercise the actual installer (with mocked native
services), rather than calling Config alone and missing this wrapper defect.

```sh
sh install.sh
PYTHON_BIN=/opt/homebrew/bin/python3.12 sh install.sh  # explicit replacement
python3 -B scripts/manage_install.py verify
```

## Installed manual commands

From the repository, `python3 -B scripts/run_installed.py refresh`, `show`, or
`diagnose` reads the saved LaunchAgent and uses its recorded runtime. Ambient
project overrides do not change those commands' target; change settings through
an explicit installation override instead. `show` is a saved-file read, not a
fresh observation. The Python that launches this helper need not be the runtime
Python; queries are dispatched to the recorded executable. No wrapper is sourced
or evaluated to recover settings.
