"""Freeze the reviewed arithmetic and explicit health source/cadence contract."""
import ast
import hashlib
from pathlib import Path
import unittest

ROOT=Path(__file__).resolve().parents[1]
EXPECTED={'property_literal': '9dfbe0522d5970505102c1a1d98a75dd5c92c99cb29391ea6e08d9e659d86082', 'dictionary_literal': '9fd6c5adb98c0774a736ffc3b6d22cd962d47c921b1c97e4e6ba12046d6004d0', 'integer_literal': '82480e12a2ad048202b869bbe27cb6044ba30b87a683cb973b2d6f7411ebdb64', 'raw_number': '627f2576b9012970731f23cb125a7f7c274085706b228619c8801cc7ff118eda', 'raw_bool': 'e5171f6f08c704febe017e6eeb5a2beb302b4fff34c6ba0564e42973d9fe44b1', 'signed_64': '21f24a0d9b001a00ee3bf9778764f97312a5f9867eccdd4822676c13d936543d', 'battery_power_w': '18704647f07ce3360cb9ffc569747357689a0a8a9ae82343ff075aa685591c96', 'battery_temperature_c': 'b5588a400a847a0af5c8151a7462021fdecfa01e741dfb6b3e248b4e62fed40d', 'read_battery_temperature_c': 'e72359617da634104223148850cf42c6c199bc8d12164dffc13decdf5e8afc24', 'battery_state': 'a3cc17bd49dc5ad10765d83557dded3cc1a77a3d675d29626b68be5e132f6acb', 'battery_remaining_energy_wh': '9804dddd5e51bff6af44094e2bf1e2c58b61c1e730a68bb983c17bb622b79ad0', 'estimated_runtime_hours': '383d7342b3b22cc403e6e43663d8411011227adf231e71874a8975b0758848e1', 'continuous_history': 'ef4ffd82f6f1d7bde01301fa8694d56662fb82b341e20339d27414ca36d814a6', 'update_history': '4c2ac8beeeff87808e4f28af356bde5074d08538cd31cf0a7cc07171c3451b08', 'five_minute_stats': '28410b8f7c00c15dce08e907775fa28b872b703efcc673fd6bd06c4d8e76b661', 'format_watts': '478de275fa3672e615dc55b74d904729e39b4db58384b4eef27c638d175d6682', 'format_temp': '177874a490cbfd1dee6149fbbd0454b4d8e210880787e197ed29bf9cc0bed038', 'format_runtime': 'a35c55cb68b643371f2e4024669206b052c11633b24366681da67e0dec0a0783', 'poll_interval_seconds': '10d009ab83e1ab9103f4ac4cfcab338bf13ef30e5a84f23c58880b0a5422bb18'}


class HealthContractTests(unittest.TestCase):
    def test_existing_arithmetic_and_parsers_are_unchanged(self):
        source=(ROOT/'update-battery.py').read_text()
        functions={n.name:n for n in ast.parse(source).body if isinstance(n,ast.FunctionDef)}
        for name,sha in EXPECTED.items():
            with self.subTest(name=name):
                self.assertEqual(hashlib.sha256(ast.get_source_segment(source,functions[name]).encode()).hexdigest(),sha)

    def test_documented_commands_and_staleness_are_explicit(self):
        notes=(ROOT/'docs/BATTERY_HEALTH.md').read_text()
        for word in ('sppower_battery_health_maximum_capacity','refresh-health','21600','cached','seven','rollback'):
            self.assertIn(word,notes.replace('21,600','21600'))
        self.assertIn('Maximum Capacity',(ROOT/'README.md').read_text())
        self.assertIn('Cycle Count',(ROOT/'README.md').read_text())
        self.assertIn('RUNCAT_BATTERY_HEALTH_FILE',(ROOT/'docs/RUNTIME_SETTINGS.md').read_text())

    def test_minimum_python_syntax_still_supported(self):
        for path in ROOT.rglob('*.py'):
            ast.parse(path.read_text(),feature_version=(3,10))

    def test_health_modes_not_in_fast_loop(self):
        loop=(ROOT/'adaptive-poll.sh').read_text()
        self.assertNotIn('system_profiler',loop)
        self.assertNotIn('--refresh-health',loop)

    def test_local_markdown_links_resolve(self):
        import re
        for path in [ROOT/'README.md', *ROOT.glob('docs/*.md')]:
            for link in re.findall(r'\]\(([^)#]+)(?:#[^)]*)?\)',path.read_text()):
                if '://' not in link:
                    self.assertTrue((path.parent/link).exists(),str(path)+' '+link)


if __name__=='__main__':unittest.main()
