import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from vfnlib.config import Config
from vfnlib.video import prepare, UPSTREAM_REVISION, safe_part
from vfnlib.cli import parser
from vfnlib.transcripts import finalize_transcript


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

    def test_prepare_reuses_correction_and_passes_it_to_screenshot_selection(self):
        with patch('vfnlib.video.run', side_effect=self.runner):
            manifest = prepare(self.config, self.url, self.out)
            segments = json.loads((self.out / 'transcripts/raw.segments.json').read_text(encoding='utf-8'))
            segments[0]['text'] = 'Corrected existing subtitle'
            correction = self.out / 'corrections.json'
            correction.write_text(json.dumps({'source_key': manifest['source']['key'], 'segments': segments}), encoding='utf-8')
            finalized = finalize_transcript(manifest['manifest'], correction)
            with patch('vfnlib.video.run', side_effect=self.runner) as run:
                refreshed = prepare(self.config, self.url, self.out)
            self.assertEqual(refreshed['transcript'], finalized['transcript'])
            screenshots = [c.args[0] for c in run.call_args_list if Path(c.args[0][1]).name == 'screenshots.py']
            self.assertEqual(screenshots[0][-2], finalized['transcript'])

    def test_chunk_completion_recovers_after_material_manifest_commit_was_interrupted(self):
        with patch('vfnlib.video.run', side_effect=self.runner):
            manifest = prepare(self.config, self.url, self.out, screenshots=False)
            manifest['transcription'] = {'mode': 'chunked', 'status': 'transcribing'}
            (self.out / 'manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
            (self.out / 'chunk-manifest.json').write_text(json.dumps({'status': 'prepared', 'completed': [0], 'remaining': 0}))
            refreshed = prepare(self.config, self.url, self.out, screenshots=False)
            self.assertIsNotNone(refreshed['transcript'])
            self.assertEqual(refreshed['transcription']['status'], 'prepared')

    def test_language_settings_apply_to_short_video_and_invalid_settings_stop_early(self):
        def asr(meta, page, out, script, **kwargs):
            target = out / ('02_' + safe_part('a' * 80) + '.txt')
            target.write_text('[00:00] Hello NVIDIA', encoding='utf-8')
            self.assertEqual(kwargs['language'], 'en')
            self.assertEqual(kwargs['hotwords'], ['GPU', 'NVIDIA'])
            self.assertTrue(kwargs['whole_file'])
            return {'status': 'prepared'}

        def runner(command, cwd=None):
            result = self.runner(command, cwd)
            if 'bili_one.py' in str(command[1]):
                next(self.out.glob('*.txt')).unlink()
            return result

        self.config.data.update(asr_hotwords=['GPU'], asr_language='zh')
        with patch('vfnlib.video.run', side_effect=runner), \
                patch('vfnlib.video.transcribe_page_chunked', side_effect=asr):
            material = prepare(self.config, self.url, self.out, transcribe=True,
                               asr_language='en', hotwords=['NVIDIA'], screenshots=False)
        self.assertTrue(material['transcript_quality']['passed'])
        with patch('vfnlib.video.run', side_effect=AssertionError('No external work')):
            with self.assertRaisesRegex(ValueError, 'ASR language'):
                prepare(self.config, self.url, self.out, asr_language='zh-CN')

    def test_output_directory_cannot_be_reused_for_another_video(self):
        with patch('vfnlib.video.run', side_effect=self.runner):
            prepare(self.config, self.url, self.out)
            with self.assertRaisesRegex(ValueError, 'different input'):
                prepare(self.config, self.url.replace('p=2', 'p=1'), self.out)

    def test_success_exit_without_transcript_is_failure_when_asr_requested(self):
        with patch('vfnlib.video.run', side_effect=self.runner):
            manifest = prepare(self.config, self.url, self.out)
            next(self.out.glob('*.txt')).unlink()
            with patch('vfnlib.video.transcribe_page_chunked', return_value={'status': 'prepared'}), \
                    self.assertRaisesRegex(RuntimeError, 'usable transcript'):
                prepare(self.config, self.url, self.out, transcribe=True)

    def test_default_screenshots_do_not_require_a_transcript(self):
        def runner(command, cwd=None):
            result = self.runner(command, cwd)
            if 'bili_one.py' in str(command[1]):
                next(self.out.glob('*.txt')).unlink()
            if Path(command[1]).name == 'screenshots.py':
                image_dir = self.out / 'images'
                image_dir.mkdir(exist_ok=True)
                (image_dir / 'manifest.json').write_text(json.dumps({'frames': [{'t': 10, 'qc': 'ok'}]}))
            return result

        with patch('vfnlib.video.run', side_effect=runner):
            manifest = prepare(self.config, self.url, self.out)
        self.assertIsNone(manifest['transcript'])
        self.assertEqual(manifest['screenshot_status'], 'review_required')
        self.assertIsNotNone(manifest['image_manifest'])

    def test_screenshot_failure_is_reported_in_material_manifest(self):
        def runner(command, cwd=None):
            if Path(command[1]).name == 'screenshots.py':
                raise RuntimeError('decoder unavailable')
            return self.runner(command, cwd)

        with patch('vfnlib.video.run', side_effect=runner):
            manifest = prepare(self.config, self.url, self.out)
        self.assertEqual(manifest['screenshot_status'], 'unavailable')
        self.assertIsNone(manifest['image_manifest'])
        self.assertTrue(any('decoder unavailable' in warning for warning in manifest['warnings']))

    def test_source_lookup_can_skip_screenshots_explicitly(self):
        with patch('vfnlib.video.run', side_effect=self.runner) as run:
            manifest = prepare(self.config, self.url, self.out, screenshots=False)
        self.assertEqual(manifest['screenshot_status'], 'not_requested')
        self.assertFalse(any(Path(call.args[0][1]).name == 'screenshots.py' for call in run.call_args_list))
        self.assertTrue(parser().parse_args(['video', 'prepare', self.url, '--output', 'out']).screenshots)
        self.assertFalse(parser().parse_args(['video', 'prepare', self.url, '--output', 'out', '--no-screenshots']).screenshots)


if __name__ == '__main__':
    unittest.main()
