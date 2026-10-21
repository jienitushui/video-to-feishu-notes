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

    @staticmethod
    def check_wiki_node(node, destination, document_id=None):
        if (not node.get('node_token') or not node.get('obj_token') or
                node.get('obj_type') != 'docx' or node.get('node_type') != 'origin' or
                str(node.get('space_id')) != destination['space_id'] or
                node.get('parent_node_token') != destination['parent_node_token'] or
                (document_id and node.get('obj_token') != document_id)):
            raise TaskError('Wiki node does not match the document and configured destination; inspect before continuing')
        return {k: node[k] for k in ('node_token', 'obj_token')}

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
        destination = self.tasks.config.destination()
        if pub.get('destination') and pub['destination'] != destination:
            raise TaskError('Publication destination changed; restore the original config or regenerate a new version')
        if pub and not pub.get('destination') and destination['type'] != 'folder':
            raise TaskError('Legacy folder draft cannot change archive mode; restore folder config or regenerate')
        pub['destination'] = destination
        if pub.get('hash') and pub['hash'] != plan_hash:
            raise TaskError('Draft content or images changed; resume the original note or regenerate a new version')
        pub.update(hash=plan_hash, expected_blocks=len(plan))
        if destination['type'] == 'both':
            targets = pub.setdefault('targets', {})
            results = {}
            for target in destination['targets']:
                fresh = self.tasks.own(record_id, run_id)
                self.tasks.assert_input(fresh, state)
                target_pub = targets.setdefault(target['type'], {'destination': target})
                result = self.publish_target(record_id, run_id, state, target_pub, target,
                                             target_pub.get('document_id'), plan, plan_hash, note)
                target_pub['published'] = result
                label = '云盘生成备份' if target['type'] == 'folder' else '知识库主文档'
                self.tasks.save_state(record_id, state, stage=label + '已归档')
                results[target['type']] = result
            # Verify both results again after the last target finishes.
            for target in destination['targets']:
                result = results[target['type']]
                self.verify_result(result, target, plan, targets[target['type']])
            primary = results['wiki']
            published = dict(primary, destination=destination, targets=results)
        else:
            document_id = pub.get('document_id') or self.tasks.get(task, 'document_id')
            published = self.publish_target(record_id, run_id, state, pub, destination,
                                            document_id, plan, plan_hash, note)
        fresh = self.tasks.own(record_id, run_id)
        self.tasks.assert_input(fresh, state)
        state['published'] = published
        self.tasks.save_state(record_id, state, status='已完成', stage='完成',
                              **self.tasks.result_links(published, note['title']), summary=note['summary'],
                              completed_at=int(time.time() * 1000), result='新生成', regenerate=False, error='')
        return dict(published, record_id=record_id, status='已完成')

    def save_target(self, record_id, state, pub, **values):
        document_id = values.get('document_id')
        if document_id:
            pub['document_id'] = document_id
        if state['publish'].get('destination', {}).get('type') == 'both':
            if pub['destination']['type'] != 'folder':
                values.pop('document_id', None)
        return self.tasks.save_state(record_id, state, **values)

    def verify_result(self, result, destination, plan, pub):
        document_id = result['document_id']
        self.api.document(document_id)
        if destination['type'] == 'wiki':
            self.check_wiki_node(self.api.wiki_node(result['wiki_node_token']), destination, document_id)
        final = [b for b in self.api.children(document_id)
                 if b.get('block_id') not in pub.get('starter_blocks', [])]
        if len(final) != len(plan) or any(signature(a) != signature(b['block']) for a, b in zip(final, plan)):
            raise TaskError('Final document block verification failed')
        if any('path' in item and not block.get('image', {}).get('token') for block, item in zip(final, plan)):
            raise TaskError('Image token missing after publication')

    def publish_target(self, record_id, run_id, state, pub, destination, document_id, plan, plan_hash, note):
        if pub.get('destination') != destination:
            raise TaskError('Target checkpoint destination differs from configured destination')
        dual = state['publish']['destination']['type'] == 'both'
        label = ('云盘生成备份' if destination['type'] == 'folder' else '知识库主文档') if dual else '文档'
        if pub.get('published'):
            result = pub['published']
            if result.get('hash') != plan_hash or result.get('document_id') != document_id:
                raise TaskError('Completed target checkpoint differs from this publication')
            self.verify_result(result, destination, plan, pub)
            return result
        if not document_id:
            if pub.get('creating'):
                raise TaskError('Document creation outcome is unknown. Find the draft by its saved title and use attach-document; do not create another')
            title = note['title'] + (' [生成备份]' if dual and destination['type'] == 'folder' else '')
            pub.update(creating=True, title=title + ' [vfn:' + record_id + ':' + state['generation'][:8] + ']')
            self.save_target(record_id, state, pub, stage='创建' + label)
            try:
                if destination['type'] == 'wiki':
                    node = self.api.create_wiki_node(pub['title'])
                    pub['wiki_node'] = self.check_wiki_node(node, destination)
                    document_id = node['obj_token']
                else:
                    document_id = self.api.create_document(pub['title'])['document_id']
            except AmbiguousWrite:
                raise
            except FeishuError:
                pub['creating'] = False
                self.save_target(record_id, state, pub)
                raise
            pub['creating'] = False
            try:
                self.save_target(record_id, state, pub, document_id=document_id, stage='写入' + label)
            except Exception as exc:
                raise TaskError('Document created but checkpoint failed. Preserve and attach document ID: ' + document_id) from exc
        else:
            self.api.document(document_id)
        if destination['type'] == 'wiki':
            node_token = pub.get('wiki_node', {}).get('node_token')
            if not node_token:
                raise TaskError('Missing wiki node checkpoint; inspect before continuing')
            self.check_wiki_node(self.api.wiki_node(node_token), destination, document_id)
        # Feishu may return one empty starter paragraph for a new document.
        existing = self.api.children(document_id)
        start = pub.get('starter_blocks')
        if start is None:
            start = []
            if len(existing) == 1 and existing[0].get('block_type') == 2 and all(
                    not r.get('text_run', {}).get('content', '') for r in existing[0].get('text', {}).get('elements', [])):
                start = [existing[0]['block_id']]
            pub['starter_blocks'] = start
            self.save_target(record_id, state, pub, document_id=document_id)
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
                scope = state['generation']
                if state['publish']['destination']['type'] == 'both':
                    scope += ':' + document_id
                token = str(uuid.uuid5(uuid.NAMESPACE_URL, scope + ':' + plan_hash + ':' + str(index)))
                block = self.api.append_block(document_id, item['block'], token)
                actual.append(block)
            if 'path' in item and not block.get('image', {}).get('token'):
                from pathlib import Path
                self.api.upload_image(document_id, block['block_id'], Path(item['path']))
            pub['confirmed_blocks'] = index + 1
            self.save_target(record_id, state, pub, stage='写入' + label)
        final = [b for b in self.api.children(document_id) if b.get('block_id') not in start]
        if len(final) != len(plan) or any(signature(a) != signature(b['block']) for a, b in zip(final, plan)):
            raise TaskError('Final document block verification failed')
        for block, item in zip(final, plan):
            if 'path' in item and not block.get('image', {}).get('token'):
                raise TaskError('Image token missing after publication')
        self.api.document(document_id)  # Confirms API caller access, not every human reader.
        published = {'document_id': document_id, 'hash': plan_hash, 'destination': destination}
        if destination['type'] == 'wiki':
            node_token = pub['wiki_node']['node_token']
            self.check_wiki_node(self.api.wiki_node(node_token), destination, document_id)
            prefix = self.tasks.config.get('wiki_url_prefix', 'https://feishu.cn/wiki/')
            url = prefix.rstrip('/') + '/' + node_token
            published['wiki_node_token'] = node_token
        else:
            prefix = self.tasks.config.get('document_url_prefix', 'https://feishu.cn/docx/')
            url = prefix.rstrip('/') + '/' + document_id
        published['url'] = url
        return dict(published, blocks=len(final))

    def attach(self, record_id, run_id, document_id, target=None):
        record = self.tasks.own(record_id, run_id)
        state = self.tasks.state(record)
        self.tasks.assert_input(record, state)
        root = state.get('publish', {})
        configured = self.tasks.config.destination()
        if root.get('destination') and root['destination'] != configured:
            raise TaskError('Publication destination changed; restore the original config')
        if configured['type'] == 'both':
            if target not in ('folder', 'wiki'):
                raise TaskError('attach-document requires --target folder or wiki in both mode')
            destination = next(d for d in configured['targets'] if d['type'] == target)
            pub = root.get('targets', {}).get(target, {})
            attached = pub.get('document_id')
        else:
            if target and target != configured['type']:
                raise TaskError('Attachment target differs from configured destination')
            destination, pub = configured, root
            attached = pub.get('document_id') or self.tasks.get(record, 'document_id')
        if attached:
            raise TaskError('A document is already attached')
        if not pub.get('creating') or not pub.get('title'):
            raise TaskError('No unresolved document creation')
        if destination['type'] == 'wiki':
            if not pub.get('destination'):
                raise TaskError('Legacy folder draft cannot become a wiki draft')
            node = self.api.wiki_node(document_id, obj_type='docx')
            pub['wiki_node'] = self.check_wiki_node(node, destination, document_id)
        if self.api.document(document_id).get('title') != pub['title']:
            raise TaskError('Document title does not match the saved creation attempt')
        children = self.api.children(document_id)
        if any(b.get('block_type') != 2 or any(e.get('text_run', {}).get('content')
               for e in b.get('text', {}).get('elements', [])) for b in children):
            raise TaskError('Expected an empty draft; inspect its content before attaching')
        pub['creating'] = False
        self.save_target(record_id, state, pub, document_id=document_id)
        return {'record_id': record_id, 'document_id': document_id, 'attached': True}
