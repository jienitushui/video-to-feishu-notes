import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from vfnlib.credentials import CREDENTIAL_KEYS, load_credentials

ROOT = Path(__file__).resolve().parents[1]


class CredentialTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.env_file = self.root / '.env.local'
        self.env_patch = patch.dict(os.environ, {}, clear=True)
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)
        self.root_patch = patch('vfnlib.credentials.ROOT', self.root)
        self.root_patch.start()
        self.addCleanup(self.root_patch.stop)

    def test_default_file_loads_quoted_credentials_and_bom_without_shell_evaluation(self):
        secret = 'literal ${HOME} $(whoami) ! % & = # 中文'
        self.env_file.write_text('FEISHU_APP_ID=cli_test\nFEISHU_APP_SECRET="'
                                 + secret + '"\nPATH=do-not-load\n', encoding='utf-8-sig')
        self.assertEqual(load_credentials(), self.env_file)
        self.assertEqual(os.environ['FEISHU_APP_ID'], 'cli_test')
        self.assertEqual(os.environ['FEISHU_APP_SECRET'], secret)
        self.assertNotIn('PATH', os.environ)

    def test_existing_environment_including_empty_values_wins(self):
        self.env_file.write_text('FEISHU_APP_ID=file\nFEISHU_APP_SECRET=file-secret\n', encoding='utf-8')
        os.environ.update(FEISHU_APP_ID='existing', FEISHU_APP_SECRET='')
        load_credentials()
        self.assertEqual(os.environ['FEISHU_APP_ID'], 'existing')
        self.assertEqual(os.environ['FEISHU_APP_SECRET'], '')

    def test_missing_default_file_is_optional(self):
        self.assertIsNone(load_credentials())
        self.assertFalse(any(k in os.environ for k in CREDENTIAL_KEYS))

    def test_explicit_file_overrides_vfn_env_file_without_merging_default(self):
        self.env_file.write_text('FEISHU_APP_SECRET=default-secret\n', encoding='utf-8')
        selected = self.root / 'selected.env'
        selected.write_text('FEISHU_USER_ACCESS_TOKEN=selected-token\n', encoding='utf-8')
        os.environ['VFN_ENV_FILE'] = str(self.root / 'missing.env')
        # macOS symlinks and Windows short names can resolve to different spellings.
        self.assertTrue(load_credentials(selected).samefile(selected))
        self.assertEqual(os.environ['FEISHU_USER_ACCESS_TOKEN'], 'selected-token')
        self.assertNotIn('FEISHU_APP_SECRET', os.environ)

    def test_vfn_env_file_selects_file(self):
        selected = self.root / 'other.env'
        selected.write_text('FEISHU_TENANT_ACCESS_TOKEN=tenant-test\n', encoding='utf-8')
        os.environ['VFN_ENV_FILE'] = str(selected)
        self.assertTrue(load_credentials().samefile(selected))
        self.assertEqual(os.environ['FEISHU_TENANT_ACCESS_TOKEN'], 'tenant-test')

    def test_explicit_missing_file_and_directory_are_errors(self):
        for path in (self.root / 'missing.env', self.root):
            with self.subTest(path=path), self.assertRaisesRegex(ValueError, 'Credential file not found'):
                load_credentials(path)
        os.environ['VFN_ENV_FILE'] = str(self.root / 'missing.env')
        with self.assertRaises(ValueError):
            load_credentials()

    def test_default_file_is_independent_of_current_directory(self):
        other = self.root / 'other'
        other.mkdir()
        (other / '.env.local').write_text('FEISHU_APP_ID=wrong-file\n', encoding='utf-8')
        self.env_file.write_text('FEISHU_APP_ID=project-file\n', encoding='utf-8')
        original_cwd = Path.cwd()
        try:
            os.chdir(other)
            load_credentials()
        finally:
            os.chdir(original_cwd)
        self.assertEqual(os.environ['FEISHU_APP_ID'], 'project-file')


class CommandEntryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='vfn 中文 space ')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.env_file = self.root / 'credentials.env'
        self.secret = 'test-secret-do-not-print'
        self.env_file.write_text('FEISHU_APP_ID=test-app\nFEISHU_APP_SECRET=' + self.secret,
                                 encoding='utf-8')
        self.config = self.root / 'config.json'
        self.config.write_text(json.dumps({'app_token': 'base-test', 'table_id': 'table-test',
                                           'folder_token': 'folder-test'}), encoding='utf-8')
        self.env = {k: v for k, v in os.environ.items()
                    if k not in (*CREDENTIAL_KEYS, 'VFN_ENV_FILE', 'VFN_CONFIG')}
        self.env['PYTHONUTF8'] = '1'

    def run_command(self, entry, *args):
        return subprocess.run([str(p) for p in (*entry, *args)], cwd=self.root,
                              env=self.env, capture_output=True, encoding='utf-8', timeout=30)

    def test_original_script_loads_credentials_before_doctor_without_exposing_values(self):
        result = self.run_command([sys.executable, ROOT / 'scripts/vfn.py'],
                                  '--env-file', self.env_file, '--config', self.config, 'doctor')
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertTrue(report['data']['auth_configured'])
        self.assertTrue(report['data']['config_ready'])
        self.assertNotIn(self.secret, result.stdout + result.stderr)

    def test_missing_explicit_file_returns_json_error_and_nonzero_exit(self):
        result = self.run_command([sys.executable, ROOT / 'scripts/vfn.py'],
                                  '--env-file', self.root / 'missing.env',
                                  '--config', self.config, 'doctor')
        self.assertEqual(result.returncode, 1)
        self.assertFalse(json.loads(result.stdout)['ok'])
        self.assertIn('Credential file not found', result.stdout)

    def test_installed_short_command_preserves_paths_arguments_and_exit_codes(self):
        entry = Path(sys.executable).parent / ('vfn.exe' if os.name == 'nt' else 'vfn')
        if not entry.is_file():
            self.skipTest('Install the console entry with python -m pip install -e .')
        result = self.run_command([entry], '--env-file', self.env_file,
                                  '--config', self.config, 'doctor')
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertTrue(report['data']['auth_configured'])
        self.assertEqual(Path(report['data']['python_executable']).resolve(), Path(sys.executable).resolve())
        self.assertNotIn(self.secret, result.stdout + result.stderr)
        # A relative input is resolved from the caller's directory, including spaces and Unicode.
        note = self.root / 'note 中文.json'
        note.write_text((ROOT / 'examples/note.json').read_text(encoding='utf-8'), encoding='utf-8')
        result = self.run_command([entry], '--env-file', self.env_file, 'validate-note', note.name)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertTrue(json.loads(result.stdout)['data']['valid'])
        result = self.run_command([entry], '--env-file', self.env_file, 'validate-note', 'missing note 中文.json')
        self.assertEqual(result.returncode, 1)
        self.assertFalse(json.loads(result.stdout)['ok'])
        self.assertIn('missing note 中文.json', result.stdout)


if __name__ == '__main__':
    unittest.main()
