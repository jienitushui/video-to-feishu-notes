"""Delete superseded generated documents only after the new links are committed."""
from __future__ import annotations

from .notes import plan_note
from .publish import Publisher
from .tasks import TaskError, digest, text


class VersionCleanup:
    def __init__(self, tasks):
        self.tasks, self.api = tasks, tasks.api

    def current(self, record_id, run_id, generation):
        record = self.api.record(record_id)
        state = self.tasks.state(record)
        if (self.tasks.get(record, 'status') != '已完成' or
                self.tasks.get(record, 'run_id') != run_id or state.get('generation') != generation):
            raise TaskError('Latest version is not completed or this cleanup run no longer owns it')
        self.tasks.assert_input(record, state)
        published = state['published']
        for key, value in self.tasks.result_links(published, '').items():
            if text(self.tasks.get(record, key)) != text(value):
                raise TaskError('Latest document links have not been confirmed in the table')
        return record, state

    def save(self, record_id, run_id, generation, state, **values):
        # Never overwrite a newer owner's checkpoint after an external call.
        self.current(record_id, run_id, generation)
        return self.tasks.save_state(record_id, state, **values)

    def run(self, record_id, run_id, note, base):
        record = self.api.record(record_id)
        state = self.tasks.state(record)
        generation = state.get('generation')
        _, state = self.current(record_id, run_id, generation)
        published = state['published']
        if note['source_key'] != state['source']['key']:
            raise TaskError('Cleanup note source differs from the latest publication')
        marker = 'VFN task={} generation={}'.format(record_id, generation)
        plan = plan_note(note, base, state['source'], marker)
        if digest([{k: v for k, v in item.items() if k != 'path'} for item in plan]) != published['hash']:
            raise TaskError('Cleanup requires the unchanged published note and images')
        targets = self.tasks.result_targets(published)
        for kind, target in targets.items():
            checkpoint = state['publish']['targets'][kind] if len(targets) > 1 else state['publish']
            blocks = Publisher(self.tasks).verify_result(target, target['destination'], plan, checkpoint)
            for block, item in zip(blocks, plan):
                if 'path' in item and self.api.image_sha256(block['image']['token']) != item['sha256']:
                    raise TaskError('Latest remote image differs from the published source; preserve old version')
        protected = {target['document_id'] for target in targets.values()}
        warnings = []
        for item in state.get('version_cleanup', []):
            if item.get('status') == 'deleted':
                continue
            self.current(record_id, run_id, generation)
            document_id = item['document_id']
            if document_id in protected:
                raise TaskError('Refusing to delete a current document')
            # Reused documents can still be the successful result of another row.
            shared = []
            for other in self.api.records():
                if other['record_id'] == record_id:
                    continue
                values = [text(self.tasks.get(other, key)) for key in ('document', 'folder_document', 'wiki_document')]
                other_state = self.tasks.state(other)
                references = [other_state.get('published'), other_state.get('publish'),
                              *other_state.get('publish', {}).get('targets', {}).values()]
                if item['url'] in values or text(self.tasks.get(other, 'document_id')) == document_id or any(
                        ref and (ref.get('document_id') == document_id or any(
                            target.get('document_id') == document_id for target in ref.get('targets', {}).values()))
                        for ref in references):
                    shared.append(other['record_id'])
            if shared:
                item.update(status='pending', error='Still referenced by task(s): ' + ', '.join(shared))
                warnings.append(item['error'])
                self.save(record_id, run_id, generation, state, stage='完成，旧版清理待重试')
                continue
            try:
                if not self.api.document_deleted(document_id):
                    if item['destination']['type'] == 'wiki':
                        node = self.api.wiki_node(item['wiki_node_token'])
                        Publisher.check_wiki_node(node, item['destination'], document_id)
                        if node.get('has_child') is not False:
                            raise TaskError('Old wiki page has child pages or child status is unknown; cleanup stopped')
                    item.update(status='deleting', error='')
                    self.save(record_id, run_id, generation, state, stage='完成，正在清理旧版')
                    self.current(record_id, run_id, generation)
                    self.api.delete_document(document_id)
                    if not self.api.document_deleted(document_id):
                        raise TaskError('Old document is still readable after deletion; reread before retrying')
                if item['destination']['type'] == 'wiki' and not self.api.wiki_node_deleted(item['wiki_node_token']):
                    raise TaskError('Old wiki node is still present after document deletion; cleanup is not confirmed')
                item.update(status='deleted', error='')
                self.save(record_id, run_id, generation, state)
            except Exception as exc:
                # An ambiguous DELETE remains recoverable: the next run reads first.
                item['error'] = str(exc)
                warnings.append(str(exc))
                self.save(record_id, run_id, generation, state, stage='完成，旧版清理待重试')
                break
        pending = sum(item.get('status') != 'deleted' for item in state.get('version_cleanup', []))
        self.current(record_id, run_id, generation)
        self.save(record_id, run_id, generation, state, stage='完成，旧版清理待重试' if pending else '完成')
        return {'pending': pending, 'deleted': sum(item.get('status') == 'deleted' for item in state.get('version_cleanup', [])),
                'warnings': warnings}
