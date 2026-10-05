"""Two-job install/rollback with real temporary files, synthetic native tools."""
from __future__ import annotations
import json
from pathlib import Path
import plistlib
import subprocess
import time
import unittest
from unittest.mock import patch
import test_install as fixture

manager=fixture.manager


class HealthInstallTests(unittest.TestCase):
    setUp=fixture.InstallTests.setUp
    old_install=fixture.InstallTests.old_install

    def test_install_registers_separate_6h_one_shot_with_same_runtime(self):
        cfg=manager.Config();manager.install(cfg)
        self.assertTrue(self.launch.is_loaded);self.assertTrue(self.launch.health_loaded)
        p=plistlib.loads(cfg.health_plist.read_bytes())
        self.assertEqual(p['StartInterval'],21600);self.assertTrue(p['RunAtLoad'])
        self.assertNotIn('KeepAlive',p)
        self.assertEqual(p['ProgramArguments'],[cfg.python,str(cfg.install/'update-battery.py'),'--refresh-health'])
        self.assertEqual(p['EnvironmentVariables'],cfg.variables())
        data=json.loads(cfg.out.read_text())
        self.assertEqual([m['formattedValue'] for m in data['metrics'][5:]],['92%','156'])

    def test_health_probe_failure_keeps_previous_running_install(self):
        cfg=manager.Config();before=self.old_install(cfg);self.launch.is_loaded=True
        with patch.object(manager,'live_health',side_effect=RuntimeError('unavailable')),self.assertRaises(RuntimeError):
            manager.install(cfg)
        self.assertEqual(self.launch.calls,[])
        self.assertEqual(before,{p:p.read_bytes() for p in cfg.targets()})

    def test_failing_second_job_restores_both_prior_job_states(self):
        cfg=manager.Config();before=self.old_install(cfg)
        self.launch.is_loaded=True;self.launch.health_loaded=True
        self.launch.fail_health_bootstraps=1
        with self.assertRaises(RuntimeError):manager.install(cfg)
        self.assertTrue(self.launch.is_loaded);self.assertTrue(self.launch.health_loaded)
        self.assertEqual(before,{p:p.read_bytes() for p in cfg.targets()})

    def test_fresh_failure_restores_both_disabled_flags_and_file_absence(self):
        cfg=manager.Config();self.launch.is_disabled=True;self.launch.health_disabled=True
        self.launch.fail_health_bootstraps=1
        with self.assertRaises(RuntimeError):manager.install(cfg)
        self.assertTrue(self.launch.is_disabled);self.assertTrue(self.launch.health_disabled)
        self.assertFalse(self.launch.is_loaded);self.assertFalse(self.launch.health_loaded)
        for path in cfg.targets():self.assertFalse(path.exists())

    def test_failed_stop_does_not_replace_any_target(self):
        cfg=manager.Config();before=self.old_install(cfg)
        self.launch.is_loaded=True;self.launch.health_loaded=True;self.launch.fail_health_bootout=True
        with self.assertRaises(RuntimeError):manager.install(cfg)
        self.assertEqual(before,{p:p.read_bytes() for p in cfg.targets()})
        self.assertTrue(self.launch.is_loaded);self.assertTrue(self.launch.health_loaded)

    def test_custom_cache_path_survives_plain_reinstall(self):
        with patch.dict(manager.os.environ,{'RUNCAT_BATTERY_HEALTH_FILE':str(self.home/'elsewhere/cache.json')}):
            cfg=manager.Config();manager.install(cfg)
        again=manager.Config();self.assertEqual(again.health,cfg.health)
        manager.install(again);self.assertEqual(again.variables(),cfg.variables())

    def test_cache_cannot_replace_output_history_or_slow_tool(self):
        cfg=manager.Config()
        for path in (cfg.out,cfg.history,cfg.plist,cfg.health_plist,Path('/usr/sbin/system_profiler')):
            with patch.dict(manager.os.environ,{'RUNCAT_BATTERY_HEALTH_FILE':str(path)}),self.assertRaises(RuntimeError):manager.Config()

    def test_uninstall_keep_data_removes_both_jobs_but_keeps_cache(self):
        cfg=manager.Config();manager.install(cfg);cache=cfg.health.read_bytes()
        manager.uninstall(cfg,True)
        self.assertFalse(self.launch.is_loaded);self.assertFalse(self.launch.health_loaded)
        self.assertFalse(cfg.plist.exists());self.assertFalse(cfg.health_plist.exists())
        self.assertEqual(cfg.health.read_bytes(),cache)

    def test_verify_fails_if_health_job_is_not_registered(self):
        cfg=manager.Config();manager.install(cfg);self.launch.health_loaded=False
        with self.assertRaisesRegex(RuntimeError,'Six-hour'):manager.verify(cfg)

    def test_private_backup_permissions_and_hashes(self):
        cfg=manager.Config();self.old_install(cfg);manager.install(cfg)
        backups=list((cfg.home/'backups').glob('*/RESTORE.json'));self.assertEqual(len(backups),1)
        info=json.loads(backups[0].read_text());self.assertEqual(info['version'],2)
        for entry in info['files']:
            if entry['backup']:
                path=backups[0].parent/entry['backup']
                self.assertEqual(path.stat().st_mode & 0o777,0o600)
                self.assertEqual(manager.hashlib.sha256(path.read_bytes()).hexdigest(),entry['sha256'])

    def test_manual_rollback_restores_previous_bytes_and_job_states(self):
        cfg=manager.Config();before=self.old_install(cfg);self.launch.is_loaded=True
        manager.install(cfg)
        backup=next((cfg.home/'backups').glob('*/RESTORE.json')).parent
        manager.rollback(cfg,backup)
        self.assertEqual(before,{p:p.read_bytes() for p in cfg.targets()})
        self.assertTrue(self.launch.is_loaded);self.assertFalse(self.launch.health_loaded)

    def test_corrupted_backup_is_rejected_before_stopping_jobs(self):
        cfg=manager.Config();self.old_install(cfg);manager.install(cfg)
        backup=next((cfg.home/'backups').glob('*/RESTORE.json')).parent
        info=json.loads((backup/'RESTORE.json').read_text())
        (backup/info['files'][0]['backup']).write_text('tampered')
        self.launch.calls.clear()
        with self.assertRaisesRegex(RuntimeError,'checksum'):manager.rollback(cfg,backup)
        self.assertEqual(self.launch.calls,[])

    def test_foreign_target_in_backup_is_rejected(self):
        cfg=manager.Config();self.old_install(cfg);manager.install(cfg)
        backup=next((cfg.home/'backups').glob('*/RESTORE.json')).parent
        info=json.loads((backup/'RESTORE.json').read_text());info['files'][0]['path']=str(self.home/'unrelated')
        (backup/'RESTORE.json').write_text(json.dumps(info));self.launch.calls.clear()
        with self.assertRaisesRegex(RuntimeError,'targets'):manager.rollback(cfg,backup)
        self.assertEqual(self.launch.calls,[])

    def test_no_raw_profile_is_saved_or_required_in_backup(self):
        cfg=manager.Config();manager.install(cfg)
        self.assertEqual(set(json.loads(cfg.health.read_text())),{
            'version','source','maximumCapacityPercent','cycleCount','observedAt','lastAttemptAt','lastAttemptSucceeded'})
        self.assertNotIn('serial',cfg.health.read_text().lower())

    def test_recent_unchanged_file_cannot_pass_live_sample_check(self):
        cfg=manager.Config();cfg.out.parent.mkdir(parents=True,exist_ok=True)
        cfg.out.write_text('{}')
        # Outer test fixture mocks live_sample. Use a fresh module to exercise
        # the production per-invocation replacement check, not that mock.
        import importlib.util
        spec=importlib.util.spec_from_file_location('fresh_live_manager',manager.ROOT/'scripts/manage_install.py')
        fresh=importlib.util.module_from_spec(spec);spec.loader.exec_module(fresh)
        with patch.object(fresh.subprocess,'run',return_value=subprocess.CompletedProcess([],0,'','')), \
             self.assertRaisesRegex(RuntimeError,'No atomic'):
            fresh.live_sample(manager.ROOT/'update-battery.py',cfg)

    def test_same_second_identical_data_is_valid_after_atomic_write(self):
        cfg=manager.Config();manager.install(cfg)
        import importlib.util
        spec=importlib.util.spec_from_file_location('fresh_live_manager2',manager.ROOT/'scripts/manage_install.py')
        fresh=importlib.util.module_from_spec(spec);spec.loader.exec_module(fresh)
        raw=cfg.out.read_bytes()
        def run(*args,**kwargs):
            manager.atomic_bytes(cfg.out,raw,0o600)
            return subprocess.CompletedProcess([],0,'','')
        with patch.object(fresh.subprocess,'run',side_effect=run):
            self.assertEqual(fresh.live_sample(manager.ROOT/'update-battery.py',cfg)['metrics'][5]['formattedValue'],'92%')

    def test_legacy_upgrade_failure_removes_only_new_health_files(self):
        cfg=manager.Config();self.old_install(cfg)
        cfg.health_plist.unlink();cfg.health.unlink()
        old=cfg.payload();old['EnvironmentVariables'].pop('RUNCAT_BATTERY_HEALTH_FILE')
        cfg.plist.write_bytes(plistlib.dumps(old))
        before={p:p.read_bytes() if p.exists() else None for p in cfg.targets()}
        self.launch.is_loaded=True;self.launch.fail_health_bootstraps=1
        with self.assertRaises(RuntimeError):manager.install(manager.Config())
        self.assertEqual(before,{p:p.read_bytes() if p.exists() else None for p in cfg.targets()})
        self.assertTrue(self.launch.is_loaded);self.assertFalse(self.launch.health_loaded)

    def test_live_probe_uses_saved_python_and_returns_only_typed_scalars(self):
        import importlib.util
        spec=importlib.util.spec_from_file_location('fresh_health_probe',manager.ROOT/'scripts/manage_install.py')
        fresh=importlib.util.module_from_spec(spec);spec.loader.exec_module(fresh)
        cfg=manager.Config()
        with patch.object(fresh.subprocess,'run',return_value=subprocess.CompletedProcess([],0,'{"maximumCapacityPercent":92,"cycleCount":156}','')) as run:
            data=fresh.live_health(cfg)
        self.assertEqual(data['maximumCapacityPercent'],92)
        self.assertEqual(run.call_args.args[0],[cfg.python,'-B',str(manager.ROOT/'update-battery.py'),'--health-probe'])
        self.assertEqual(run.call_args.kwargs['env']['RUNCAT_BATTERY_HEALTH_FILE'],str(cfg.health))
        self.assertFalse(cfg.health.exists())

    def test_live_probe_failure_never_echoes_native_private_output(self):
        import importlib.util
        spec=importlib.util.spec_from_file_location('fresh_health_probe_failure',manager.ROOT/'scripts/manage_install.py')
        fresh=importlib.util.module_from_spec(spec);spec.loader.exec_module(fresh)
        cfg=manager.Config()
        with patch.object(fresh.subprocess,'run',return_value=subprocess.CompletedProcess([],1,'PRIVATE_SENTINEL','PRIVATE_SENTINEL')):
            with self.assertRaises(RuntimeError) as error:fresh.live_health(cfg)
        self.assertNotIn('PRIVATE',str(error.exception));self.assertFalse(cfg.health.exists())


if __name__ == '__main__':unittest.main()
