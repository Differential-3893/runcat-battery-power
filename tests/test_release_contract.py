"""Keep supported manual commands, local documentation links and CI policy aligned."""
from __future__ import annotations
import ast
from pathlib import Path
import re
import subprocess
import unittest
from urllib.parse import unquote,urlsplit

ROOT = Path(__file__).resolve().parents[1]


class ReleaseContractTests(unittest.TestCase):
    def test_readme_minimum_python_matches_installer_contract(self):
        tree=ast.parse((ROOT/'scripts/manage_install.py').read_text())
        minimum=next(ast.literal_eval(node.value) for node in tree.body if isinstance(node,ast.Assign)
                     and any(isinstance(n,ast.Name) and n.id=='MIN_PYTHON' for n in node.targets))
        version='.'.join(map(str,minimum))
        self.assertRegex((ROOT/'README.md').read_text(),r'Python\s+\*\*'+re.escape(version)+r'\+')
        for p in ROOT.rglob('*.py'):
            if '__pycache__' not in p.parts:
                ast.parse(p.read_text(),filename=str(p),feature_version=minimum)

    def test_documented_installed_commands_exist_and_have_help(self):
        readme=(ROOT/'README.md').read_text()
        for action in ('refresh','show','diagnose'):
            self.assertIn('python3 -B scripts/run_installed.py '+action,readme)
        self.assertNotIn('python3 ~/.codex/runcat-neo-hook.py',readme)
        self.assertNotIn('python3 ~/.runcat/runcat-battery-power/update-battery.py',readme)
        p=subprocess.run([__import__('sys').executable,'-B',str(ROOT/'scripts/run_installed.py'),'--help'],
                         capture_output=True,text=True,timeout=5)
        self.assertEqual(p.returncode,0,p.stderr)
        for action in ('refresh','show','diagnose'):self.assertIn(action,p.stdout)

    def test_all_relative_markdown_links_have_existing_targets(self):
        for p in [ROOT/'README.md',*sorted((ROOT/'docs').glob('*.md'))]:
            for match in re.finditer(r'\[[^\]]*\]\(([^\s)]+)(?:\s+[^)]*)?\)',p.read_text()):
                link=match.group(1);url=urlsplit(link)
                if url.scheme or url.netloc or not url.path:continue
                target=(p.parent/unquote(url.path)).resolve()
                self.assertTrue(target.is_relative_to(ROOT.resolve()),(p,link))
                self.assertTrue(target.is_file(),(p,link))

    def test_ci_uses_full_pinned_checkout_without_write_credentials(self):
        s=(ROOT/'.github/workflows/ci.yml').read_text()
        actions=re.findall(r'^\s*-\s*uses:\s*(\S+)',s,re.M)
        self.assertEqual(actions,['actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1'])
        self.assertRegex(s,r'@3d3c42e5aac5ba805825da76410c181273ba90b1\s+# v7\.0\.1')
        self.assertIn('contents: read',s);self.assertNotIn('write-all',s)
        self.assertIn('persist-credentials: false',s)
        self.assertIn('timeout-minutes: 10',s)
        self.assertNotIn('pull_request_target',s)
        self.assertIn('os: [ubuntu-latest, macos-latest]',s)
        self.assertIn('python3 -B -m unittest discover -s tests -v',s)
        self.assertNotIn('continue-on-error',s)
        for shell in ROOT.rglob('*.sh'):
            relative=shell.relative_to(ROOT).as_posix()
            self.assertIn('sh -n '+relative,s)


if __name__=='__main__':unittest.main()
