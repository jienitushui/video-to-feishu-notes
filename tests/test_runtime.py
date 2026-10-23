import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from vfnlib.config import Config
from vfnlib import runtime
from vfnlib.transcription import ChunkedTranscriptionUnavailable


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.config = Config({'media_runtime_path': str(self.root / 'runtime')}, self.root / 'config.json')
        env = patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        for name in ('VFN_MEDIA_RUNTIME', 'VFN_MEDIA_PYTHON', 'VFN_DISABLE_RUNTIME_RECOVERY'):
            os.environ.pop(name, None)
        self.bad = {'ready': False, 'checked_runtimes': []}

    def test_healthy_runtime_is_reused_without_installing(self):
        healthy = {'ready': True, 'python': sys.executable}
        with patch.object(runtime, 'inspect_runtime', return_value=healthy), patch.object(runtime, '_step') as step:
            self.assertEqual(runtime.ensure_runtime(self.config), healthy)
        step.assert_not_called()
        self.assertFalse((self.root / 'runtime').exists())

    def test_explicit_unavailable_and_opt_out_do_not_install(self):
        with patch.object(runtime, 'inspect_runtime', return_value=self.bad), patch.object(runtime, '_step') as step:
            self.config.data['media_python'] = 'missing.exe'
            with self.assertRaisesRegex(runtime.RuntimeRecoveryError, 'Configured'):
                runtime.ensure_runtime(self.config)
            del self.config.data['media_python']
            self.config.data['auto_recover_runtime'] = False
            with self.assertRaisesRegex(runtime.RuntimeRecoveryError, 'disabled'):
                runtime.ensure_runtime(self.config)
        step.assert_not_called()

    def test_managed_pointer_and_explicit_precedence(self):
        root = runtime.runtime_root(self.config)
        root.mkdir()
        (root / 'active.json').write_text(json.dumps({'directory': 'env-good'}))
        self.assertIn(runtime.python_in(root / 'env-good'), runtime.candidates(self.config))
        self.config.data['media_python'] = 'custom/python.exe'
        self.assertEqual(runtime.candidates(self.config), [(self.root / 'custom/python.exe').resolve()])

    def test_python_probe_preserves_virtual_environment_executable_path(self):
        virtual = self.root / 'virtual/bin/python'
        with patch.object(runtime.Path, 'is_file', return_value=True), \
             patch.object(runtime.Path, 'resolve', side_effect=AssertionError('Do not follow venv symlinks')), \
             patch.object(runtime, 'run_capture', return_value=SimpleNamespace(returncode=0, stdout=json.dumps(
                 {'ready': True, 'python': str(virtual)}))) as run:
            self.assertTrue(runtime.check_python(virtual)['ready'])
            self.assertEqual(run.call_args.args[0][0], virtual)

    def test_success_switches_pointer_only_after_verification(self):
        def step(command, label, **kwargs):
            if label == 'venv':
                Path(command[-1]).mkdir()
                self.build = Path(command[-1])
                self.assertFalse((self.build.parent / 'active.json').exists())
        def check(python):
            self.assertEqual(Path(python), runtime.python_in(self.build))
            return {'ready': True, 'python': str(python), 'versions': {'av': '12.3.0'}}
        with patch.object(runtime, 'inspect_runtime', return_value=self.bad), \
             patch.object(runtime, 'recovery_python', return_value=sys.executable), \
             patch.object(runtime.shutil, 'disk_usage', return_value=SimpleNamespace(free=2**32)), \
             patch.object(runtime, '_step', side_effect=step), patch.object(runtime, 'check_python', side_effect=check):
            result = runtime.ensure_runtime(self.config)
        self.assertTrue(result['recovered'])
        self.assertEqual(json.loads((self.build.parent / 'active.json').read_text())['directory'], self.build.name)
        self.assertFalse((self.build.parent / 'install.lock').exists())

    def test_failed_install_preserves_prior_environment_pointer_and_task(self):
        root = runtime.runtime_root(self.config)
        old = root / 'env-old'
        old.mkdir(parents=True)
        pointer = root / 'active.json'
        pointer.write_text('{"directory":"env-old"}')
        task = self.root / 'checkpoint.json'
        task.write_text('checkpoint')
        def step(command, label, **kwargs):
            if label == 'venv':
                Path(command[-1]).mkdir()
            else:
                raise runtime.RuntimeRecoveryError('fixture pip failure')
        with patch.object(runtime, 'inspect_runtime', return_value=self.bad), \
             patch.object(runtime, 'recovery_python', return_value=sys.executable), \
             patch.object(runtime.shutil, 'disk_usage', return_value=SimpleNamespace(free=2**32)), \
             patch.object(runtime, '_step', side_effect=step):
            with self.assertRaisesRegex(runtime.RuntimeRecoveryError, 'pip failure'):
                runtime.ensure_runtime(self.config)
        self.assertEqual(pointer.read_text(), '{"directory":"env-old"}')
        self.assertEqual(list(root.glob('env-*')), [old])
        self.assertEqual(task.read_text(), 'checkpoint')
        self.assertFalse((root / 'install.lock').exists())

    def test_other_installer_lock_is_not_removed(self):
        root = runtime.runtime_root(self.config)
        root.mkdir()
        lock = root / 'install.lock'
        lock.write_text('other PID')
        with patch.object(runtime, 'inspect_runtime', return_value=self.bad):
            with self.assertRaisesRegex(runtime.RuntimeRecoveryError, 'lock exists'):
                runtime.ensure_runtime(self.config)
        self.assertEqual(lock.read_text(), 'other PID')

    def test_pip_retry_is_bounded(self):
        failed = SimpleNamespace(returncode=1, stderr='network failure', stdout='')
        with patch.object(runtime, 'run_capture', return_value=failed) as run:
            with self.assertRaisesRegex(runtime.RuntimeRecoveryError, 'network failure'):
                runtime._step(['pip'], 'pip', attempts=2)
        self.assertEqual(run.call_count, 2)

    def test_insufficient_space_stops_before_creation_and_removes_own_lock(self):
        with patch.object(runtime, 'inspect_runtime', return_value=self.bad), \
             patch.object(runtime.shutil, 'disk_usage', return_value=SimpleNamespace(free=100)), \
             patch.object(runtime, '_step') as step:
            with self.assertRaisesRegex(runtime.RuntimeRecoveryError, 'Less than 1 GiB'):
                runtime.ensure_runtime(self.config)
        step.assert_not_called()
        self.assertFalse((runtime.runtime_root(self.config) / 'install.lock').exists())

    def test_import_failure_is_diagnosed_and_language_check_allows_recovery(self):
        from vfnlib.transcription import normalize_language
        with patch.object(runtime, 'run_capture', return_value=SimpleNamespace(returncode=1, stdout='', stderr='DLL load failed')):
            report = runtime.check_python(sys.executable)
        self.assertFalse(report['ready'])
        self.assertIn('DLL load failed', report['error'])
        original = __import__
        def broken(name, *args, **kwargs):
            if name == 'faster_whisper.tokenizer':
                raise OSError('DLL load failed')
            return original(name, *args, **kwargs)
        with patch('builtins.__import__', side_effect=broken):
            self.assertEqual(normalize_language('zh'), 'zh')

    def test_python_discovery_skips_incompatible_interpreter(self):
        def probe(command, **kwargs):
            return SimpleNamespace(returncode=0 if str(command[0]) == 'compatible-python' else 1, stdout='')
        with patch.object(runtime.shutil, 'which', side_effect=lambda name: 'compatible-python' if name == 'python3.12' else None), \
             patch.object(runtime, 'run_capture', side_effect=probe):
            self.assertEqual(runtime.recovery_python(), 'compatible-python')

    def test_worker_retains_paths_options_and_surfaces_errors_without_rebuilding(self):
        python = self.root / 'isolated/python.exe'
        output = self.root / '任务 空格'
        report = {'ready': True, 'python': str(python), 'recovered': True}
        with patch.object(runtime, 'ensure_runtime', return_value=report) as ensure, \
             patch.object(runtime.subprocess, 'run', return_value=SimpleNamespace(returncode=0, stdout='{"ok":true,"data":{"status":"transcribing"}}')) as run:
            result = runtime.transcribe_with_runtime(self.config, self.root / '元数据.json', 2, output,
                                                      'upstream.py', max_chunks_per_run=1, hotwords=['中文'])
            command = run.call_args.args[0]
            self.assertIn(str(output), command)
            self.assertEqual(json.loads(command[-1])['hotwords'], ['中文'])
            self.assertEqual(result['status'], 'transcribing')
            self.assertEqual(ensure.call_count, 1)
        with patch.object(runtime, 'ensure_runtime', return_value=report) as ensure, \
             patch.object(runtime.subprocess, 'run', return_value=SimpleNamespace(returncode=1, stdout='{"ok":false,"error":"model network failure"}')):
            with self.assertRaisesRegex(runtime.RuntimeRecoveryError, 'model network failure'):
                runtime.transcribe_with_runtime(self.config, 'meta', 1, output, 'upstream.py')
            self.assertEqual(ensure.call_count, 1)

    def test_local_asr_error_is_not_replayed(self):
        with patch.object(runtime, 'ensure_runtime', return_value={'python': sys.executable}) as ensure, \
             patch('vfnlib.transcription.transcribe_page_chunked', side_effect=ValueError('bad audio')) as asr:
            with self.assertRaisesRegex(ValueError, 'bad audio'):
                runtime.transcribe_with_runtime(self.config, 'meta', 1, self.root, 'upstream.py')
            self.assertEqual(ensure.call_count, 1)
            self.assertEqual(asr.call_count, 1)

    def test_worker_preserves_chunk_unavailable_error_for_existing_fallback(self):
        with patch.object(runtime, 'ensure_runtime', return_value={'python': str(self.root / 'python.exe')}), \
             patch.object(runtime.subprocess, 'run', return_value=SimpleNamespace(returncode=1, stdout=json.dumps(
                 {'ok': False, 'error_type': 'ChunkedTranscriptionUnavailable', 'error': 'no audio'}))):
            with self.assertRaisesRegex(ChunkedTranscriptionUnavailable, 'no audio'):
                runtime.transcribe_with_runtime(self.config, 'meta', 1, self.root, 'upstream.py')

    def test_worker_real_process_resumes_existing_checkpoints_with_unicode_paths(self):
        output = self.root / '任务 空格'
        output.mkdir()
        (output / 'audio.bin').write_bytes(b'audio')
        upstream = self.root / 'fake_upstream.py'
        upstream.write_text('''def _cfg(key, default): return default
def safe_name(part): return part
def load_model(*args): return {}
def transcribe(*args, **kwargs):
    from pathlib import Path
    log = Path(__file__).with_name('recognition-calls.txt')
    with log.open('a') as f: f.write('call\\n')
    return '[00:00] original text'
''', encoding='utf-8')
        meta = output / 'metadata.json'
        meta.write_text(json.dumps({'bvid': 'fixture', 'title': '中文', 'pages': [
            {'page': 1, 'part': 'part', 'duration': 10, 'media_path': 'audio.bin'}]}), encoding='utf-8')
        command = [sys.executable, str(Path(runtime.__file__).with_name('runtime_worker.py')),
                   '--metadata', str(meta), '--page', '1', '--output', str(output),
                   '--upstream-script', str(upstream), '--options', '{"whole_file":true}']
        for _ in range(2):
            result = subprocess.run(command, capture_output=True, encoding='utf-8', timeout=20)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(json.loads(result.stdout)['data']['status'], 'prepared')
        self.assertEqual((self.root / 'recognition-calls.txt').read_text(), 'call\n')


if __name__ == '__main__':
    unittest.main()
