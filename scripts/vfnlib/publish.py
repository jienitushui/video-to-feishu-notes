from __future__ import annotations

import time
import uuid

from .feishu import AmbiguousWrite, FeishuError
from .notes import plan_note
from .tasks import TaskError, digest


def signature(block):
    kind = block.get('block_type')
    if kind == 27:
        return {'block_type': 27}
    name = {2: 'text', 3: 'heading1', 4: 'heading2', 12: 'bullet', 14: 'code', 15: 'quote'}.get(kind)
    payload = block.get(name, {}) if name else {}
    runs = []
    for element in payload.get('elements', []):
        run = element.get('text_run', {})
        runs.append({'text': run.get('content', ''),
                     'link': run.get('text_element_style', {}).get('link', {}).get('url')})
    return {'block_type': kind, 'runs': runs}


class Publisher:
    def __init__(self, tasks):
        self.tasks, self.api = tasks, tasks.api

    def publish(self, record_id, run_id, note, base):
        task = self.tasks.own(record_id, run_id)
        state = self.tasks.state(task)
        self.tasks.assert_input(task, state)
        source = state.get('source')
        if not source or note['source_key'] != source['key']:
            raise TaskError('Bind a matching source before publishing')
        marker = 'VFN task={} generation={}'.format(record_id, state['generation'])
        plan = plan_note(note, base, source, marker)
        # Absolute paths differ across devices; content and image hashes define identity.
        plan_hash = digest([{k: v for k, v in item.items() if k != 'path'} for item in plan])
        pub = state.setdefault('publish', {})
        if pub.get('hash') and pub['hash'] != plan_hash:
            raise TaskError('Draft content or images changed; resume the original note or regenerate a new version')
        pub.update(hash=plan_hash, expected_blocks=len(plan))
        document_id = self.tasks.get(task, 'document_id')
        if not document_id:
            if pub.get('creating'):
                raise TaskError('Document creation outcome is unknown. Find the draft by its saved title and use attach-document; do not create another')
            pub.update(creating=True, title=note['title'] + ' [vfn:' + record_id + ':' + state['generation'][:8] + ']')
            self.tasks.save_state(record_id, state, stage='创建文档')
            try:
                document = self.api.create_document(pub['title'])
            except AmbiguousWrite:
                raise
            except FeishuError:
                pub['creating'] = False
                self.tasks.save_state(record_id, state)
                raise
            document_id = document['document_id']
            pub['creating'] = False
            try:
                self.tasks.save_state(record_id, state, document_id=document_id, stage='写入文档')
            except Exception as exc:
                raise TaskError('Document created but checkpoint failed. Preserve and attach document ID: ' + document_id) from exc
        else:
            self.api.document(document_id)
        # Feishu may return one empty starter paragraph for a new document.
        existing = self.api.children(document_id)
        start = pub.get('starter_blocks')
        if start is None:
            start = []
            if len(existing) == 1 and existing[0].get('block_type') == 2 and all(
                    not r.get('text_run', {}).get('content', '') for r in existing[0].get('text', {}).get('elements', [])):
                start = [existing[0]['block_id']]
            pub['starter_blocks'] = start
            self.tasks.save_state(record_id, state, document_id=document_id)
        actual = [b for b in existing if b.get('block_id') not in start]
        if len(actual) > len(plan):
            raise TaskError('Draft contains extra blocks; inspect remote document before continuing')
        for index, block in enumerate(actual):
            if signature(block) != signature(plan[index]['block']):
                raise TaskError('Draft content differs at block ' + str(index) + '; refusing to append')
        # Each append uses a stable UUID and is reconciled on the next invocation.
        for index, item in enumerate(plan):
            fresh = self.tasks.own(record_id, run_id)
            self.tasks.assert_input(fresh, state)
            if index < len(actual):
                block = actual[index]
            else:
                token = str(uuid.uuid5(uuid.NAMESPACE_URL, state['generation'] + ':' + plan_hash + ':' + str(index)))
                block = self.api.append_block(document_id, item['block'], token)
                actual.append(block)
            if 'path' in item and not block.get('image', {}).get('token'):
                from pathlib import Path
                self.api.upload_image(document_id, block['block_id'], Path(item['path']))
            pub['confirmed_blocks'] = index + 1
            self.tasks.save_state(record_id, state, stage='写入文档')
        final = [b for b in self.api.children(document_id) if b.get('block_id') not in start]
        if len(final) != len(plan) or any(signature(a) != signature(b['block']) for a, b in zip(final, plan)):
            raise TaskError('Final document block verification failed')
        for block, item in zip(final, plan):
            if 'path' in item and not block.get('image', {}).get('token'):
                raise TaskError('Image token missing after publication')
        fresh = self.tasks.own(record_id, run_id)
        self.tasks.assert_input(fresh, state)
        self.api.document(document_id)  # Confirms API caller access, not every human reader.
        prefix = self.tasks.config.get('document_url_prefix', 'https://feishu.cn/docx/')
        url = prefix.rstrip('/') + '/' + document_id
        state['published'] = {'document_id': document_id, 'url': url, 'hash': plan_hash}
        self.tasks.save_state(record_id, state, status='已完成', stage='完成',
                              document={'link': url, 'text': note['title']}, summary=note['summary'],
                              completed_at=int(time.time() * 1000), result='新生成', regenerate=False, error='')
        return {'record_id': record_id, 'status': '已完成', 'document_id': document_id, 'url': url, 'blocks': len(final)}

    def attach(self, record_id, run_id, document_id):
        record = self.tasks.own(record_id, run_id)
        state = self.tasks.state(record)
        self.tasks.assert_input(record, state)
        if self.tasks.get(record, 'document_id'):
            raise TaskError('A document is already attached')
        pub = state.get('publish', {})
        if not pub.get('creating') or not pub.get('title'):
            raise TaskError('No unresolved document creation')
        if self.api.document(document_id).get('title') != pub['title']:
            raise TaskError('Document title does not match the saved creation attempt')
        children = self.api.children(document_id)
        if any(b.get('block_type') != 2 or any(e.get('text_run', {}).get('content')
               for e in b.get('text', {}).get('elements', [])) for b in children):
            raise TaskError('Expected an empty draft; inspect its content before attaching')
        pub['creating'] = False
        self.tasks.save_state(record_id, state, document_id=document_id)
        return {'record_id': record_id, 'document_id': document_id, 'attached': True}
