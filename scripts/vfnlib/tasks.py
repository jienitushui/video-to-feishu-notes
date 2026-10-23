from __future__ import annotations

import hashlib
import json
import time
import uuid

from .config import STATUSES


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def text(value):
    if isinstance(value, list):
        return ''.join(str(v.get('text', '')) if isinstance(v, dict) else str(v) for v in value)
    if isinstance(value, dict):
        return value.get('link', value.get('text', ''))
    return str(value) if value is not None else ''


class TaskError(RuntimeError):
    pass


class Tasks:
    def __init__(self, api, config):
        self.api, self.config = api, config

    def get(self, record, key, default=None):
        return record.get('fields', {}).get(self.config.field(key), default)

    def put(self, record_id, values):
        return self.api.update_record(record_id, {self.config.field(k): v for k, v in values.items()})

    @staticmethod
    def _empty_value(value):
        if value is None or value is False or value == '':
            return True
        if isinstance(value, (list, tuple, dict)):
            if not value:
                return True
            if isinstance(value, dict):
                return not any(str(v).strip() for v in value.values() if v is not None)
        return False

    @classmethod
    def is_empty_record(cls, record):
        return not any(not cls._empty_value(value)
                       for value in record.get('fields', {}).values())

    def cleanup_empty(self, apply=False, limit=100):
        if limit < 1 or limit > 100:
            raise ValueError('limit must be 1..100')
        candidates = [r for r in self.api.records() if self.is_empty_record(r)][:limit]
        result = {'candidates': [r['record_id'] for r in candidates],
                  'deleted': 0, 'applied': bool(apply)}
        if apply:
            for record in candidates:
                self.api.delete_record(record['record_id'])
                result['deleted'] += 1
        return result

    def snapshot(self, record):
        return {'url': text(self.get(record, 'url')).strip(),
                'requirements': text(self.get(record, 'requirements')).strip(),
                'template': self.config.get('template_version', 'v1'),
                'language': self.config.get('language', 'zh-CN')}

    def state(self, record):
        value = self.get(record, 'state')
        if not value:
            return {}
        try:
            state = json.loads(text(value))
            if not isinstance(state, dict):
                raise ValueError()
            return state
        except (ValueError, TypeError) as exc:
            raise TaskError('Task checkpoint is invalid; inspect it before continuing') from exc

    def save_state(self, record_id, state, **values):
        return self.put(record_id, dict(values, state=json.dumps(state, ensure_ascii=False), updated_at=int(time.time() * 1000)))

    def own(self, record_id, run_id):
        record = self.api.record(record_id)
        if self.get(record, 'status') != '处理中' or self.get(record, 'run_id') != run_id:
            raise TaskError('Task is not owned by this run; reread Feishu')
        return record

    def assert_input(self, record, state):
        if self.snapshot(record) != state.get('input'):
            raise TaskError('Task input changed; preserve the draft and start a new version for the new input')

    def select(self, limit=3, category=None, retry=False):
        if limit < 1 or limit > 100:
            raise ValueError('limit must be 1..100')
        selected = []
        for record in self.api.records():
            status = self.get(record, 'status', '待处理') or '待处理'
            eligible = status == '待处理' or (status == '已完成' and self.get(record, 'regenerate') is True)
            eligible = eligible or (retry and status == '失败')
            if not eligible or self.get(record, 'enabled') is not True or not text(self.get(record, 'url')).strip():
                continue
            if category is not None and text(self.get(record, 'category')) != category:
                continue
            selected.append({'record_id': record['record_id'], 'input': self.snapshot(record),
                             'status': status, 'regenerate': self.get(record, 'regenerate') is True})
            if len(selected) >= limit:
                break
        return selected

    def claim(self, record_id, *, requested=False, retry=False, regenerate=False, resume=False):
        if resume and (retry or regenerate):
            raise TaskError('--resume cannot be combined with --retry or --regenerate')
        record = self.api.record(record_id)
        if not self.snapshot(record)['url']:
            raise TaskError('Video URL is empty')
        status = self.get(record, 'status', '待处理') or '待处理'
        old_state = self.state(record)
        if resume:
            if status not in ('处理中', '失败', '待人工处理') or not old_state.get('input'):
                raise TaskError('No recoverable run; claim a new task instead')
            self.assert_input(record, old_state)
            state = old_state
        else:
            if status == '处理中':
                raise TaskError('Already in progress; confirm previous execution ended before --resume')
            if self.get(record, 'enabled') is not True and not requested:
                raise TaskError('Task is not enabled; use --requested only for an explicit user request')
            # The checkbox stays true until success. An explicit retry of an
            # already-created generation must not consume it for a second time.
            new_version = regenerate or (self.get(record, 'regenerate') is True and not (retry and old_state))
            if status == '已完成' and not new_version:
                raise TaskError('Already completed')
            if status in ('失败', '待人工处理') and not (retry or new_version):
                raise TaskError('Retry requires an explicit retry request')
            if status not in STATUSES:
                raise TaskError('Unknown task status')
            # Retry identical inputs with --resume, so a saved draft is not orphaned.
            if old_state and status in ('失败', '待人工处理') and not new_version:
                self.assert_input(record, old_state)
                state = old_state
            else:
                state = {'input': self.snapshot(record), 'generation': uuid.uuid4().hex,
                         'force': bool(new_version), 'publish': {}}
                # Retain cleanup work across failed/replaced generations. Only verified
                # published results are eligible; arbitrary pasted links are never deleted.
                cleanup = [dict(item) for item in old_state.get('version_cleanup', [])
                           if item.get('status') != 'deleted']
                previous = old_state.get('published')
                for target in self.superseded_targets(record, previous):
                    if not any(item['document_id'] == target['document_id'] for item in cleanup):
                        cleanup.append(dict(target, status='pending'))
                if cleanup:
                    state['version_cleanup'] = cleanup
        run_id = uuid.uuid4().hex
        state['run_id'] = run_id
        next_version = int(self.get(record, 'version', 0) or 0)
        if state is not old_state or not old_state:
            next_version += 1
        values = {'run_id': run_id, 'status': '处理中', 'stage': '校验输入', 'error': '',
                  'attempts': int(self.get(record, 'attempts', 0) or 0) + 1}
        if state is not old_state or not old_state:
            values.update(document_id='', source_key='', fingerprint='',
                          version=next_version)
        self.save_state(record_id, state, **values)
        self.own(record_id, run_id)
        return {'record_id': record_id, 'run_id': run_id, 'version': next_version,
                'workdir': str(self.config.workdir(record_id, next_version)),
                'state': state}

    def stage(self, record_id, run_id, stage):
        record = self.own(record_id, run_id)
        state = self.state(record)
        self.assert_input(record, state)
        state['checkpoint'] = stage
        self.save_state(record_id, state, stage=stage)
        return {'record_id': record_id, 'stage': stage}

    def fail(self, record_id, run_id, message, manual=False):
        self.own(record_id, run_id)
        self.put(record_id, {'status': '待人工处理' if manual else '失败',
                             'error': message[:2000], 'updated_at': int(time.time() * 1000)})
        return {'record_id': record_id, 'error': message}

    @staticmethod
    def result_targets(published):
        destination = published['destination']
        if destination['type'] == 'both':
            targets = published.get('targets', {})
            if set(targets) != {'folder', 'wiki'}:
                raise TaskError('Dual archive result is incomplete')
            for target in destination['targets']:
                result = targets[target['type']]
                if result.get('destination') != target or result.get('hash') != published.get('hash'):
                    raise TaskError('Dual archive result does not match its destination or content')
            return targets
        return {destination['type']: published}

    def superseded_targets(self, record, previous):
        # A legacy checkpoint or a manually replaced link is not deletion evidence.
        if not previous:
            return []
        try:
            targets = list(self.result_targets(previous).values())
            if any(not all(target.get(key) for key in ('document_id', 'url', 'hash', 'destination'))
                   for target in targets):
                return []
            if any(text(self.get(record, key)) != text(value)
                   for key, value in self.result_links(previous, '').items()):
                return []
        except (KeyError, TypeError, TaskError):
            return []
        return targets

    def result_links(self, published, title):
        targets = self.result_targets(published)
        values = {'document': {'link': published['url'], 'text': title}}
        backup = targets.get('folder') if published['destination']['type'] == 'both' else None
        values['folder_document'] = {'link': backup['url'], 'text': title + '（生成备份）'} if backup else None
        # Maintain explicitly mapped legacy columns without adding them to new tables.
        if 'wiki_document' in self.config.get('fields', {}):
            wiki = targets.get('wiki')
            values['wiki_document'] = {'link': wiki['url'], 'text': title} if wiki else None
        return values

    def bind(self, record_id, run_id, manifest):
        record = self.own(record_id, run_id)
        state = self.state(record)
        self.assert_input(record, state)
        if text(manifest.get('request')).strip() != state['input']['url']:
            raise TaskError('Manifest does not belong to this input')
        source = manifest['source']
        key = source['key']
        fingerprint = digest(dict(state['input'], url=key))
        if state.get('fingerprint') and state['fingerprint'] != fingerprint:
            raise TaskError('Source changed within this generation')
        state.update(source=source, fingerprint=fingerprint)
        self.save_state(record_id, state, task_name=source['title'], source_key=key,
                        fingerprint=fingerprint, title=source['title'], platform=source['platform'])
        if not state.get('force') and not state.get('publish'):
            for other in self.api.records():
                if other['record_id'] == record_id or self.get(other, 'status') != '已完成':
                    continue
                if self.get(other, 'fingerprint') != fingerprint:
                    continue
                candidate = self.state(other)
                published = candidate.get('published')
                if not published or not published.get('document_id'):
                    continue
                quality = published.get('note_quality', {})
                if not quality.get('images') and not quality.get('text_only_reason'):
                    # Old text-only results must not bypass the new image requirement via reuse.
                    continue
                # Content can be reused only within the requested archive location.
                # Legacy results without a destination need a new publication.
                if published.get('destination') != self.config.destination():
                    continue
                try:
                    for result in self.result_targets(published).values():
                        self.api.document(result['document_id'])
                        if result['destination']['type'] == 'wiki':
                            from .publish import Publisher
                            node = self.api.wiki_node(result['wiki_node_token'])
                            Publisher.check_wiki_node(node, result['destination'], result['document_id'])
                except Exception:
                    continue
                fresh = self.own(record_id, run_id)
                self.assert_input(fresh, state)
                state['published'] = dict(published, reused_from=other['record_id'])
                self.save_state(record_id, state, status='已完成', stage='完成', result='复用已有笔记',
                                **self.result_links(published, source['title']),
                                summary=text(self.get(other, 'summary')), completed_at=int(time.time() * 1000),
                                regenerate=False, error='')
                return {'record_id': record_id, 'reused': True, 'url': published['url'],
                        'targets': self.result_targets(published)}
        return {'record_id': record_id, 'reused': False, 'fingerprint': fingerprint}

    def schema(self, apply=False):
        actual = {f['field_name']: f for f in self.api.fields()}
        missing, mismatched = [], []
        for key, (_, field_type) in self.config.field_definitions().items():
            name = self.config.field(key)
            if name in actual:
                if actual[name]['type'] != field_type:
                    mismatched.append({'field': name, 'expected': field_type, 'actual': actual[name]['type']})
            else:
                definition = {'field_name': name, 'type': field_type}
                if key == 'status':
                    definition['property'] = {'options': [{'name': status} for status in STATUSES]}
                if key in ('updated_at', 'completed_at'):
                    definition['property'] = {'date_formatter': 'yyyy/MM/dd HH:mm', 'auto_fill': False}
                missing.append(definition)
        if mismatched:
            raise TaskError('Existing field types do not match: ' + json.dumps(mismatched, ensure_ascii=False))
        if apply:
            for definition in missing:
                self.api.create_field(definition)
        return {'missing': missing, 'created': len(missing) if apply else 0}
