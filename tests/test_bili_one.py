import gzip
import io
import json
import sys
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.error import HTTPError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from vfnlib import bili_one


BVID = 'BV1Wv411h7kN'
VIEW_URL = 'https://api.bilibili.com/x/web-interface/view?bvid=' + BVID
PAGE_URL = 'https://www.bilibili.com/video/' + BVID
PAGELIST_URL = 'https://api.bilibili.com/x/player/pagelist?bvid=' + BVID
SUBTITLE_URL = 'https://api.bilibili.com/x/player/v2?bvid=' + BVID + '&cid=22'
PLAYURL = 'https://api.bilibili.com/x/player/playurl?bvid=' + BVID + '&cid=22'


class BiliOneTests(unittest.TestCase):
    def setUp(self):
        self.pages = [
            {'page': 1, 'cid': 11, 'part': 'Introduction', 'duration': 10},
            {'page': 2, 'cid': 22, 'part': 'Second lesson', 'duration': 20},
        ]
        self.video = {'bvid': BVID, 'aid': 123, 'title': 'Lecture',
                      'owner': {'name': 'Teacher'}, 'desc': 'Description',
                      'duration': 30, 'pages': self.pages}
        self.http_get = Mock(return_value=self.html())
        self.view_error = HTTPError(VIEW_URL, 412, 'Precondition Failed', {}, None)
        self.view_result = None
        self.pagelist_result = {'code': 0, 'data': self.pages}
        self.subtitle_result = {'code': 0, 'data': {'subtitle': {'subtitles': []}}}
        self.playurl_result = {'code': 0, 'data': {'dash': {'audio': []}}}
        self.original_api = Mock(side_effect=self.respond)

    def html(self, video=None):
        state = {'videoData': self.video if video is None else video}
        return ('<script>window.__INITIAL_STATE__ = '
                + json.dumps(state) + ';(function(){})();</script>').encode('utf-8')

    def respond(self, url, *args, **kwargs):
        if url == VIEW_URL:
            if self.view_error:
                raise self.view_error
            return self.view_result
        if url == PAGELIST_URL:
            return self.pagelist_result
        if url == SUBTITLE_URL:
            return self.subtitle_result
        if url == PLAYURL:
            return self.playurl_result
        raise AssertionError('Unexpected API URL: ' + url)

    def run_adapter(self, page=2):
        def upstream_main():
            api = upstream_main.__globals__['api']
            self.result = api(VIEW_URL, cookie='test-cookie')
            self.subtitle = api(SUBTITLE_URL, 'test-cookie')
            self.playurl = api(PLAYURL, 'test-cookie')

        globals_patch = {'api': self.original_api, 'http_get': self.http_get}
        argv = ['bili_one.py', 'upstream.py', BVID, 'output', str(page)]
        with patch.dict(upstream_main.__globals__, globals_patch), \
                patch('vfnlib.bili_one.runpy.run_path', return_value={'main': upstream_main}), \
                patch.object(sys, 'argv', argv), redirect_stderr(io.StringIO()):
            try:
                bili_one.main()
            finally:
                self.assertIs(sys.argv, argv)
                self.assertIs(upstream_main.__globals__['api'], self.original_api)

    def test_http_412_falls_back_and_selects_requested_page(self):
        self.run_adapter()
        data = self.result['data']
        self.assertEqual(data['bvid'], BVID)
        for field in ('aid', 'title', 'owner', 'desc'):
            self.assertEqual(data[field], self.video[field])
        self.assertEqual(data['pages'], [self.pages[1]])
        self.assertEqual(data['duration'], 20)
        self.http_get.assert_called_once_with(PAGE_URL, cookie='test-cookie')
        self.assertIn(((PAGELIST_URL,), {'cookie': 'test-cookie'}),
                      self.original_api.call_args_list)

    def test_successful_view_preserves_original_path(self):
        self.view_error = None
        self.view_result = {'code': 0, 'data': self.video}
        self.run_adapter(page=1)
        self.assertEqual(self.result['data']['pages'], [self.pages[0]])
        self.assertEqual(self.result['data']['duration'], 10)
        self.http_get.assert_not_called()
        self.assertNotIn(PAGELIST_URL, [call.args[0] for call in self.original_api.call_args_list])
        self.assertEqual(self.video['pages'], self.pages)
        self.assertEqual(self.video['duration'], 30)

    def test_business_api_failure_also_uses_fallback(self):
        self.view_error = RuntimeError('Interface failed code=-352')
        self.run_adapter()
        self.assertEqual(self.result['data']['pages'], [self.pages[1]])

    def test_pagelist_overrides_stale_webpage_pages(self):
        self.http_get.return_value = self.html(dict(self.video, pages=[self.pages[0]]))
        self.run_adapter()
        self.assertEqual(self.result['data']['pages'], [self.pages[1]])

    def test_gzip_page_and_json_strings_with_braces_are_supported(self):
        title = 'Lecture }; and "quotes"'
        self.http_get.return_value = gzip.compress(self.html(dict(self.video, title=title)))
        self.run_adapter()
        self.assertEqual(self.result['data']['title'], title)

    def test_subtitle_and_playurl_are_passed_through(self):
        self.run_adapter()
        self.assertIs(self.subtitle, self.subtitle_result)
        self.assertIs(self.playurl, self.playurl_result)
        self.original_api.assert_any_call(SUBTITLE_URL, 'test-cookie')
        self.original_api.assert_any_call(PLAYURL, 'test-cookie')

    def test_missing_page_fails_without_processing_another_page(self):
        with self.assertRaisesRegex(ValueError, 'Requested page does not exist'):
            self.run_adapter(page=3)
        self.assertNotIn(SUBTITLE_URL, [call.args[0] for call in self.original_api.call_args_list])

    def test_wrong_video_or_incomplete_metadata_is_rejected(self):
        for changed in ({'bvid': 'BV1xx411c7mD'}, {'aid': None},
                        {'title': ''}, {'owner': {}}, {'owner': None}):
            with self.subTest(changed=changed):
                self.http_get.return_value = self.html(dict(self.video, **changed))
                with self.assertRaisesRegex(RuntimeError, 'view:.*412.*fallback:'):
                    self.run_adapter()

    def test_missing_or_malformed_initial_state_is_rejected(self):
        for body in (b'<html>Login required</html>',
                     b'<script>window.__INITIAL_STATE__={broken;</script>'):
            with self.subTest(body=body):
                self.http_get.return_value = body
                with self.assertRaisesRegex(RuntimeError, 'metadata fetch failed'):
                    self.run_adapter()

    def test_invalid_pagelist_is_rejected(self):
        for pages in ([], None, {}, [dict(self.pages[0], cid=None)],
                      [dict(self.pages[0], duration=-1)], [self.pages[0]] * 2):
            with self.subTest(pages=pages):
                self.pagelist_result = {'code': 0, 'data': pages}
                with self.assertRaisesRegex(RuntimeError, 'Pagelist returned'):
                    self.run_adapter()

    def test_fallback_network_failure_preserves_both_errors(self):
        self.http_get.side_effect = TimeoutError('Video page timed out')
        with self.assertRaisesRegex(RuntimeError, 'view:.*412.*fallback: Video page timed out'):
            self.run_adapter()

    def test_pagelist_api_failure_is_reported(self):
        def respond(url, *args, **kwargs):
            if url == PAGELIST_URL:
                raise RuntimeError('Pagelist failed code=-404')
            return self.respond(url, *args, **kwargs)

        self.original_api.side_effect = respond
        with self.assertRaisesRegex(RuntimeError, 'fallback: Pagelist failed code=-404'):
            self.run_adapter()


if __name__ == '__main__':
    unittest.main()
