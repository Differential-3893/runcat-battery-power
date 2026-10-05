"""Execute the documented installed-runtime commands with synthetic telemetry."""
from __future__ import annotations
import importlib.util
import json
import os
from pathlib import Path
import plistlib
import shlex
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts/run_installed.py'
spec = importlib.util.spec_from_file_location('manual_helper', SCRIPT)
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)


class ManualCommandTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="manual 한글 '")
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.runtime = self.home / 'saved runtime'
        self.runtime.mkdir()
        self.alias = self.home / 'saved Python'
        self.spy = self.home / 'spy.py'
        self.calls = self.home / 'python-calls.jsonl'
        self.spy.write_text(r'''import json,os,subprocess,sys
from pathlib import Path
with Path(os.environ['SPY_RECORD']).open('a') as f:
    f.write(json.dumps({'argv':sys.argv[1:], 'env':dict(os.environ)})+'\n')
raise SystemExit(subprocess.run([sys.executable,*sys.argv[1:]]).returncode)
''')
        self.alias.write_text('#!/bin/sh\nexec '+shlex.quote(sys.executable)+' '+shlex.quote(str(self.spy))+' "$@"\n')
        self.alias.chmod(0o755)
        self.out = self.home / 'custom out' / 'metric.json'
        self.out.parent.mkdir()
        self.history = self.home / 'custom history.json'
        self.producer = self.runtime / ('runcat-neo-hook.py' if helper.PROJECT == 'codex-runcat-neo' else 'update-battery.py')
        self.producer.write_text('''import json,os,sys
from pathlib import Path
from datetime import datetime,timezone
def fetch_account_data():
    return {'planType':'pro','credits':{'hasCredits':True,'balance':'2'}}
if __name__ == '__main__':
    if '--diagnose' in sys.argv:
        print(json.dumps({'TemperatureC':30.5,'PowerW':12.0}))
    else:
        card={'title':TITLE,'symbol':'test','metrics':[{'title':'test','formattedValue':'12.0 W'}],
              'lastUpdatedDate':datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}
        Path(os.environ['RUNCAT_OUT_FILE']).write_text(json.dumps(card))
'''.replace('TITLE',repr(helper.TITLE)))
        self.env = {k:v for k,v in os.environ.items() if not k.startswith(('RUNCAT_','CODEX_')) and k not in ('PYTHON_BIN','PYTHONPATH','PYTHONHOME')}
        self.env.update(HOME=str(self.home), SPY_RECORD=str(self.calls), PYTHONDONTWRITEBYTECODE='1',
                        RUNCAT_OUT_FILE=str(self.home/'wrong.json'), CODEX_HOME='/deliberately/wrong',
                        PYTHON_BIN='/does-not-exist', RUNCAT_BATTERY_HISTORY_FILE='/wrong/history')
        self.plist = self.home / 'Library/LaunchAgents' / (helper.LABEL+'.plist')
        self.plist.parent.mkdir(parents=True)
        if helper.PROJECT == 'codex-runcat-neo':
            saved = {'CODEX_HOME':str(self.runtime), 'CODEX_BIN':'/saved/bin/codex',
                     'PATH':os.environ.get('PATH',os.defpath), 'RUNCAT_OUT_FILE':str(self.out)}
            args = [str(self.alias),str(self.producer),'--refresh']
        else:
            saved = {'RUNCAT_HOME':str(self.runtime), 'RUNCAT_OUT_FILE':str(self.out),
                     'PYTHON_BIN':str(self.alias), 'RUNCAT_BATTERY_SCRIPT':str(self.producer),
                     'RUNCAT_BATTERY_HISTORY_FILE':str(self.history),'RUNCAT_BATTERY_INSTALL_DIR':str(self.runtime)}
            args = ['/bin/sh',str(self.runtime/'adaptive-poll.sh')]
        self.payload = {'Label':helper.LABEL,'ProgramArguments':args,'EnvironmentVariables':saved}
        self.save()

    def save(self):
        self.plist.write_bytes(plistlib.dumps(self.payload))

    def run_helper(self, action, expected=0):
        p = subprocess.run([sys.executable,'-B',str(SCRIPT),action],env=self.env,
                           capture_output=True,text=True,timeout=25)
        self.assertEqual(p.returncode,expected,p.stdout+p.stderr)
        return p

    def test_refresh_uses_saved_python_paths_and_environment(self):
        result = json.loads(self.run_helper('refresh').stdout)
        self.assertEqual(result['title'],helper.TITLE)
        self.assertFalse((self.home/'wrong.json').exists())
        call = json.loads(self.calls.read_text().splitlines()[0])
        self.assertEqual(call['argv'][0],str(self.producer))
        self.assertEqual(call['env']['RUNCAT_OUT_FILE'],str(self.out))
        key = 'CODEX_HOME' if helper.PROJECT == 'codex-runcat-neo' else 'RUNCAT_BATTERY_HISTORY_FILE'
        self.assertEqual(call['env'][key],self.payload['EnvironmentVariables'][key])
        self.assertNotIn('PYTHONPATH',call['env'])
        self.assertNotIn('PYTHONHOME',call['env'])

    def test_show_only_reads_saved_file_without_running_python(self):
        self.out.write_text(json.dumps({'title':helper.TITLE,'metrics':[], 'lastUpdatedDate':'2000-01-01T00:00:00Z'}))
        p=self.run_helper('show')
        self.assertIn('2000-01-01',p.stdout)
        self.assertFalse(self.calls.exists())

    def test_diagnose_uses_saved_runtime_without_snapshot_write(self):
        self.out.write_text('previous bytes')
        before=self.plist.read_bytes()
        value=json.loads(self.run_helper('diagnose').stdout)
        self.assertIn('planType' if helper.PROJECT=='codex-runcat-neo' else 'TemperatureC',value)
        self.assertEqual(self.out.read_text(),'previous bytes')
        self.assertEqual(self.plist.read_bytes(),before)
        self.assertTrue(self.calls.exists())

    def test_wrong_label_does_not_execute_a_command(self):
        self.payload['Label']='another.service';self.save()
        self.run_helper('refresh',1)
        self.assertFalse(self.calls.exists())

    def test_bad_command_is_rejected(self):
        self.payload['ProgramArguments']=['/bin/sh','/somebody-elses-script.sh'];self.save()
        self.run_helper('refresh',1)
        self.assertFalse(self.calls.exists())

    def test_invalid_configuration_does_not_leak_contents(self):
        self.plist.write_bytes(b'PRIVATE_SENTINEL <broken>')
        p=self.run_helper('refresh',1)
        self.assertNotIn('PRIVATE_SENTINEL',p.stdout+p.stderr)
        self.assertNotIn('Traceback',p.stderr)

    def test_missing_saved_python_does_not_fall_back(self):
        self.alias.unlink()
        self.run_helper('refresh',1)
        self.assertFalse(self.calls.exists())
        self.assertFalse(self.out.exists())

    def test_failure_does_not_print_raw_stderr_or_saved_snapshot(self):
        self.producer.write_text("import sys\nprint('PRIVATE_SENTINEL',file=sys.stderr)\nsys.exit(1)\n")
        self.out.write_text('old snapshot retained')
        p=self.run_helper('refresh',1)
        self.assertNotIn('PRIVATE_SENTINEL',p.stdout+p.stderr)
        self.assertEqual(self.out.read_text(),'old snapshot retained')
        self.assertEqual(p.stdout,'')

    def test_successful_exit_without_new_snapshot_is_not_success(self):
        self.producer.write_text('pass\n')
        self.out.write_text(json.dumps({'title':helper.TITLE,'metrics':[], 'lastUpdatedDate':'2000-01-01T00:00:00Z'}))
        self.run_helper('refresh',1)

    def test_symlink_configuration_is_rejected(self):
        original=self.home/'unrelated.plist';self.plist.rename(original);self.plist.symlink_to(original)
        self.run_helper('refresh',1)
        self.assertFalse(self.calls.exists())

    def test_refresh_health_uses_saved_path_then_updates_card(self):
        cache=self.home/'saved health cache.json'
        self.payload['EnvironmentVariables']['RUNCAT_BATTERY_HEALTH_FILE']=str(cache)
        self.save()
        self.run_helper('refresh-health')
        records=[json.loads(line) for line in self.calls.read_text().splitlines()]
        self.assertEqual(len(records),2)
        self.assertEqual(records[0]['argv'][-2:],['--refresh-health','--force-health'])
        self.assertEqual(records[0]['env']['RUNCAT_BATTERY_HEALTH_FILE'],str(cache))
        self.assertEqual(records[1]['env']['RUNCAT_BATTERY_HEALTH_FILE'],str(cache))


if __name__ == '__main__': unittest.main()
