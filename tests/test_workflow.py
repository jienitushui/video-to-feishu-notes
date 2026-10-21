import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))

from vfnlib.config import Config
from vfnlib.feishu import AmbiguousWrite, FeishuError
from vfnlib.notes import load_note, validate_note
from vfnlib.publish import Publisher
from vfnlib.tasks import Tasks, TaskError
from vfnlib.video import classify, safe_part


class MemoryFeishu:
    """Remote-side state fake, including lost responses after accepted writes."""
    def __init__(self):
        self.rows = {}
        self.docs = {}
        self.wiki_nodes = {}
        self.tokens = {}
        self.fail_append_at = None
        self.fail_append_document = None
        self.fail_complete = False
        self.ambiguous_create = False
        self.lose_checkpoint = False
        self.image_uploads = 0

    def add(self, record_id, url='https://www.bilibili.com/video/BV1Wv411h7kN?p=19', **fields):
        self.rows[record_id] = {'record_id': record_id, 'fields': {
            '视频链接': url, '开始生成': True, '处理状态': '待处理', **fields}}

    def records(self):
        return copy.deepcopy(list(self.rows.values()))

    def record(self, record_id):
        return copy.deepcopy(self.rows[record_id])

    def update_record(self, record_id, fields):
        if self.fail_complete and fields.get('处理状态') == '已完成':
            self.fail_complete = False
            raise RuntimeError('completion update rejected')
        if self.lose_checkpoint and fields.get('工作文档ID'):
            self.lose_checkpoint = False
            raise RuntimeError('checkpoint lost')
        self.rows[record_id]['fields'].update(copy.deepcopy(fields))
        return self.record(record_id)

    def create_document(self, title):
        did = 'doc' + str(len(self.docs) + 1)
        self.docs[did] = {'document_id': did, 'title': title, 'blocks': []}
        if self.ambiguous_create:
            self.ambiguous_create = False
            raise AmbiguousWrite('connection lost after document creation')
        return {'document_id': did, 'title': title}

    def document(self, document_id):
        document = self.docs[document_id]
        return {'document_id': document_id, 'title': document['title']}

    def create_wiki_node(self, title):
        ambiguous, self.ambiguous_create = self.ambiguous_create, False
        document = self.create_document(title)
        token = 'wiki' + str(len(self.wiki_nodes) + 1)
        node = {'node_token': token, 'obj_token': document['document_id'], 'title': title,
                'obj_type': 'docx', 'node_type': 'origin', 'space_id': '123',
                'parent_node_token': 'parent'}
        self.wiki_nodes[token] = node
        if ambiguous:
            raise AmbiguousWrite('connection lost after wiki creation')
        return copy.deepcopy(node)

    def wiki_node(self, token, obj_type='wiki'):
        if obj_type == 'docx':
            return copy.deepcopy(next(n for n in self.wiki_nodes.values() if n['obj_token'] == token))
        return copy.deepcopy(self.wiki_nodes[token])

    def children(self, document_id):
        return copy.deepcopy(self.docs[document_id]['blocks'])

    def append_block(self, document_id, block, token):
        if token in self.tokens:
            return copy.deepcopy(self.tokens[token])
        blocks = self.docs[document_id]['blocks']
        block = dict(copy.deepcopy(block), block_id='b' + str(len(blocks) + 1))
        blocks.append(block)
        self.tokens[token] = block
        if self.fail_append_at == len(blocks) and self.fail_append_document in (None, document_id):
            self.fail_append_at = None
            raise AmbiguousWrite('append accepted but response lost')
        return copy.deepcopy(block)

    def upload_image(self, document_id, block_id, path):
        for block in self.docs[document_id]['blocks']:
            if block['block_id'] == block_id:
                block['image']['token'] = 'img-token'
        self.image_uploads += 1
        return 'img-token'


SOURCE = {'key': 'bilibili:BV1Wv411h7kN:p19', 'platform': 'bilibili',
          'title': 'Test lecture', 'url': 'https://www.bilibili.com/video/BV1Wv411h7kN?p=19', 'duration': 2025}


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)
        self.config = Config({'folder_token': 'folder', 'document_url_prefix': 'https://example.feishu.cn/docx/'})
        self.api = MemoryFeishu()
        self.api.add('rec1')
        self.tasks = Tasks(self.api, self.config)
        self.publisher = Publisher(self.tasks)
        self.note = {'schema_version': 1, 'title': 'Test', 'summary': 'Summary', 'source_key': SOURCE['key'],
                     'sections': [{'id': 'intro', 'title': 'Introduction',
                                   'blocks': [{'type': 'text', 'text': 'Explanation', 'seconds': 3}]}]}

    def bound(self, record='rec1', **kwargs):
        claim = self.tasks.claim(record, **kwargs)
        run = claim['run_id']
        self.tasks.bind(record, run, {'request': self.api.rows[record]['fields']['视频链接'], 'source': SOURCE})
        return run

    def publish(self, run, record='rec1'):
        return self.publisher.publish(record, run, self.note, self.base)

    def use_wiki(self):
        self.config.data.update(publish_target='wiki', wiki_space_id='123',
                                wiki_parent_node_token='parent', wiki_url_prefix='https://example.feishu.cn/wiki/')
        self.config.data.pop('folder_token', None)

    def use_both(self):
        self.use_wiki()
        self.config.data.update(publish_target='both', folder_token='folder')

    def test_both_creates_two_independent_copies_and_returns_both_links(self):
        self.use_both()
        result = self.publish(self.bound())
        self.assertEqual(len(self.api.docs), 2)
        self.assertEqual(result['targets']['folder']['document_id'], 'doc1')
        self.assertEqual(result['targets']['wiki']['document_id'], 'doc2')
        self.assertEqual(result['url'], result['targets']['wiki']['url'])
        self.assertEqual(result['targets']['folder']['hash'], result['targets']['wiki']['hash'])
        self.assertEqual(self.api.docs['doc1']['blocks'], self.api.docs['doc2']['blocks'])
        fields = self.api.rows['rec1']['fields']
        self.assertEqual(fields['云盘笔记']['link'], result['targets']['folder']['url'])
        self.assertEqual(fields['知识库笔记']['link'], result['targets']['wiki']['url'])
        self.assertEqual(fields['工作文档ID'], 'doc1')
        self.assertEqual(fields['处理状态'], '已完成')

    def test_both_wiki_failure_preserves_folder_and_resumes_only_pending_target(self):
        from unittest.mock import patch
        self.use_both()
        run = self.bound()
        with patch.object(self.api, 'create_wiki_node', side_effect=FeishuError('permission denied')):
            with self.assertRaises(FeishuError):
                self.publish(run)
        state = self.tasks.state(self.api.record('rec1'))
        self.assertIn('published', state['publish']['targets']['folder'])
        self.assertEqual(self.tasks.get(self.api.record('rec1'), 'status'), '处理中')
        self.assertIsNone(self.tasks.get(self.api.record('rec1'), 'document'))
        self.tasks.fail('rec1', run, 'permission denied', manual=True)
        run = self.tasks.claim('rec1', retry=True)['run_id']
        append = self.api.append_block

        def append_pending(document_id, block, token):
            self.assertNotEqual(document_id, 'doc1', 'completed folder copy must not be appended again')
            return append(document_id, block, token)

        with patch.object(self.api, 'append_block', side_effect=append_pending):
            self.publish(run)
        self.assertEqual(len(self.api.docs), 2)

    def test_both_lost_wiki_append_resumes_without_duplicate_blocks(self):
        self.use_both()
        run = self.bound()
        self.api.fail_append_at, self.api.fail_append_document = 3, 'doc2'
        with self.assertRaises(AmbiguousWrite):
            self.publish(run)
        self.publish(self.tasks.claim('rec1', resume=True)['run_id'])
        self.assertEqual([len(d['blocks']) for d in self.api.docs.values()], [7, 7])

    def test_both_unknown_wiki_creation_requires_target_specific_attachment(self):
        from unittest.mock import patch
        self.use_both()
        run = self.bound()
        create = self.api.create_wiki_node

        def lost(title):
            create(title)
            raise AmbiguousWrite('wiki node accepted but response lost')

        with patch.object(self.api, 'create_wiki_node', side_effect=lost):
            with self.assertRaises(AmbiguousWrite):
                self.publish(run)
        with self.assertRaisesRegex(TaskError, 'creation outcome is unknown'):
            self.publish(run)
        with self.assertRaisesRegex(TaskError, 'requires --target'):
            self.publisher.attach('rec1', run, 'doc2')
        with self.assertRaisesRegex(TaskError, 'already attached'):
            self.publisher.attach('rec1', run, 'doc2', target='folder')
        self.publisher.attach('rec1', run, 'doc2', target='wiki')
        self.publish(run)
        self.assertEqual(len(self.api.docs), 2)

    def test_both_unknown_folder_creation_attaches_before_creating_wiki(self):
        self.use_both()
        run = self.bound()
        self.api.ambiguous_create = True
        with self.assertRaises(AmbiguousWrite):
            self.publish(run)
        self.assertFalse(self.api.wiki_nodes)
        self.publisher.attach('rec1', run, 'doc1', target='folder')
        self.publish(run)
        self.assertEqual(len(self.api.docs), 2)

    def test_both_wiki_checkpoint_failure_recovers_without_new_document(self):
        from unittest.mock import patch
        self.use_both()
        run = self.bound()
        update = self.api.update_record

        def lose_wiki_checkpoint(record_id, fields):
            state = json.loads(fields.get('任务检查点', '{}'))
            if state.get('publish', {}).get('targets', {}).get('wiki', {}).get('document_id'):
                raise RuntimeError('wiki checkpoint rejected')
            return update(record_id, fields)

        with patch.object(self.api, 'update_record', side_effect=lose_wiki_checkpoint):
            with self.assertRaisesRegex(TaskError, 'doc2'):
                self.publish(run)
        self.publisher.attach('rec1', run, 'doc2', target='wiki')
        self.publish(run)
        self.assertEqual(len(self.api.docs), 2)

    def test_both_images_and_final_table_update_resume_without_new_uploads(self):
        self.use_both()
        (self.base / 'frame.png').write_bytes(b'image fixture')
        self.note['sections'][0]['blocks'].append({'type': 'image', 'path': 'frame.png',
                                                  'caption': 'A frame', 'seconds': 5})
        run = self.bound()
        self.api.fail_complete = True
        with self.assertRaises(RuntimeError):
            self.publish(run)
        self.assertEqual(self.api.image_uploads, 2)
        self.publish(run)
        self.assertEqual(self.api.image_uploads, 2)
        self.assertEqual(len(self.api.docs), 2)

    def test_both_reuse_requires_both_accessible_documents(self):
        self.use_both()
        self.publish(self.bound())
        self.api.add('rec2')
        self.bound('rec2')
        self.assertEqual(self.tasks.get(self.api.record('rec2'), 'status'), '已完成')
        for field in ('云盘笔记', '知识库笔记'):
            self.assertEqual(self.api.rows['rec1']['fields'][field]['link'], self.api.rows['rec2']['fields'][field]['link'])
        del self.api.docs['doc1']
        self.api.add('rec3')
        self.bound('rec3')
        self.assertEqual(self.tasks.get(self.api.record('rec3'), 'status'), '处理中')

    def test_both_regeneration_keeps_both_previous_links_until_complete(self):
        self.use_both()
        self.publish(self.bound())
        old = copy.deepcopy(self.api.rows['rec1']['fields'])
        run = self.bound(regenerate=True)
        self.api.fail_append_at, self.api.fail_append_document = 2, 'doc4'
        with self.assertRaises(AmbiguousWrite):
            self.publish(run)
        for field in ('笔记文档', '云盘笔记', '知识库笔记'):
            self.assertEqual(self.api.rows['rec1']['fields'][field], old[field])
        self.publish(run)
        for field in ('笔记文档', '云盘笔记', '知识库笔记'):
            self.assertNotEqual(self.api.rows['rec1']['fields'][field], old[field])
        self.assertEqual(len(self.api.docs), 4)

    def test_both_changed_config_or_notes_preserve_completed_copy(self):
        from unittest.mock import patch
        self.use_both()
        run = self.bound()
        with patch.object(self.api, 'create_wiki_node', side_effect=FeishuError('temporarily unavailable')):
            with self.assertRaises(FeishuError):
                self.publish(run)
        self.config.data['folder_token'] = 'other-folder'
        with self.assertRaisesRegex(TaskError, 'destination changed'):
            self.publish(run)
        self.config.data['folder_token'] = 'folder'
        self.note['summary'] = 'Changed'
        with self.assertRaisesRegex(TaskError, 'content or images changed'):
            self.publish(run)
        self.assertEqual(len(self.api.docs), 1)

    def test_both_completed_folder_edited_during_wiki_publication_blocks_completion(self):
        from unittest.mock import patch
        self.use_both()
        run = self.bound()
        create = self.api.create_wiki_node

        def edit_folder(title):
            self.api.docs['doc1']['blocks'][0]['text']['elements'][0]['text_run']['content'] = 'manual edit'
            return create(title)

        with patch.object(self.api, 'create_wiki_node', side_effect=edit_folder):
            with self.assertRaisesRegex(TaskError, 'verification failed'):
                self.publish(run)
        self.assertEqual(self.tasks.get(self.api.record('rec1'), 'status'), '处理中')

    def test_wiki_publication_returns_node_link_and_reuses_result(self):
        self.use_wiki()
        result = self.publish(self.bound())
        self.assertEqual(result['url'], 'https://example.feishu.cn/wiki/wiki1')
        self.assertEqual(result['document_id'], 'doc1')
        self.assertEqual(result['wiki_node_token'], 'wiki1')
        self.assertEqual(self.api.rows['rec1']['fields']['笔记文档']['link'], result['url'])
        self.api.add('rec2')
        self.bound('rec2')
        state = self.tasks.state(self.api.record('rec2'))
        self.assertEqual(state['published']['reused_from'], 'rec1')
        self.assertEqual(len(self.api.wiki_nodes), 1)

    def test_wiki_resumes_append_and_failed_completion_without_new_node(self):
        self.use_wiki()
        run = self.bound()
        self.api.fail_append_at = 3
        with self.assertRaises(AmbiguousWrite):
            self.publish(run)
        run = self.tasks.claim('rec1', resume=True)['run_id']
        self.api.fail_complete = True
        with self.assertRaises(RuntimeError):
            self.publish(run)
        result = self.publish(run)
        self.assertEqual(result['blocks'], 7)
        self.assertEqual(len(self.api.wiki_nodes), 1)

    def test_wiki_unknown_creation_requires_verified_attachment(self):
        self.use_wiki()
        run = self.bound()
        self.api.ambiguous_create = True
        with self.assertRaises(AmbiguousWrite):
            self.publish(run)
        with self.assertRaisesRegex(TaskError, 'creation outcome is unknown'):
            self.publish(run)
        self.api.wiki_nodes['wiki1']['parent_node_token'] = 'wrong-parent'
        with self.assertRaisesRegex(TaskError, 'Wiki node does not match'):
            self.publisher.attach('rec1', run, 'doc1')
        self.api.wiki_nodes['wiki1']['parent_node_token'] = 'parent'
        self.publisher.attach('rec1', run, 'doc1')
        self.publish(run)
        self.assertEqual(len(self.api.wiki_nodes), 1)

    def test_wiki_checkpoint_failure_can_attach_existing_node(self):
        self.use_wiki()
        run = self.bound()
        self.api.lose_checkpoint = True
        with self.assertRaisesRegex(TaskError, 'doc1'):
            self.publish(run)
        self.publisher.attach('rec1', run, 'doc1')
        self.publish(run)
        self.assertEqual(len(self.api.wiki_nodes), 1)

    def test_malformed_wiki_creation_response_keeps_guard(self):
        from unittest.mock import patch
        self.use_wiki()
        run = self.bound()
        original = self.api.create_wiki_node

        def malformed(title):
            node = original(title)
            node.pop('obj_token')
            return node

        with patch.object(self.api, 'create_wiki_node', side_effect=malformed):
            with self.assertRaisesRegex(TaskError, 'Wiki node does not match'):
                self.publish(run)
        with self.assertRaisesRegex(TaskError, 'creation outcome is unknown'):
            self.publish(run)
        self.publisher.attach('rec1', run, 'doc1')
        self.publish(run)
        self.assertEqual(len(self.api.wiki_nodes), 1)

    def test_wiki_moved_during_publication_cannot_be_marked_complete(self):
        from unittest.mock import patch
        self.use_wiki()
        run = self.bound()
        original = self.api.document

        def moved(document_id):
            self.api.wiki_nodes['wiki1']['parent_node_token'] = 'moved'
            return original(document_id)

        with patch.object(self.api, 'document', side_effect=moved):
            with self.assertRaisesRegex(TaskError, 'Wiki node does not match'):
                self.publish(run)
        row = self.api.record('rec1')
        self.assertEqual(self.tasks.get(row, 'status'), '处理中')
        self.assertIsNone(self.tasks.get(row, 'document'))

    def test_wiki_regeneration_keeps_old_link_until_new_version_completes(self):
        self.use_wiki()
        old = self.publish(self.bound())['url']
        run = self.bound(regenerate=True)
        self.api.fail_append_at = 2
        with self.assertRaises(AmbiguousWrite):
            self.publish(run)
        self.assertEqual(self.api.rows['rec1']['fields']['笔记文档']['link'], old)
        result = self.publish(run)
        self.assertNotEqual(old, result['url'])
        self.assertEqual(len(self.api.wiki_nodes), 2)

    def test_changed_destination_stops_resume(self):
        self.use_wiki()
        run = self.bound()
        self.api.fail_append_at = 2
        with self.assertRaises(AmbiguousWrite):
            self.publish(run)
        self.config.data['wiki_parent_node_token'] = 'other'
        with self.assertRaisesRegex(TaskError, 'destination changed'):
            self.publish(run)
        self.assertEqual(len(self.api.wiki_nodes), 1)

    def test_moved_wiki_node_blocks_resume_and_reuse(self):
        self.use_wiki()
        self.publish(self.bound())
        self.api.wiki_nodes['wiki1']['parent_node_token'] = 'moved'
        self.api.add('rec2')
        run = self.bound('rec2')
        self.assertEqual(self.tasks.get(self.api.record('rec2'), 'status'), '处理中')
        self.api.fail_append_at = 2
        with self.assertRaises(AmbiguousWrite):
            self.publish(run, 'rec2')
        self.api.wiki_nodes['wiki2']['obj_token'] = 'another-doc'
        with self.assertRaisesRegex(TaskError, 'Wiki node does not match'):
            self.publish(run, 'rec2')

    def test_folder_result_not_reused_for_wiki_destination(self):
        self.publish(self.bound())
        self.use_wiki()
        self.api.add('rec2')
        run = self.bound('rec2')
        self.assertEqual(self.tasks.get(self.api.record('rec2'), 'status'), '处理中')
        result = self.publish(run, 'rec2')
        self.assertEqual(result['document_id'], 'doc2')
        self.assertEqual(len(self.api.wiki_nodes), 1)

    def test_legacy_folder_draft_not_adopted_by_wiki(self):
        run = self.bound()
        self.api.fail_append_at = 2
        with self.assertRaises(AmbiguousWrite):
            self.publish(run)
        state = self.tasks.state(self.api.record('rec1'))
        del state['publish']['destination']
        self.tasks.save_state('rec1', state)
        self.use_wiki()
        with self.assertRaisesRegex(TaskError, 'Legacy folder draft'):
            self.publish(run)
        self.assertFalse(self.api.wiki_nodes)

    def test_publish_and_skip_completed(self):
        result = self.publish(self.bound())
        self.assertEqual(result['status'], '已完成')
        self.assertEqual(len(self.api.docs), 1)
        self.assertEqual(self.tasks.select(), [])
        self.assertEqual(self.api.rows['rec1']['fields']['笔记文档']['link'], result['url'])

    def test_duplicate_share_url_reuses_same_source(self):
        self.publish(self.bound())
        self.api.add('rec2', url='https://b23.tv/different-share')
        run = self.tasks.claim('rec2')['run_id']
        result = self.tasks.bind('rec2', run, {'request': 'https://b23.tv/different-share', 'source': SOURCE})
        self.assertTrue(result['reused'])
        self.assertEqual(len(self.api.docs), 1)
        self.assertEqual(self.api.rows['rec2']['fields']['处理状态'], '已完成')

    def test_different_requirements_not_reused(self):
        self.publish(self.bound())
        self.api.add('rec2', 笔记要求='Explain code')
        run = self.tasks.claim('rec2')['run_id']
        result = self.tasks.bind('rec2', run, {'request': SOURCE['url'], 'source': SOURCE})
        self.assertFalse(result['reused'])

    def test_lost_append_response_resumes_without_duplicates(self):
        run = self.bound()
        self.api.fail_append_at = 3
        with self.assertRaises(AmbiguousWrite):
            self.publish(run)
        run = self.tasks.claim('rec1', resume=True)['run_id']
        result = self.publish(run)
        self.assertEqual(len(self.api.docs), 1)
        self.assertEqual(result['blocks'], 7)

    def test_complete_document_with_failed_table_update(self):
        run = self.bound()
        self.api.fail_complete = True
        with self.assertRaises(RuntimeError):
            self.publish(run)
        blocks = len(self.api.docs['doc1']['blocks'])
        result = self.publish(run)
        self.assertEqual(result['blocks'], blocks)
        self.assertEqual(len(self.api.docs), 1)

    def test_ambiguous_document_create_requires_attachment(self):
        run = self.bound()
        self.api.ambiguous_create = True
        with self.assertRaises(AmbiguousWrite):
            self.publish(run)
        with self.assertRaisesRegex(TaskError, 'creation outcome is unknown'):
            self.publish(run)
        self.assertEqual(len(self.api.docs), 1)
        self.publisher.attach('rec1', run, 'doc1')
        self.publish(run)
        self.assertEqual(len(self.api.docs), 1)

    def test_created_document_id_checkpoint_failure_is_recoverable(self):
        run = self.bound()
        self.api.lose_checkpoint = True
        with self.assertRaisesRegex(TaskError, 'doc1'):
            self.publish(run)
        self.publisher.attach('rec1', run, 'doc1')
        self.publish(run)
        self.assertEqual(len(self.api.docs), 1)

    def test_regeneration_preserves_old_published_link(self):
        old = self.publish(self.bound())['url']
        run = self.bound(regenerate=True)
        self.assertEqual(self.api.rows['rec1']['fields']['笔记文档']['link'], old)
        self.api.fail_append_at = 2
        with self.assertRaises(AmbiguousWrite):
            self.publish(run)
        self.assertEqual(self.api.rows['rec1']['fields']['笔记文档']['link'], old)
        result = self.publish(run)
        self.assertNotEqual(old, result['url'])
        self.assertEqual(len(self.api.docs), 2)

    def test_input_change_rejected_and_old_run_cannot_write(self):
        run = self.bound()
        self.api.rows['rec1']['fields']['笔记要求'] = 'New requirements'
        with self.assertRaisesRegex(TaskError, 'input changed'):
            self.publish(run)
        self.assertEqual(self.api.docs, {})
        self.api.rows['rec1']['fields']['笔记要求'] = ''
        new_run = self.tasks.claim('rec1', resume=True)['run_id']
        self.assertNotEqual(run, new_run)
        with self.assertRaises(TaskError):
            self.tasks.stage('rec1', run, 'invalid owner')

    def test_remote_draft_edit_stops_append(self):
        run = self.bound()
        self.api.fail_append_at = 3
        with self.assertRaises(AmbiguousWrite):
            self.publish(run)
        self.api.docs['doc1']['blocks'][0]['text']['elements'][0]['text_run']['content'] = 'edited'
        with self.assertRaisesRegex(TaskError, 'differs'):
            self.publish(run)
        self.assertEqual(len(self.api.docs['doc1']['blocks']), 3)

    def test_retry_keeps_draft_even_when_regenerate_checkbox_is_still_true(self):
        self.publish(self.bound())
        self.api.rows['rec1']['fields']['重新生成'] = True
        run = self.bound()
        self.api.fail_append_at = 2
        with self.assertRaises(AmbiguousWrite):
            self.publish(run)
        before = self.tasks.state(self.api.record('rec1'))
        self.tasks.fail('rec1', run, 'transient failure')
        resumed = self.tasks.claim('rec1', retry=True)
        self.assertEqual(resumed['state']['generation'], before['generation'])
        self.assertEqual(self.api.rows['rec1']['fields']['工作文档ID'], 'doc2')
        self.publish(resumed['run_id'])
        self.assertEqual(len(self.api.docs), 2)

    def test_unknown_create_error_keeps_creation_guard(self):
        run = self.bound()
        from unittest.mock import patch
        with patch.object(self.api, 'create_document', side_effect=KeyError('invalid response')):
            with self.assertRaises(KeyError):
                self.publish(run)
        self.assertTrue(self.tasks.state(self.api.record('rec1'))['publish']['creating'])
        with self.assertRaisesRegex(TaskError, 'creation outcome is unknown'):
            self.publish(run)

    def test_image_upload_resume_and_note_directory_relocation(self):
        (self.base / 'frame.png').write_bytes(b'image fixture')
        self.note['sections'][0]['blocks'].append({'type': 'image', 'path': 'frame.png',
                                                  'caption': 'A frame', 'seconds': 5})
        run = self.bound()
        self.api.fail_complete = True
        with self.assertRaises(RuntimeError):
            self.publish(run)
        relocated = self.base / 'another-device'
        relocated.mkdir()
        (relocated / 'frame.png').write_bytes(b'image fixture')
        self.publisher.publish('rec1', run, self.note, relocated)
        self.assertEqual(self.api.image_uploads, 1)

    def test_invalid_images_and_nonfinite_timestamps_rejected(self):
        block = {'type': 'image', 'path': '../outside.png', 'caption': 'x', 'seconds': 1}
        self.note['sections'][0]['blocks'] = [block]
        with self.assertRaises(ValueError):
            validate_note(self.note, self.base)
        self.note['sections'][0]['blocks'] = [{'type': 'text', 'text': 'x', 'seconds': float('nan')}]
        with self.assertRaises(ValueError):
            validate_note(self.note, self.base)

    def test_task_selection_failure_disabled_and_limit(self):
        self.api.add('rec2', 处理状态='失败')
        self.api.add('rec3', 开始生成=False)
        self.api.add('rec4')
        self.assertEqual([r['record_id'] for r in self.tasks.select()], ['rec1', 'rec4'])
        self.assertEqual(len(self.tasks.select(limit=1, retry=True)), 1)
        self.assertIn('rec2', [r['record_id'] for r in self.tasks.select(retry=True)])

    def test_page_classification_and_safe_filename_match_upstream(self):
        self.assertEqual(classify(SOURCE['url'])[2], 19)
        self.assertEqual(classify('分享 https://v.douyin.com/test/。')[0], 'douyin')
        with self.assertRaises(ValueError):
            classify('https://bilibili.com.evil.example/video/BV123')
        self.assertEqual(len(safe_part('a' * 100)), 60)


if __name__ == '__main__':
    unittest.main()
