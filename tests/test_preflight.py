import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from vfnlib.config import Config
from vfnlib.cli import execute, parser
from vfnlib.preflight import disk_report, preflight
from vfnlib.probe_video import probe
from vfnlib.video import UPSTREAM_REVISION

BVID = 'BV1Wv411h7kN'


class PreflightTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.scripts = self.root / 'upstream/scripts'
        self.scripts.mkdir(parents=True)
        (self.scripts / 'bili_fetch.py').write_text('# fixture', encoding='utf-8')
        self.output = self.root / 'new/output'
        self.config = Config({'upstream_path': str(self.scripts.parent), 'output_root': str(self.output)})

    def test_cli_reports_all_durations_before_any_media_work(self):
        def runner(command):
            if command[-2:] == ['rev-parse', 'HEAD']:
                return UPSTREAM_REVISION
            self.assertEqual(Path(command[1]).name, 'probe_video.py')
            page = int(command[-1])
            return json.dumps({'title': 'Page ' + str(page), 'source_key': f'bilibili:{BVID}:p{page}',
                               'duration_seconds': 3600 * page})

        args = parser().parse_args(['video', 'preflight', BVID,
                                    f'https://www.bilibili.com/video/{BVID}?p=2'])
        with patch('vfnlib.cli.Config.load', return_value=self.config), \
                patch('vfnlib.preflight.run', side_effect=runner), \
                patch('vfnlib.preflight.shutil.disk_usage', return_value=SimpleNamespace(free=2 * 1024**3)), \
                patch('vfnlib.cli.Feishu', side_effect=AssertionError('No Feishu access')):
            result = execute(args)
        self.assertTrue(result['complete'])
        self.assertEqual(result['total_minutes'], 180)
        self.assertEqual([v['duration_seconds'] for v in result['videos']], [3600, 7200])
        self.assertEqual(result['disk']['free_gib'], 2)
        self.assertFalse(self.output.exists())

    def test_failures_preserve_other_videos_and_do_not_report_partial_sum_as_total(self):
        def runner(command):
            if command[-2:] == ['rev-parse', 'HEAD']:
                return UPSTREAM_REVISION
            if command[-1] == '2':
                raise RuntimeError('Metadata unavailable')
            return json.dumps({'title': 'known', 'source_key': f'bilibili:{BVID}:p1', 'duration_seconds': 90})

        with patch('vfnlib.preflight.run', side_effect=runner):
            result = preflight(self.config, [BVID, f'https://www.bilibili.com/video/{BVID}?p=2'])
        self.assertFalse(result['complete'])
        self.assertIsNone(result['total_seconds'])
        self.assertEqual(result['known_total_seconds'], 90)
        self.assertEqual(result['unknown_duration_count'], 1)
        self.assertIn('Metadata unavailable', result['videos'][1]['error'])

    def test_invalid_duration_is_unknown_and_low_disk_does_not_block(self):
        for value in (None, 0, -1, True, '120', float('inf'), float('nan')):
            with self.subTest(value=value), \
                    patch('vfnlib.preflight.run', side_effect=[UPSTREAM_REVISION, json.dumps(
                        {'title': 'unknown', 'source_key': 'key', 'duration_seconds': value})]), \
                    patch('vfnlib.preflight.shutil.disk_usage', return_value=SimpleNamespace(free=1)):
                result = preflight(self.config, [BVID])
                self.assertIsNone(result['total_seconds'])
                self.assertEqual(result['disk']['free_bytes'], 1)
                self.assertEqual(result['unknown_duration_count'], 1)

    def test_short_url_preserves_explicit_page_and_output_override(self):
        commands = []
        def runner(command):
            commands.append(command)
            return UPSTREAM_REVISION if command[-2:] == ['rev-parse', 'HEAD'] else json.dumps(
                {'title': 'page', 'source_key': 'key', 'duration_seconds': 10})
        response = Mock(url=f'https://www.bilibili.com/video/{BVID}?p=3')
        override = self.root / 'other/new'
        with patch('vfnlib.preflight.run', side_effect=runner), \
                patch('vfnlib.video.requests.get', return_value=response):
            result = preflight(self.config, ['https://b23.tv/example?p=2'], override)
        self.assertEqual(commands[-1][-1], '2')
        self.assertEqual(result['disk']['output_root'], str(override))
        self.assertFalse(override.exists())

    def test_disk_check_handles_missing_output_and_filesystem_error(self):
        with patch('vfnlib.preflight.shutil.disk_usage', side_effect=OSError('disk unavailable')) as usage:
            report = disk_report(self.output)
        usage.assert_called_once_with(self.root)
        self.assertIsNone(report['free_bytes'])
        self.assertIn('disk unavailable', report['error'])
        self.assertFalse(self.output.exists())

    def test_missing_upstream_still_reports_disk_and_unknown_videos(self):
        (self.scripts / 'bili_fetch.py').unlink()
        with patch('vfnlib.preflight.run', side_effect=AssertionError('No upstream calls')):
            result = preflight(self.config, [BVID])
        self.assertIn('not installed', result['videos'][0]['error'])
        self.assertIsNotNone(result['disk']['free_bytes'])


class MetadataOnlyTests(unittest.TestCase):
    def test_bilibili_reads_only_metadata_and_selected_page(self):
        data = {'bvid': BVID, 'title': 'Collection', 'pages': [
            {'page': 1, 'part': 'First', 'duration': 10}, {'page': 2, 'part': 'Second', 'duration': 25}]}
        api = Mock(return_value={'data': data})
        ns = {'api': api, 'extract_bvid': lambda source: BVID,
              'main': Mock(side_effect=AssertionError('No fetch main'))}
        with patch('vfnlib.probe_video.runpy.run_path', return_value=ns):
            report = probe('upstream/scripts', 'bilibili', BVID, 2)
        self.assertEqual(report['duration_seconds'], 25)
        self.assertEqual(report['title'], 'Collection / Second')
        api.assert_called_once_with('https://api.bilibili.com/x/web-interface/view?bvid=' + BVID)

    def test_bilibili_metadata_fallback_is_shared_with_prepare(self):
        ns = {'extract_bvid': lambda source: BVID, 'api': Mock(side_effect=RuntimeError('412')),
              'http_get': Mock()}
        with patch('vfnlib.probe_video.runpy.run_path', return_value=ns), \
                patch('vfnlib.probe_video.fallback_view', return_value={'data': {
                    'bvid': BVID, 'title': 'Video', 'pages': [{'page': 1, 'duration': 50}]}}) as fallback:
            report = probe('scripts', 'bilibili', BVID, 1)
        self.assertEqual(report['duration_seconds'], 50)
        self.assertEqual(fallback.call_count, 1)

    def test_douyin_reads_detail_without_download_or_main(self):
        with tempfile.TemporaryDirectory() as temp:
            scripts = Path(temp)
            (scripts / 'abogus.py').write_text('class ABogus: pass')
            ns = {'extract_video_id': lambda source: '1234567890123456789',
                  'fetch_aweme_detail': lambda video_id: {'aweme_id': video_id, 'desc': 'Douyin', 'video': {'duration': 12500}},
                  'check_not_album': Mock(), 'main': Mock(side_effect=AssertionError('No main')),
                  'download': Mock(side_effect=AssertionError('No media download'))}
            with patch('vfnlib.probe_video.runpy.run_path', return_value=ns):
                report = probe(scripts, 'douyin', 'https://v.douyin.com/example', 1)
            self.assertEqual(report['duration_seconds'], 12.5)
            self.assertFalse((scripts / 'metadata.json').exists())
            self.assertFalse((scripts / 'media_p01.mp4').exists())

    def test_douyin_broken_helper_does_not_enter_upstream_installer(self):
        with tempfile.TemporaryDirectory() as temp:
            scripts = Path(temp)
            (scripts / 'abogus.py').write_text('raise ImportError("missing dependency")')
            with patch('vfnlib.probe_video.runpy.run_path', side_effect=AssertionError('No upstream import')):
                with self.assertRaisesRegex(ImportError, 'missing dependency'):
                    probe(scripts, 'douyin', 'url', 1)

    def test_douyin_missing_helper_does_not_trigger_installation(self):
        with tempfile.TemporaryDirectory() as temp, \
                patch('vfnlib.probe_video.runpy.run_path', side_effect=AssertionError('No import/install')):
            with self.assertRaisesRegex(ValueError, 'does not install'):
                probe(temp, 'douyin', 'url', 1)
