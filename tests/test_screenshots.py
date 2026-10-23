import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from vfnlib.screenshots import extract


class ScreenshotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.meta = self.root / 'metadata.json'
        self.meta.write_text(json.dumps({'pages': [{'page': 1, 'duration': 80, 'media_path': 'video.mp4'}]}))
        (self.root / 'video.mp4').write_bytes(b'fixture')
        self.helpers = {'parse_ts_seconds': Mock(return_value=[]), 'auto_anchors': Mock(return_value=[]),
                        'qc_check': Mock(return_value='ok')}

    def test_no_transcript_uses_timeline_and_records_actual_frame_times(self):
        image = Mock()
        with patch('vfnlib.screenshots.runpy.run_path', return_value=self.helpers), patch(
                'vfnlib.screenshots.capture', return_value=[(image, 12.04), None, (image, 68.08)]) as capture:
            result = extract('upstream.py', self.meta, self.root / 'missing.txt', self.root / 'images')
        self.assertEqual(capture.call_args.args[1], [12, 40, 68])
        self.assertEqual([f['t'] for f in result['frames']], [12.04, 68.08])
        self.assertEqual(result['failed'][0]['t'], 40)
        self.assertEqual(result['method'], 'timeline')
        self.assertTrue(all(f['review_required'] for f in result['frames']))
        self.assertEqual(json.loads((self.root / 'images/manifest.json').read_text(encoding='utf-8')), result)
        self.assertTrue((self.root / 'video.mp4').exists())

    def test_transcript_without_keywords_still_produces_candidates(self):
        transcript = self.root / 'transcript.txt'
        transcript.write_text('[00:01] A short sentence')
        with patch('vfnlib.screenshots.runpy.run_path', return_value=self.helpers), patch(
                'vfnlib.screenshots.capture', return_value=[(Mock(), 12), (Mock(), 40), (Mock(), 68)]):
            result = extract('upstream.py', self.meta, transcript, self.root / 'images')
        self.assertEqual(len(result['frames']), 3)
        self.helpers['parse_ts_seconds'].assert_called_once()

    def test_transcript_anchors_and_qc_retry_keep_actual_time(self):
        self.helpers['auto_anchors'].return_value = [('topic', 'topic', 'caption', 10)]
        self.helpers['qc_check'].side_effect = ['dark', 'ok']
        with patch('vfnlib.screenshots.runpy.run_path', return_value=self.helpers), patch(
                'vfnlib.screenshots.capture', side_effect=[[(Mock(), 10.04)], [(Mock(), 14.08)]]):
            result = extract('upstream.py', self.meta, self.root / 'missing.txt', self.root / 'images')
        self.assertEqual(result['method'], 'transcript_anchors')
        self.assertEqual(result['frames'][0]['t'], 14.08)
        self.assertEqual(result['frames'][0]['qc'], 'ok')


if __name__ == '__main__':
    unittest.main()
