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

    def test_wiki_create_uses_native_docx_node_and_no_folder(self):
        node = {'node_token': 'wiki1', 'obj_token': 'doc1'}
        api, session = self.api([Response({'code': 0, 'data': {'node': node}})])
        api.config.data.update(publish_target='wiki', wiki_space_id='123', wiki_parent_node_token='parent')
        self.assertEqual(api.create_wiki_node('Title'), node)
        method, url, payload = session.calls[0]
        self.assertEqual(method, 'POST')
        self.assertTrue(url.endswith('/wiki/v2/spaces/123/nodes'))
        self.assertEqual(payload['json'], {'obj_type': 'docx', 'node_type': 'origin',
                                          'parent_node_token': 'parent', 'title': 'Title'})

    def test_wiki_creation_not_replayed_on_uncertain_response(self):
        for response in [requests.Timeout(), Response(status=503)]:
            api, session = self.api([response])
            api.config.data.update(publish_target='wiki', wiki_space_id='123', wiki_parent_node_token='parent')
            with self.assertRaises(AmbiguousWrite):
                api.create_wiki_node('Title')
            self.assertEqual(len(session.calls), 1)

    def test_wiki_document_lookup_and_parent_preflight(self):
        node = {'node_token': 'parent', 'space_id': 123, 'obj_token': 'doc-parent'}
        api, session = self.api([Response({'data': {'node': node}}), Response({'data': {'node': node}})])
        api.config.data.update(publish_target='wiki', wiki_space_id='123', wiki_parent_node_token='parent')
        self.assertEqual(api.wiki_node('doc-parent', obj_type='docx'), node)
        self.assertEqual(session.calls[0][2]['params'], {'token': 'doc-parent', 'obj_type': 'docx'})
        report = api.check_destination()
        self.assertTrue(report['readable'])
        self.assertFalse(report['write_access_checked'])
        self.assertTrue(all(c[0] == 'GET' for c in session.calls))

    def test_wiki_parent_from_another_space_is_rejected(self):
        api, _ = self.api([Response({'data': {'node': {'node_token': 'parent', 'space_id': 'wrong'}}})])
        api.config.data.update(publish_target='wiki', wiki_space_id='123', wiki_parent_node_token='parent')
        with self.assertRaisesRegex(FeishuError, 'configured space'):
            api.check_destination()

    def test_destination_config_requires_only_selected_mode(self):
        self.assertEqual(Config({'folder_token': 'folder'}).destination(),
                         {'type': 'folder', 'folder_token': 'folder'})
        config = Config({'publish_target': 'wiki', 'wiki_space_id': '123', 'wiki_parent_node_token': 'parent'})
        self.assertEqual(config.destination()['type'], 'wiki')
        with self.assertRaisesRegex(ValueError, 'wiki_parent_node_token'):
            Config({'publish_target': 'wiki', 'wiki_space_id': '123'}).destination()
        with self.assertRaisesRegex(ValueError, 'publish_target'):
            Config({'publish_target': 'unknown'})

    def test_both_preflight_checks_folder_and_wiki_without_writes(self):
        api, session = self.api([Response({'data': {'files': []}}), Response({'data': {
            'node': {'node_token': 'parent', 'space_id': '123'}}})])
        api.config.data.update(publish_target='both', folder_token='folder',
                               wiki_space_id='123', wiki_parent_node_token='parent')
        result = api.check_destination()
        self.assertEqual([t['type'] for t in result['targets']], ['folder', 'wiki'])
        self.assertTrue(all(t['readable'] and not t['write_access_checked'] for t in result['targets']))
        self.assertTrue(all(call[0] == 'GET' for call in session.calls))
        self.assertEqual(session.calls[0][2]['params']['folder_token'], 'folder')

    def test_both_requires_both_resource_configs_and_supports_native_wiki_creation(self):
        config = Config({'publish_target': 'both', 'folder_token': 'folder', 'wiki_space_id': '123'})
        with self.assertRaisesRegex(ValueError, 'wiki_parent_node_token'):
            config.destination()
        api, session = self.api([Response({'data': {'node': {'node_token': 'wiki1', 'obj_token': 'doc2'}}})])
        api.config.data.update(publish_target='both', folder_token='folder',
                               wiki_space_id='123', wiki_parent_node_token='parent')
        self.assertEqual(api.create_wiki_node('Title')['obj_token'], 'doc2')
        self.assertEqual(session.calls[0][2]['json']['parent_node_token'], 'parent')

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
