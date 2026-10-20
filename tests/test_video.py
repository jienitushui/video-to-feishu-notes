import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from vfnlib.config import Config
from vfnlib.video import prepare, UPSTREAM_REVISION, safe_part


class VideoTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'upstream/scripts').mkdir(parents=True)
        (self.root / 'upstream/scripts/bili_fetch.py').write_text('# fixture', encoding='utf-8')
        self.config = Config({'upstream_path': str(self.root / 'upstream')})
        self.out = self.root / 'output'
        self.url = 'https://www.bilibili.com/video/BV1Wv411h7kN?p=2'

    def runner(self, command, cwd=None):
        if command[-2:] == ['rev-parse', 'HEAD']:
            return UPSTREAM_REVISION + '\n'
        if 'bili_one.py' in str(command[1]):
            self.assertEqual(command[-1], '2')
            meta = {'bvid': 'BV1Wv411h7kN', 'title': 'Lecture', 'duration': 20,
                    'pages': [{'page': 2, 'part': 'a' * 80, 'duration': 20, 'cid': 22}]}
            self.out.mkdir(exist_ok=True)
            (self.out / 'metadata.json').write_text(json.dumps(meta), encoding='utf-8')
            (self.out / ('02_' + safe_part('a' * 80) + '.txt')).write_text('[00:00] Existing subtitle', encoding='utf-8')
        return ''

    def test_selected_page_and_existing_subtitle_are_recognized(self):
        with patch('vfnlib.video.run', side_effect=self.runner):
            manifest = prepare(self.config, self.url, self.out)
        self.assertEqual(manifest['source']['key'], 'bilibili:BV1Wv411h7kN:p2')
        self.assertIsNotNone(manifest['transcript'])
        self.assertEqual(len(json.loads((self.out / 'selected-metadata.json').read_text())['pages']), 1)

    def test_output_directory_cannot_be_reused_for_another_video(self):
        with patch('vfnlib.video.run', side_effect=self.runner):
            prepare(self.config, self.url, self.out)
            with self.assertRaisesRegex(ValueError, 'different input'):
                prepare(self.config, self.url.replace('p=2', 'p=1'), self.out)

    def test_success_exit_without_transcript_is_failure_when_asr_requested(self):
        with patch('vfnlib.video.run', side_effect=self.runner):
            manifest = prepare(self.config, self.url, self.out)
            Path(manifest['transcript']).unlink()
            with self.assertRaisesRegex(RuntimeError, 'usable transcript'):
                prepare(self.config, self.url, self.out, transcribe=True)


if __name__ == '__main__':
    unittest.main()
