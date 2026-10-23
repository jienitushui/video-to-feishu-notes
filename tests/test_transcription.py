import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))

from vfnlib.transcription import run_chunked_transcription, transcribe_page_chunked


class ChunkedTranscriptionTests(unittest.TestCase):
    def test_checkpoints_resume_and_merge_global_timeline(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            audio = root / 'audio.bin'
            audio.write_bytes(b'audio')
            output = root / 'transcript.txt'
            calls = []

            def transcribe(start, end, index):
                calls.append(index)
                return f'[00:00] sentence-{index}\n'

            first = run_chunked_transcription(audio, output, root, transcribe,
                                               duration=650, max_chunks_per_run=2)
            self.assertEqual(first['status'], 'transcribing')
            self.assertEqual(first['completed'], 2)
            self.assertEqual(calls, [0, 1])
            self.assertEqual(len(list((root / 'chunk-checkpoints').glob('*.json'))), 2)

            calls.clear()
            second = run_chunked_transcription(audio, output, root, transcribe,
                                                duration=650, max_chunks_per_run=2)
            self.assertEqual(second['status'], 'prepared')
            self.assertEqual(second['completed'], 3)
            self.assertEqual(calls, [2])
            self.assertEqual(output.read_text(encoding='utf-8').count('sentence-'), 3)
            self.assertTrue((root / 'transcript.txt.done').exists())
            manifest = json.loads((root / 'chunk-manifest.json').read_text(encoding='utf-8'))
            self.assertEqual(manifest['remaining'], 0)

    def test_changed_audio_or_plan_does_not_reuse_checkpoints(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            audio = root / 'audio.bin'
            audio.write_bytes(b'audio')
            output = root / 'transcript.txt'
            run_chunked_transcription(audio, output, root, lambda s, e, i: '[00:00] x',
                                       duration=10, chunk_seconds=5, max_chunks_per_run=1)
            with self.assertRaisesRegex(ValueError, 'different audio or chunk plan'):
                run_chunked_transcription(audio, output, root, lambda s, e, i: '[00:00] x',
                                           duration=10, chunk_seconds=10, max_chunks_per_run=0)

    def test_changed_duration_does_not_reuse_checkpoints(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            audio = root / 'audio.bin'
            audio.write_bytes(b'audio')
            output = root / 'transcript.txt'
            run_chunked_transcription(audio, output, root, lambda s, e, i: '[00:00] x',
                                       duration=10, chunk_seconds=5, max_chunks_per_run=1,
                                       audio_identity='video:p1')
            with self.assertRaisesRegex(ValueError, 'different audio or chunk plan'):
                run_chunked_transcription(audio, output, root, lambda s, e, i: '[00:00] x',
                                           duration=11, chunk_seconds=5, max_chunks_per_run=1,
                                           audio_identity='video:p1')

    def test_stale_checkpoint_window_is_retranscribed(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            audio = root / 'audio.bin'
            audio.write_bytes(b'audio')
            output = root / 'transcript.txt'
            run_chunked_transcription(audio, output, root, lambda s, e, i: '[00:00] old',
                                       duration=10, chunk_seconds=5, max_chunks_per_run=1)
            checkpoint = root / 'chunk-checkpoints/chunk-000.json'
            payload = json.loads(checkpoint.read_text(encoding='utf-8'))
            payload['core_end'] = 999
            checkpoint.write_text(json.dumps(payload), encoding='utf-8')

            calls = []
            result = run_chunked_transcription(
                audio, output, root,
                lambda s, e, i: calls.append(i) or '[00:00] repaired',
                duration=10, chunk_seconds=5, max_chunks_per_run=1)
            self.assertEqual(calls, [0])
            self.assertEqual(result['completed'], 1)
            self.assertIn('repaired', output.read_text(encoding='utf-8'))
            self.assertNotIn('old', output.read_text(encoding='utf-8'))

    def test_partial_run_reuses_audio_and_removes_it_after_completion(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            meta = root / 'metadata.json'
            meta.write_text(json.dumps({'bvid': 'BV123', 'pages': [
                {'page': 1, 'part': 'Lecture', 'cid': 10, 'duration': 600}
            ]}), encoding='utf-8')
            calls = {'download': 0, 'transcribe': 0}
            fake_upstream = types.SimpleNamespace(
                load_sessdata=lambda: None,
                download_audio=lambda bvid, cid, path, cookie: (calls.__setitem__('download', calls['download'] + 1),
                                                                  Path(path).write_bytes(b'audio')),
                _cfg=lambda key, default: default,
                load_model=lambda model, device: {},
                transcribe=lambda wav, model, language, device, terms=None:
                    (calls.__setitem__('transcribe', calls['transcribe'] + 1) or '[00:00] speech'),
                safe_name=lambda value: value,
            )
            with patch.dict(sys.modules, {'av': types.ModuleType('av')}), \
                    patch('vfnlib.transcription._load_upstream', return_value=fake_upstream), \
                    patch('vfnlib.transcription._window_wav'):
                first = transcribe_page_chunked(meta, 1, root, root / 'bili_transcribe.py',
                                                max_chunks_per_run=1)
                audio = root / '.chunk-audio-01.m4s'
                self.assertEqual(first['status'], 'transcribing')
                self.assertTrue(audio.exists())
                second = transcribe_page_chunked(meta, 1, root, root / 'bili_transcribe.py',
                                                 max_chunks_per_run=1)
                self.assertEqual(second['status'], 'prepared')
                self.assertFalse(audio.exists())
            self.assertEqual(calls['download'], 1)
            self.assertEqual(calls['transcribe'], 2)

    def test_language_terms_reach_asr_and_changed_settings_cannot_resume(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / 'media.mp4').write_bytes(b'audio')
            meta = root / 'metadata.json'
            meta.write_text(json.dumps({'bvid': 'BV123', 'title': 'Interview', 'owner': {'name': 'Alice'},
                                        'pages': [{'page': 1, 'part': 'AI', 'duration': 600, 'media_path': 'media.mp4'}]}), encoding='utf-8')
            calls = []
            module = types.SimpleNamespace(
                _cfg=lambda key, default: default, load_model=lambda *args: {},
                transcribe=lambda audio, model, language, device, terms=None:
                    calls.append((language, terms)) or '[00:00] English speech', safe_name=lambda v: v)
            with patch.dict(sys.modules, {'av': types.ModuleType('av')}), \
                    patch('vfnlib.transcription._load_upstream', return_value=module), \
                    patch('vfnlib.transcription._window_wav'):
                transcribe_page_chunked(meta, 1, root, root / 'script', max_chunks_per_run=1,
                                         language='auto', hotwords=['CUDA'])
                self.assertIsNone(calls[0][0])
                self.assertEqual(calls[0][1], ['CUDA', 'Interview', 'Alice', 'AI'])
                with self.assertRaisesRegex(ValueError, 'ASR settings changed'):
                    transcribe_page_chunked(meta, 1, root, root / 'script', language='en', hotwords=['CUDA'])
                self.assertEqual(len(calls), 1)
                transcribe_page_chunked(meta, 1, root, root / 'script', language='auto', hotwords=['CUDA'])
                self.assertEqual(calls[1], calls[0])

    def test_whole_file_fallback_preserves_explicit_language_and_hotwords(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / 'media.mp4').write_bytes(b'audio')
            meta = root / 'metadata.json'
            meta.write_text(json.dumps({'video_id': '123', 'pages': [
                {'page': 1, 'part': 'Interview', 'duration': 30, 'media_path': 'media.mp4'}]}), encoding='utf-8')
            calls = []
            module = types.SimpleNamespace(
                _cfg=lambda key, default: default, load_model=lambda *args: {}, safe_name=lambda v: v,
                transcribe=lambda audio, model, language, device, terms=None:
                    calls.append((audio, language, terms)) or '[00:00] English speech')
            with patch('vfnlib.transcription._load_upstream', return_value=module), \
                    patch('vfnlib.transcription._window_wav', side_effect=AssertionError('No slicing')):
                result = transcribe_page_chunked(meta, 1, root, root / 'script', language='en',
                                                 hotwords=['NVIDIA'], whole_file=True)
            self.assertEqual(result['status'], 'prepared')
            self.assertEqual(calls, [(root / 'media.mp4', 'en', ['NVIDIA', 'Interview'])])


if __name__ == '__main__':
    unittest.main()
