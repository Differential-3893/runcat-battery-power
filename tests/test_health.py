"""Capacity/cycle contracts: synthetic data, no actual Mac queries or user files."""
from __future__ import annotations
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("battery_health_tests", ROOT / "update-battery.py")
battery = importlib.util.module_from_spec(spec)
spec.loader.exec_module(battery)


def report(cap="92%", cycles=156):
    return {"SPPowerDataType": [{"_name": "sppower_battery_information",
        "sppower_battery_health_info": {"sppower_battery_health_maximum_capacity": cap,
                                         "sppower_battery_cycle_count": cycles},
        "sppower_battery_serial_number": "PRIVATE_SENTINEL"},
        {"_name": "sppower_ac_charger_information", "serial": "PRIVATE_SENTINEL"}]}


TEXT = ('"BatteryInstalled" = Yes\n"CycleCount" = 156\n"MaxCapacity" = 100\n'
        '"ExternalConnected" = No\n"IsCharging" = No\n"Voltage" = 12000\n'
        '"InstantAmperage" = -1000\n"Temperature" = 3049\n'
        '"BatteryData" = {"FullChargeCapacity"=5280,"NominalChargeCapacity"=5430,'
        '"DesignCapacity"=6075,"RemainingCapacity"=4173}\n')


class HealthParsingTests(unittest.TestCase):
    def test_user_observation_uses_reported_92_not_100_89_or_87(self):
        got = battery.parse_health_report(json.dumps(report()))
        self.assertEqual(got, {"maximumCapacityPercent": 92, "cycleCount": 156})
        self.assertEqual(battery.cycle_count(TEXT), 156)
        self.assertNotEqual(round(5430 / 6075 * 100), 92)
        self.assertNotEqual(round(5280 / 6075 * 100), 92)

    def test_health_never_derived_from_ioreg_capacity_fields(self):
        bad = {"SPPowerDataType": [{"sppower_battery_health_info": {
            "FullChargeCapacity": 5280, "NominalChargeCapacity": 5430, "MaxCapacity": 100}}]}
        with self.assertRaises(RuntimeError):
            battery.parse_health_report(json.dumps(bad))

    def test_percent_string_integer_and_integral_number(self):
        for value in ("92%", " 92 % ", 92, 92.0, "100%", "1%"):
            self.assertIsNotNone(battery.health_percent(value))

    def test_invalid_percent_never_normalized_or_clamped(self):
        for value in (True, False, None, "92", "0.92", .92, 0, -1, 101, 6075,
                      "100.0%", "92%private", "99e2%", float('nan'), float('inf')):
            with self.subTest(value=value):
                self.assertIsNone(battery.health_percent(value))

    def test_unique_grouped_items_supported(self):
        payload = {"SPPowerDataType": [{"_items": report()["SPPowerDataType"]}]}
        self.assertEqual(battery.parse_health_report(json.dumps(payload))["maximumCapacityPercent"], 92)

    def test_other_datatypes_and_arbitrary_subtrees_not_selected(self):
        for payload in ({"Other": report()["SPPowerDataType"]},
                        {"SPPowerDataType": [{"charger": report()["SPPowerDataType"][0]}]}):
            with self.assertRaises(RuntimeError):
                battery.parse_health_report(json.dumps(payload))

    def test_two_battery_sections_are_not_guessed(self):
        data = report(); data['SPPowerDataType'].append(data['SPPowerDataType'][0])
        with self.assertRaises(RuntimeError): battery.parse_health_report(json.dumps(data))

    def test_malformed_report_shapes_fail_safely(self):
        for data in (None, [], {}, {"SPPowerDataType": {}}, {"SPPowerDataType": [None]},
                     {"SPPowerDataType": [{"_items": {}}]},
                     {"SPPowerDataType": [{"sppower_battery_health_info": []}]}):
            with self.subTest(data=data), self.assertRaises(RuntimeError):
                battery.parse_health_report(json.dumps(data))

    def test_duplicate_json_nonfinite_and_bad_encoding_rejected(self):
        for raw in (b'{"SPPowerDataType":[],"SPPowerDataType":[]}', b'NaN', b'\xff', b'{invalid'):
            with self.assertRaises(RuntimeError): battery.parse_health_report(raw)

    def test_oversized_or_excessive_depth_rejected(self):
        with self.assertRaises(RuntimeError): battery.parse_health_report(b' ' * (battery.MAX_PROFILER_BYTES + 1))
        data = report()['SPPowerDataType'][0]
        for _ in range(6): data = {'_items':[data]}
        with self.assertRaises(RuntimeError): battery.parse_health_report(json.dumps({'SPPowerDataType':[data]}))

    def test_profiler_cycles_are_optional_not_a_guess(self):
        for cycles in (None, True, -1, 100001, 1.25, '156x'):
            self.assertIsNone(battery.parse_health_report(json.dumps(report(cycles=cycles)))['cycleCount'])
        self.assertEqual(battery.parse_health_report(json.dumps(report(cycles='156')))['cycleCount'],156)

    def test_cycle_zero_missing_invalid_and_wrong_scope(self):
        self.assertEqual(battery.cycle_count('"CycleCount" = 0'), 0)
        for data in ('', '"CycleCount" = -1', '"CycleCount" = Yes', '"CycleCount" = 100001',
                     '"AdapterDetails" = {"CycleCount"=9}', '"LifetimeData" = {"CycleCount"=9}'):
            self.assertIsNone(battery.cycle_count(data))
        self.assertEqual(battery.cycle_count('"BatteryData" = {"CycleCount"=156}'),156)

    def test_absent_battery_has_no_cycle(self):
        self.assertIsNone(battery.cycle_count('"BatteryInstalled" = No\n"CycleCount" = 10'))

    def test_report_output_does_not_include_serials_or_health_strings(self):
        data = report(); data['SPPowerDataType'][0]['sppower_battery_health_info']['sppower_battery_health']='PRIVATE_SENTINEL'
        self.assertNotIn('PRIVATE', json.dumps(battery.parse_health_report(json.dumps(data))))


class HealthCacheTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="health 한글 '")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.cache, self.out, self.history = (self.root / name for name in ('health.json', 'out.json', 'history.json'))
        self.stack = contextlib.ExitStack(); self.addCleanup(self.stack.close)
        for key, path in (('HEALTH', self.cache), ('OUT', self.out), ('HISTORY', self.history)):
            self.stack.enter_context(patch.object(battery,key,path))
        self.now = time.time()

    def save(self, age=0, good=True, cycles=156):
        data = battery.health_record({'maximumCapacityPercent':92,'cycleCount':cycles}, self.now-age)
        if not good: data.update(lastAttemptAt=self.now, lastAttemptSucceeded=False)
        battery.atomic_write_json(self.cache,data)
        return data

    def display(self, text=TEXT): return battery.health_display(text, now=self.now)

    def test_fresh_cache_displays_92_and_observation_age(self):
        self.save(age=20)
        label, meta = self.display()
        self.assertEqual(label,'92%'); self.assertEqual(meta['HealthAgeSeconds'],20)
        self.assertFalse(meta['HealthCached'])

    def test_six_hour_overdue_cache_is_explicit(self):
        self.save(age=battery.HEALTH_INTERVAL_SECONDS+301)
        self.assertEqual(self.display()[0],'92% (cached)')

    def test_failed_refresh_preserves_value_and_observation_time(self):
        previous = self.save(age=22000)
        with patch.object(battery,'read_health_report',side_effect=RuntimeError('PRIVATE_SENTINEL')):
            self.assertFalse(battery.refresh_health())
        data = battery.load_health_cache()
        self.assertEqual(data['observedAt'],previous['observedAt'])
        self.assertEqual(data['maximumCapacityPercent'],92)
        self.assertFalse(data['lastAttemptSucceeded'])
        self.assertNotIn('PRIVATE',self.cache.read_text())
        self.assertEqual(battery.health_display(TEXT)[0],'92% (cached)')

    def test_failed_first_read_does_not_make_a_zero_or_100_value(self):
        with patch.object(battery,'read_health_report',side_effect=OSError('private')):
            self.assertFalse(battery.refresh_health())
        self.assertIsNone(battery.load_health_cache()['maximumCapacityPercent'])
        self.assertEqual(battery.health_display(TEXT)[0],'—')

    def test_failures_do_not_start_a_five_second_retry_loop(self):
        with patch.object(battery,'read_health_report',side_effect=OSError()) as probe:
            self.assertFalse(battery.refresh_health())
            for _ in range(50): battery.refresh_health()
            self.assertEqual(probe.call_count,1)

    def test_recent_cache_skips_startup_probe_and_does_not_rewrite(self):
        self.save()
        before=self.cache.read_bytes(); inode=self.cache.stat().st_ino
        with patch.object(battery,'read_health_report',side_effect=AssertionError('no query')):
            self.assertTrue(battery.refresh_health())
        self.assertEqual(self.cache.read_bytes(),before);self.assertEqual(self.cache.stat().st_ino,inode)

    def test_login_refresh_after_short_cooldown_does_not_wait_another_six_hours(self):
        self.save(age=1800)
        with patch.object(battery,'read_health_report',return_value={'maximumCapacityPercent':92,'cycleCount':156}) as probe:
            self.assertTrue(battery.refresh_health())
        probe.assert_called_once()

    def test_forced_refresh_replaces_cache_not_power_history(self):
        self.save(); self.out.write_text('metric'); self.history.write_text('history')
        with patch.object(battery,'read_health_report',return_value={'maximumCapacityPercent':91,'cycleCount':157}) as probe:
            self.assertTrue(battery.refresh_health(force=True)); probe.assert_called_once()
        self.assertEqual(battery.load_health_cache()['maximumCapacityPercent'],91)
        self.assertEqual(self.out.read_text(),'metric'); self.assertEqual(self.history.read_text(),'history')
        self.assertEqual(self.cache.stat().st_mode & 0o777,0o600)

    def test_expired_or_future_cache_never_shown_as_fresh(self):
        self.save(age=battery.HEALTH_MAX_AGE_SECONDS+1); self.assertEqual(self.display()[0],'—')
        self.save(age=-30); self.assertEqual(self.display()[0],'—')

    def test_cycle_decrease_hides_potentially_different_battery_cache(self):
        self.save(cycles=200); self.assertEqual(self.display()[0],'—')
        self.save(cycles=155); self.assertEqual(self.display()[0],'92%')

    def test_absent_battery_hides_cache(self):
        self.save(); self.assertEqual(self.display('"BatteryInstalled" = No')[0],'—')

    def test_corrupt_nonfinite_and_extra_fields_fail_closed(self):
        for raw in (b'{}',b'\xff',b'NaN',b'[]',b'{}'*10000):
            self.cache.write_bytes(raw); self.assertIsNone(battery.load_health_cache())
        data=self.save();data['private']='PRIVATE_SENTINEL';self.cache.write_text(json.dumps(data))
        self.assertIsNone(battery.load_health_cache())

    def test_cache_schema_types_are_validated(self):
        for key,value in (('version',True),('lastAttemptAt',True),('lastAttemptSucceeded',1),
                          ('observedAt',None),('maximumCapacityPercent',92.0),('cycleCount',True),('source','other')):
            data=self.save();data[key]=value;self.cache.write_text(json.dumps(data))
            with self.subTest(key=key): self.assertIsNone(battery.load_health_cache())

    def test_cache_symlink_and_fifo_are_not_followed_or_blocking(self):
        target=self.root/'keep';target.write_text('untouched');self.cache.symlink_to(target)
        self.assertIsNone(battery.load_health_cache());self.assertEqual(target.read_text(),'untouched')
        self.cache.unlink();os.mkfifo(self.cache)
        self.assertIsNone(battery.load_health_cache())

    def test_health_lock_symlink_is_not_followed(self):
        target=self.root/'keep';target.write_text('untouched')
        self.cache.with_name('.health.json.lock').symlink_to(target)
        with self.assertRaises(OSError): battery.refresh_health()
        self.assertEqual(target.read_text(),'untouched')

    def test_data_path_collision_stops_health_write(self):
        self.out.write_text('keep')
        with patch.object(battery,'HEALTH',self.out),self.assertRaises(ValueError): battery.refresh_health()
        self.assertEqual(self.out.read_text(),'keep')

    def test_health_exception_does_not_affect_fast_sample(self):
        self.cache.write_text('broken')
        with patch.object(battery,'run_ioreg',return_value=TEXT), \
             patch.object(battery,'read_health_report',side_effect=AssertionError('must not run')):
            battery.sample_once()
        data=json.loads(self.out.read_text());self.assertEqual(data['metrics'][5]['formattedValue'],'—')
        self.assertEqual(data['metrics'][6]['formattedValue'],'156')
        self.assertEqual(data['metricsBarValue'],'12.0 W')

    def test_fast_sample_adds_no_ioreg_or_system_profiler_call(self):
        self.save()
        with patch.object(battery,'run_ioreg',return_value=TEXT) as ioreg, \
             patch.object(battery,'read_health_report',side_effect=AssertionError('slow path')):
            for _ in range(5): battery.sample_once()
        self.assertEqual(ioreg.call_count,5)
        data=json.loads(self.out.read_text());self.assertEqual(len(data['metrics']),7)
        self.assertEqual(data['metrics'][5],{'title':'Maximum Capacity','formattedValue':'92%'})

    def test_first_five_rows_and_menu_do_not_depend_on_health(self):
        args=dict(power_w=12.0, temperature_c=30.5, state='discharging',average_w=11.,peak_w=13.,
                  runtime_hours=6.5,runtime_coverage_seconds=300,runtime_sample_count=61)
        a=battery.build_snapshot(**args)
        b=battery.build_snapshot(**args, maximum_capacity='92%', cycles=156)
        self.assertEqual(a['metrics'][:5],b['metrics'][:5]);self.assertEqual(a['metricsBarValue'],b['metricsBarValue'])
        self.assertNotIn('normalizedValue',b['metrics'][5])

    def test_diagnostics_explain_cached_source_without_running_profiler(self):
        self.save()
        with patch.object(battery,'read_health_report',side_effect=AssertionError('slow path')):
            value=battery.diagnostic(TEXT)
        self.assertEqual(value['MaximumCapacity'],'92%');self.assertEqual(value['CycleCount'],156)
        self.assertEqual(value['MaximumCapacitySource'],'system_profiler.SPPowerDataType')

    def test_unrelated_lock_does_not_block_sampling(self):
        self.save()
        # Holding a health lock is precisely what the slow worker does while
        # system_profiler runs. The sampler must never acquire that lock.
        with battery.sample_lock(self.cache), patch.object(battery,'run_ioreg',return_value=TEXT):
            self.assertEqual(battery.sample_once(),'discharging')

    def test_atomic_failure_preserves_previous_health_bytes(self):
        self.save();before=self.cache.read_bytes()
        with patch.object(battery.os,'replace',side_effect=OSError()), \
             patch.object(battery,'read_health_report',return_value={'maximumCapacityPercent':90,'cycleCount':157}), \
             self.assertRaises(OSError):
            battery.refresh_health(force=True)
        self.assertEqual(self.cache.read_bytes(),before)
        self.assertFalse(list(self.root.glob('.health.json-*')))


class HealthProcessTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory(prefix="profiler process '");self.addCleanup(temp.cleanup)
        self.root=Path(temp.name);self.command=self.root/'profiler';self.body=self.root/'profiler.py'
        self.command.write_text('#!/bin/sh\nexec '+shlex.quote(sys.executable)+' '+shlex.quote(str(self.body))+' "$@"\n')
        self.command.chmod(0o755)
        self.patcher=patch.object(battery,'SYSTEM_PROFILER',str(self.command));self.patcher.start();self.addCleanup(self.patcher.stop)

    def test_real_child_output_is_projected_and_command_is_fixed(self):
        self.body.write_text('import sys,json\nassert sys.argv[1:] == ["SPPowerDataType","-json","-timeout","15"]\nprint('+repr(json.dumps(report()))+')\n')
        self.assertEqual(battery.read_health_report(),{'maximumCapacityPercent':92,'cycleCount':156})

    def test_nonzero_exit_does_not_accept_valid_looking_payload(self):
        self.body.write_text('import sys\nprint('+repr(json.dumps(report()))+')\nsys.exit(1)\n')
        with self.assertRaisesRegex(RuntimeError,'query failed'):battery.read_health_report()

    def test_large_report_child_is_killed_and_reaped(self):
        self.body.write_text('import sys,time\nsys.stdout.write("x"*600000);sys.stdout.flush();time.sleep(30)\n')
        with self.assertRaisesRegex(RuntimeError,'too large'):battery.read_health_report()

    def test_deadline_kills_and_reaps_sleeping_child(self):
        self.body.write_text('import os,time\nopen('+repr(str(self.root/'pid'))+',"w").write(str(os.getpid()))\ntime.sleep(30)\n')
        original = subprocess.Popen
        children = []
        def launch(*a, **kw):
            child = original(*a, **kw); children.append(child); return child
        with patch.object(battery.subprocess, 'Popen', side_effect=launch), \
             patch.object(battery,'HEALTH_QUERY_TIMEOUT_SECONDS',.5),self.assertRaisesRegex(RuntimeError,'timed out'):
            battery.read_health_report()
        self.assertEqual(len(children), 1)
        self.assertIsNotNone(children[0].poll())
        with self.assertRaises(ProcessLookupError):os.kill(children[0].pid,0)

    def test_private_stderr_never_reaches_exception(self):
        self.body.write_text('import sys\nprint("PRIVATE_SENTINEL",file=sys.stderr)\nsys.exit(2)\n')
        with self.assertRaises(RuntimeError) as error:battery.read_health_report()
        self.assertNotIn('PRIVATE',str(error.exception))


if __name__ == '__main__':unittest.main()
