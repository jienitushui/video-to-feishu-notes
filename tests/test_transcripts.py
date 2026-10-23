import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))

from vfnlib.cli import parser, execute
from vfnlib.transcripts import (retain_transcript, finalize_transcript, verify_material_transcript,
                                inspect_text, correction_quality, read_json)
from vfnlib.transcription import run_chunked_transcription


class TranscriptTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.original = self.root / 'source.txt'
        self.text = '[00:00] NVIDIA 的研究\n[00:12] This is the original source sentence.\n'
        self.original.write_text(self.text, encoding='utf-8')
        self.source = {'key': 'bilibili:BV1:p1', 'duration': 30}
        retained = retain_transcript(self.original, self.root, self.source, 'chunked')
        self.manifest = self.root / 'manifest.json'
        self.material = dict(source=self.source, transcription={'status': 'prepared'}, **retained)
        self.manifest.write_text(json.dumps(self.material), encoding='utf-8')
        self.raw = read_json(self.root / 'transcripts/raw.segments.json')

    def corrections(self, segments):
        target = self.root / 'corrections.json'
        target.write_text(json.dumps({'source_key': self.source['key'], 'segments': segments}), encoding='utf-8')
        return target

    def test_finalize_and_prepare_keep_original_language_and_choose_correction(self):
        corrected = copy.deepcopy(self.raw)
        corrected[0]['text'] = 'NVIDIA 的相关研究'
        args = parser().parse_args(['video', 'finalize-transcript', '--manifest', str(self.manifest),
                                    '--corrections', str(self.corrections(corrected))])
        with patch('vfnlib.cli.Config.load', side_effect=AssertionError('Local correction needs no Feishu config')):
            result = execute(args)
        self.assertEqual((self.root / 'transcripts/raw.txt').read_text(encoding='utf-8'), self.text)
        self.assertEqual(read_json(self.root / 'transcripts/raw.segments.json'), self.raw)
        self.assertIn('This is the original', Path(result['transcript']).read_text(encoding='utf-8'))
        verify_material_transcript(result, self.root)
        refreshed = retain_transcript(self.original, self.root, self.source, 'chunked')
        self.assertEqual(refreshed['transcript'], result['transcript'])
        # Same correction is retryable, including a crash before manifest update.
        self.manifest.write_text(json.dumps(self.material), encoding='utf-8')
        repeated = finalize_transcript(self.manifest, self.root / 'corrections.json')
        verify_material_transcript(repeated, self.root)

    def test_invalid_corrections_do_not_change_active_or_raw_artifacts(self):
        before = self.manifest.read_bytes()
        for variant in ('count', 'timestamp', 'id', 'empty', 'retention', 'nan', 'multiline', 'not_list'):
            with self.subTest(variant=variant):
                corrected = copy.deepcopy(self.raw)
                if variant == 'count':
                    corrected.pop()
                elif variant == 'timestamp':
                    corrected[0]['start'] = 1
                elif variant == 'id':
                    corrected[0]['id'] = 99
                elif variant == 'empty':
                    corrected[0]['text'] = ''
                elif variant == 'retention':
                    for segment in corrected:
                        segment['text'] = 'x'
                elif variant == 'nan':
                    corrected[0]['start'] = float('nan')
                elif variant == 'multiline':
                    corrected[0]['text'] += '\n[00:01] injected paragraph'
                else:
                    corrected = None
                with self.assertRaisesRegex(ValueError, 'quality check failed'):
                    finalize_transcript(self.manifest, self.corrections(corrected))
                self.assertEqual(self.manifest.read_bytes(), before)
                self.assertEqual(self.original.read_text(encoding='utf-8'), self.text)
                self.assertFalse((self.root / 'transcripts/corrected.txt').exists())

    def test_retention_warning_is_distinct_from_failure(self):
        raw = [{'id': 1, 'start': 0, 'text': 'a' * 100}]
        report = correction_quality(raw, [dict(raw[0], text='a' * 60)])
        self.assertTrue(report['passed'])
        self.assertIn('character_retention_below_70_percent', report['warnings'])

    def test_modified_raw_and_corrected_are_detected(self):
        corrected = copy.deepcopy(self.raw)
        result = finalize_transcript(self.manifest, self.corrections(corrected))
        for name in ('raw.txt', 'raw.segments.json', 'corrected.txt', 'corrected.segments.json'):
            with self.subTest(name=name):
                file = self.root / 'transcripts' / name
                before = file.read_bytes()
                file.write_bytes(before + b' ')
                with self.assertRaisesRegex(ValueError, 'transcript changed'):
                    verify_material_transcript(result, self.root)
                file.write_bytes(before)

    def test_quality_rejects_malformed_empty_and_disordered_text(self):
        for text in ('', '[00:00] ', '[00:61] invalid', '[00:00] valid\nunlocated text',
                     '[00:10] later\n[00:01] earlier', '[00:31] out of range'):
            with self.subTest(text=text):
                self.assertFalse(inspect_text(text, 20)[1]['passed'])
        self.assertTrue(inspect_text('[01:00:00] English words', 4000)[1]['passed'])

    def test_changed_correction_requires_a_new_version(self):
        corrected = copy.deepcopy(self.raw)
        finalize_transcript(self.manifest, self.corrections(corrected))
        corrected[0]['text'] += ' revised'
        with self.assertRaisesRegex(ValueError, 'different correction already exists'):
            finalize_transcript(self.manifest, self.corrections(corrected))

    def test_partial_transcript_is_not_a_complete_material(self):
        self.material['transcription']['status'] = 'transcribing'
        with self.assertRaisesRegex(ValueError, 'still transcribing'):
            verify_material_transcript(self.material, self.root)


class ChunkQualityTests(unittest.TestCase):
    def test_all_silent_audio_is_not_marked_complete(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            audio = root / 'audio'
            audio.write_bytes(b'audio')
            with self.assertRaisesRegex(ValueError, 'quality check failed'):
                run_chunked_transcription(audio, root / 'result.txt', root, lambda *args: '', duration=10)
            self.assertFalse((root / 'result.txt.done').exists())
            self.assertEqual(read_json(root / 'chunk-manifest.json')['status'], 'quality_failed')

    def test_silent_chunk_can_be_resumed_with_speech_later(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            audio = root / 'audio'
            audio.write_bytes(b'audio')
            run_chunked_transcription(audio, root / 'out.txt', root, lambda *args: '',
                                       duration=20, chunk_seconds=10, max_chunks_per_run=1)
            result = run_chunked_transcription(audio, root / 'out.txt', root, lambda *args: '[00:01] speech',
                                              duration=20, chunk_seconds=10, max_chunks_per_run=1)
            self.assertEqual(result['status'], 'prepared')
            self.assertEqual(result['quality']['silent_chunk_candidates'], [0])

    def test_invalid_chunk_is_not_checkpointed(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            audio = root / 'audio'
            audio.write_bytes(b'audio')
            with self.assertRaisesRegex(ValueError, 'quality check failed'):
                run_chunked_transcription(audio, root / 'out.txt', root, lambda *args: '[00:02] text\nbroken', duration=20)
            self.assertFalse((root / 'chunk-checkpoints/chunk-000.json').exists())

    def test_missing_future_checkpoint_is_not_counted_as_completed(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            audio = root / 'audio'
            audio.write_bytes(b'audio')
            args = (audio, root / 'out.txt', root, lambda *args: '[00:01] text')
            run_chunked_transcription(*args, duration=30, chunk_seconds=10, max_chunks_per_run=0)
            (root / 'chunk-checkpoints/chunk-000.json').unlink()
            (root / 'chunk-checkpoints/chunk-002.json').unlink()
            result = run_chunked_transcription(*args, duration=30, chunk_seconds=10, max_chunks_per_run=1)
            self.assertEqual(result['status'], 'transcribing')
            self.assertEqual(result['remaining'], 1)
