import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))

from vfnlib.transcription import run_chunked_transcription


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


if __name__ == '__main__':
    unittest.main()
