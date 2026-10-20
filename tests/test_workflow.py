import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))

from vfnlib.config import Config
from vfnlib.feishu import AmbiguousWrite
from vfnlib.notes import load_note, validate_note
from vfnlib.publish import Publisher
from vfnlib.tasks import Tasks, TaskError
from vfnlib.video import classify, safe_part


class MemoryFeishu:
    """Remote-side state fake, including lost responses after accepted writes."""
    def __init__(self):
        self.rows = {}
        self.docs = {}
        self.tokens = {}
        self.fail_append_at = None
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

    def children(self, document_id):
        return copy.deepcopy(self.docs[document_id]['blocks'])

    def append_block(self, document_id, block, token):
        if token in self.tokens:
            return copy.deepcopy(self.tokens[token])
        blocks = self.docs[document_id]['blocks']
        block = dict(copy.deepcopy(block), block_id='b' + str(len(blocks) + 1))
        blocks.append(block)
        self.tokens[token] = block
        if self.fail_append_at == len(blocks):
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
