import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from vfnlib.cli import execute, parser
from vfnlib.reading_index import refresh_reading_index
from vfnlib.transcripts import retain_transcript, finalize_transcript, read_json


class ReadingIndexTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.original = self.root / 'source.txt'
        self.original.write_text('[00:00] ' + '甲' * 7000 + '\n[00:01] ' + '乙' * 7000 +
                                 '\n[10:00] boundary\n[30:00] end\n', encoding='utf-8')
        self.source = {'key': 'bilibili:BV1:p1', 'title': '长视频', 'url': 'https://example.com', 'duration': 1900}
        self.material = dict(source=self.source, transcription={'status': 'prepared'},
                             **retain_transcript(self.original, self.root, self.source, 'chunked'))
        self.manifest = self.root / 'manifest.json'
        self.manifest.write_text(json.dumps(self.material), encoding='utf-8')

    def test_complete_ordered_segments_boundaries_and_sparse_windows(self):
        index = read_json(self.material['reading_index'])
        self.assertEqual([i for p in index['parts'] for i in p['segment_ids']], [1, 2, 3, 4])
        self.assertEqual(len(index['parts']), 4)
        self.assertEqual(index['parts'][2]['window_id'], 'window-0002')
        self.assertEqual(index['no_segment_start_windows'], ['window-0003'])
        directory = Path(self.material['reading_index']).parent
        for part in index['parts']:
            content = (directory / part['file']).read_text(encoding='utf-8')
            self.assertIn('来源标识', content)
            self.assertIn(index['transcript_sha256'], content)
        self.assertEqual(read_json(self.material['reading_progress'])['read_count'], 0)

    def test_cli_marks_only_requested_parts_and_rebuild_preserves_progress(self):
        args = parser().parse_args(['video', 'reading-index', '--manifest', str(self.manifest),
                                    '--mark-read', 'part-0001', 'part-0001'])
        with patch('vfnlib.cli.Config.load', side_effect=AssertionError('Local only')):
            result = execute(args)
        self.assertEqual(result['reading_coverage']['read_count'], 1)
        self.assertEqual(refresh_reading_index(self.manifest)['reading_coverage']['read_count'], 1)
        before = self.manifest.read_bytes()
        with self.assertRaisesRegex(ValueError, 'Unknown reading part'):
            refresh_reading_index(self.manifest, ['part-9999'])
        self.assertEqual(self.manifest.read_bytes(), before)

    def test_corrected_transcript_resets_progress_and_retains_original_index(self):
        refresh_reading_index(self.manifest, ['part-0001'])
        segments = copy.deepcopy(read_json(self.root / 'transcripts/raw.segments.json'))
        segments[-1]['text'] = 'corrected end'
        correction = self.root / 'corrections.json'
        correction.write_text(json.dumps({'source_key': self.source['key'], 'segments': segments}), encoding='utf-8')
        updated = finalize_transcript(self.manifest, correction)
        self.assertNotEqual(updated['reading_index'], self.material['reading_index'])
        self.assertEqual(updated['reading_coverage']['read_count'], 0)
        self.assertEqual(read_json(self.material['reading_progress'])['read_count'], 1)
        self.assertEqual(refresh_reading_index(self.manifest)['reading_index'], updated['reading_index'])

    def test_partial_or_tampered_transcript_cannot_gain_reading_index(self):
        material = dict(self.material, transcription={'status': 'transcribing'})
        self.manifest.write_text(json.dumps(material), encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'still transcribing'):
            refresh_reading_index(self.manifest)
        self.manifest.write_text(json.dumps(self.material), encoding='utf-8')
        Path(self.material['transcript']).write_text('changed', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'transcript changed'):
            refresh_reading_index(self.manifest)

    def test_packet_never_truncates_an_oversized_original_paragraph(self):
        other = self.root / 'other'
        other.mkdir()
        original = other / 'text.txt'
        original.write_text('[00:00] ' + 'word ' * 4000, encoding='utf-8')
        result = retain_transcript(original, other, self.source, 'subtitle')
        index = read_json(result['reading_index'])
        self.assertEqual(len(index['parts']), 1)
        packet = Path(result['reading_index']).parent / index['parts'][0]['file']
        self.assertIn('word ' * 3999 + 'word', packet.read_text(encoding='utf-8'))


if __name__ == '__main__':
    unittest.main()
