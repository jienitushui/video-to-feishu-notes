from __future__ import annotations

import os
import time
from urllib.parse import quote

import requests


class FeishuError(RuntimeError):
    pass


class AmbiguousWrite(FeishuError):
    """A mutation may have reached Feishu; inspect remote state before retrying."""


class Feishu:
    def __init__(self, config, session=None, sleep=time.sleep):
        self.config = config
        self.session = session or requests.Session()
        self.sleep = sleep
        self.token = None
        self.expires = 0
        self.base = config.get('base_url', 'https://open.feishu.cn') + '/open-apis'

    def access_token(self):
        explicit = os.getenv('FEISHU_USER_ACCESS_TOKEN') or os.getenv('FEISHU_TENANT_ACCESS_TOKEN')
        if explicit:
            return explicit
        if self.token and time.time() < self.expires:
            return self.token
        app_id, secret = os.getenv('FEISHU_APP_ID'), os.getenv('FEISHU_APP_SECRET')
        if not app_id or not secret:
            raise FeishuError('Set FEISHU_APP_ID and FEISHU_APP_SECRET, or a Feishu access token environment variable')
        try:
            response = self.session.post(self.base + '/auth/v3/tenant_access_token/internal',
                                         json={'app_id': app_id, 'app_secret': secret}, timeout=(10, 30))
            response.raise_for_status()
            payload = response.json()
        except (requests.RequestException, ValueError) as exc:
            raise FeishuError('Feishu authentication request failed') from exc
        if payload.get('code', 0) != 0 or not payload.get('tenant_access_token'):
            raise FeishuError('Feishu authentication failed: code=' + str(payload.get('code')))
        self.token = payload['tenant_access_token']
        self.expires = time.time() + max(0, payload.get('expire', 7200) - 120)
        return self.token

    def request(self, method, path, *, body=None, params=None, files=None, data=None, idempotent=False):
        method = method.upper()
        # Never replay a non-idempotent POST on a timeout, connection error, or 5xx.
        safe = method == 'GET' or idempotent
        for attempt in range(3):
            try:
                response = self.session.request(
                    method, self.base + path, headers={'Authorization': 'Bearer ' + self.access_token()},
                    json=body, params=params, files=files, data=data, timeout=(10, 90))
            except requests.RequestException as exc:
                if safe and files is None and attempt < 2:
                    self.sleep(2 ** attempt)
                    continue
                error = FeishuError if safe else AmbiguousWrite
                raise error('Feishu request interrupted; read remote state before retrying ' + method + ' ' + path) from exc
            if response.status_code == 429 or response.status_code >= 500:
                if safe and files is None and attempt < 2:
                    try:
                        delay = min(30, max(1, float(response.headers.get('Retry-After', 2 ** attempt))))
                    except ValueError:
                        delay = 2 ** attempt
                    self.sleep(delay)
                    continue
                error = AmbiguousWrite if not safe and response.status_code >= 500 else FeishuError
                raise error('Feishu HTTP ' + str(response.status_code) + '; retry later after checking remote state')
            try:
                payload = response.json()
            except ValueError as exc:
                raise (FeishuError if safe else AmbiguousWrite)('Invalid Feishu response') from exc
            if response.status_code >= 400 or payload.get('code', 0) != 0:
                raise FeishuError('Feishu code=' + str(payload.get('code', response.status_code)) +
                                  ': ' + str(payload.get('msg', 'Request failed')))
            return payload.get('data', payload)
        raise FeishuError('Request retry limit reached')

    def paginate(self, path, key='items', params=None):
        query = dict(params or {}, page_size=100)
        seen = set()
        while True:
            data = self.request('GET', path, params=query)
            yield from data.get(key, [])
            if not data.get('has_more'):
                break
            token = data.get('page_token')
            if not token or token in seen:
                raise FeishuError('Missing or repeated pagination token')
            seen.add(token)
            query['page_token'] = token

    @property
    def table_path(self):
        return '/bitable/v1/apps/{}/tables/{}'.format(
            quote(self.config.require('app_token'), safe=''), quote(self.config.require('table_id'), safe=''))

    def records(self):
        return list(self.paginate(self.table_path + '/records'))

    def record(self, record_id):
        return self.request('GET', self.table_path + '/records/' + quote(record_id, safe=''))['record']

    def update_record(self, record_id, fields):
        return self.request('PUT', self.table_path + '/records/' + quote(record_id, safe=''),
                            body={'fields': fields}, idempotent=True)['record']

    def create_record(self, fields, token):
        return self.request('POST', self.table_path + '/records',
                            body={'fields': fields}, params={'client_token': token})['record']

    def fields(self):
        return list(self.paginate(self.table_path + '/fields'))

    def create_field(self, definition):
        return self.request('POST', self.table_path + '/fields', body=definition)

    def document(self, document_id):
        return self.request('GET', '/docx/v1/documents/' + quote(document_id, safe=''))['document']

    def create_document(self, title):
        return self.request('POST', '/docx/v1/documents',
                            body={'title': title, 'folder_token': self.config.require('folder_token')})['document']

    def wiki_node(self, token, obj_type='wiki'):
        return self.request('GET', '/wiki/v2/spaces/get_node',
                            params={'token': token, 'obj_type': obj_type})['node']

    def create_wiki_node(self, title):
        destination = next((d for d in self.config.destinations() if d['type'] == 'wiki'), None)
        if not destination:
            raise ValueError('Configure publish_target=wiki or both before creating a wiki node')
        return self.request('POST', '/wiki/v2/spaces/{}/nodes'.format(
            quote(destination['space_id'], safe='')), body={
                'obj_type': 'docx', 'node_type': 'origin', 'title': title,
                'parent_node_token': destination['parent_node_token']})['node']

    def check_destination(self):
        destination = self.config.destination()
        if destination['type'] == 'both':
            return {'type': 'both', 'targets': [self._check_destination(d) for d in destination['targets']]}
        return self._check_destination(destination)

    def _check_destination(self, destination):
        if destination['type'] == 'wiki':
            node = self.wiki_node(destination['parent_node_token'])
            if (str(node.get('space_id')) != destination['space_id'] or
                    node.get('node_token') != destination['parent_node_token']):
                raise FeishuError('Wiki parent node does not belong to the configured space')
            return dict(destination, readable=True, write_access_checked=False)
        # Listing confirms access, but does not prove the caller may create files.
        self.request('GET', '/drive/v1/files', params={
            'folder_token': destination['folder_token'], 'page_size': 1})
        return dict(destination, readable=True, write_access_checked=False)

    def children(self, document_id):
        return list(self.paginate('/docx/v1/documents/{0}/blocks/{0}/children'.format(quote(document_id, safe=''))))

    def append_block(self, document_id, block, token):
        return self.request('POST', '/docx/v1/documents/{0}/blocks/{0}/children'.format(quote(document_id, safe='')),
                            params={'document_revision_id': -1, 'client_token': token},
                            body={'children': [block], 'index': -1})['children'][0]

    def upload_image(self, document_id, block_id, path):
        with path.open('rb') as handle:
            payload = self.request('POST', '/drive/v1/medias/upload_all',
                                   data={'file_name': path.name, 'parent_type': 'docx_image',
                                         'parent_node': document_id, 'size': str(path.stat().st_size)},
                                   files={'file': (path.name, handle, 'application/octet-stream')})
        token = payload['file_token']
        self.request('PATCH', '/docx/v1/documents/{}/blocks/{}'.format(
            quote(document_id, safe=''), quote(block_id, safe='')),
            params={'document_revision_id': -1}, body={'replace_image': {'token': token}}, idempotent=True)
        return token
