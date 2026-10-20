import sys
import unittest
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from vfnlib.config import Config
from vfnlib.feishu import Feishu, FeishuError, AmbiguousWrite


class Response:
    def __init__(self, data=None, status=200):
        self.status_code = status
        self.headers = {'Retry-After': '0'}
        self.payload = data if data is not None else {'code': 0, 'data': {}}

    def json(self):
        return self.payload


class Session:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, copy_kwargs(kwargs)))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def copy_kwargs(kwargs):
    import copy
    return copy.deepcopy(kwargs)


class FeishuTests(unittest.TestCase):
    def api(self, responses):
        session = Session(responses)
        api = Feishu(Config({'app_token': 'base123', 'table_id': 'tbl123'}), session=session, sleep=lambda _: None)
        api.access_token = lambda: 'test-token'
        return api, session

    def test_get_retries_network_and_rate_limits(self):
        api, session = self.api([requests.Timeout(), Response(status=429), Response({'code': 0, 'data': {'value': 7}})])
        self.assertEqual(api.request('GET', '/path'), {'value': 7})
        self.assertEqual(len(session.calls), 3)

    def test_document_creation_not_replayed_on_timeout_or_server_error(self):
        for response in [requests.Timeout(), Response(status=503)]:
            api, session = self.api([response])
            api.config.data['folder_token'] = 'folder'
            with self.assertRaises(AmbiguousWrite):
                api.create_document('Title')
            self.assertEqual(len(session.calls), 1)

    def test_record_update_can_replay_same_payload(self):
        api, session = self.api([requests.Timeout(), Response({'code': 0, 'data': {'record': {'record_id': 'rec'}}})])
        self.assertEqual(api.update_record('rec', {'处理状态': '处理中'})['record_id'], 'rec')
        self.assertEqual(session.calls[0][2]['json'], session.calls[1][2]['json'])

    def test_record_pagination_and_missing_page_token(self):
        api, session = self.api([
            Response({'code': 0, 'data': {'items': [{'record_id': 'r1'}], 'has_more': True, 'page_token': 'p2'}}),
            Response({'code': 0, 'data': {'items': [{'record_id': 'r2'}], 'has_more': False}})])
        self.assertEqual([r['record_id'] for r in api.records()], ['r1', 'r2'])
        self.assertEqual(session.calls[1][2]['params']['page_token'], 'p2')
        api, _ = self.api([Response({'code': 0, 'data': {'has_more': True}})])
        with self.assertRaises(FeishuError):
            api.records()

    def test_api_error_does_not_look_successful(self):
        api, session = self.api([Response({'code': 99991672, 'msg': 'permission denied'})])
        with self.assertRaisesRegex(FeishuError, 'permission denied'):
            api.request('GET', '/path')
        self.assertEqual(len(session.calls), 1)


if __name__ == '__main__':
    unittest.main()
