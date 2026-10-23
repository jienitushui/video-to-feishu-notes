from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
import uuid
from pathlib import Path

from .config import Config
from .credentials import load_credentials
from .feishu import Feishu
from .notes import load_note, markdown, validate_note
from .publish import Publisher
from .tasks import Tasks
from .video import prepare, install_upstream
from .local_cleanup import apply_cleanup, canonical_version_dir, plan_cleanup


def parser():
    ap = argparse.ArgumentParser(description='Agent-driven video notes with Feishu task state')
    ap.add_argument('--config', help='Config JSON, or set VFN_CONFIG')
    ap.add_argument('--env-file', help='Credential file, or set VFN_ENV_FILE; defaults to project .env.local')
    sub = ap.add_subparsers(dest='command', required=True)
    doctor = sub.add_parser('doctor', help='Check local configuration; --remote checks table fields and access')
    doctor.add_argument('--remote', action='store_true')
    schema = sub.add_parser('schema', help='Plan missing Bitable fields; --apply creates missing fields')
    schema.add_argument('--apply', action='store_true')
    cleanup = sub.add_parser('cleanup-empty', help='List empty Bitable rows; --apply deletes them')
    cleanup.add_argument('--apply', action='store_true')
    cleanup.add_argument('--limit', type=int, default=100)
    local = sub.add_parser('cleanup-local', help='Preview or delete old local work versions; --apply is required to delete')
    local.add_argument('record_id')
    local.add_argument('--version', type=int, required=True,
                       help='Current completed Feishu generation version; this version is protected')
    local.add_argument('--output-root', default='output')
    local.add_argument('--status', default='已完成',
                       help='Remote task status; cleanup is blocked unless it is 已完成')
    local.add_argument('--include-legacy', action='store_true',
                       help='Include old flat directories such as <record_id>-v2; default is to leave them untouched')
    local.add_argument('--apply', action='store_true')
    tasks = sub.add_parser('tasks')
    tasks.add_argument('--limit', type=int, default=3)
    tasks.add_argument('--category')
    tasks.add_argument('--retry', action='store_true')
    show = sub.add_parser('show', help='Read one task and its recovery checkpoint')
    show.add_argument('record_id')
    wiki = sub.add_parser('wiki-node', help='Resolve a wiki page token to its space, node and document IDs')
    wiki.add_argument('token')
    wiki.add_argument('--obj-type', choices=('wiki', 'docx'), default='wiki')
    enqueue = sub.add_parser('enqueue')
    enqueue.add_argument('url')
    enqueue.add_argument('--enabled', action='store_true')
    enqueue.add_argument('--requirements', default='')
    enqueue.add_argument('--category', default='')
    claim = sub.add_parser('claim')
    claim.add_argument('record_id')
    for option in ('requested', 'retry', 'regenerate', 'resume'):
        claim.add_argument('--' + option, action='store_true')
    for name in ('stage', 'fail', 'bind-source', 'publish', 'cleanup-versions', 'attach-document'):
        cmd = sub.add_parser(name)
        cmd.add_argument('record_id')
        cmd.add_argument('--run-id', required=True)
        if name == 'stage':
            cmd.add_argument('--stage', required=True)
        elif name == 'fail':
            cmd.add_argument('--message', required=True)
            cmd.add_argument('--manual', action='store_true')
        elif name == 'bind-source':
            cmd.add_argument('--manifest', required=True)
        elif name in ('publish', 'cleanup-versions'):
            cmd.add_argument('--note', required=True)
        else:
            cmd.add_argument('--document-id', required=True)
            cmd.add_argument('--target', choices=('folder', 'wiki'),
                             help='Required for attach-document in both mode')
    note = sub.add_parser('validate-note')
    note.add_argument('note')
    export = sub.add_parser('render-markdown')
    export.add_argument('note')
    export.add_argument('--manifest', required=True)
    export.add_argument('--output', required=True)
    video = sub.add_parser('video').add_subparsers(dest='action', required=True)
    install = video.add_parser('install-upstream')
    install.add_argument('--target')
    prep = video.add_parser('prepare')
    prep.add_argument('url')
    prep.add_argument('--output', help='Explicit work directory; when omitted use output/<record_id>/v<version>/')
    prep.add_argument('--record-id', help='Feishu record ID used for the canonical local work directory')
    prep.add_argument('--version', type=int, help='Feishu generation version used for the canonical local work directory')
    prep.add_argument('--output-root', default='output', help='Root for canonical local work directories')
    prep.add_argument('--transcribe', action='store_true')
    prep.add_argument('--max-chunks-per-run', type=int, default=3,
                      help='长视频单次最多新增的 ASR 片段数；0 表示本次处理全部片段')
    prep.add_argument('--screenshots', action=argparse.BooleanOptionalAction, default=True,
                      help='Extract screenshot candidates by default; --no-screenshots for initial source lookup')
    return ap


def execute(args):
    cmd = args.command
    if cmd == 'validate-note':
        note, base = load_note(args.note)
        return validate_note(note, base)
    if cmd == 'render-markdown':
        note, _ = load_note(args.note)
        manifest = json.loads(Path(args.manifest).read_text(encoding='utf-8-sig'))
        if note['source_key'] != manifest['source']['key']:
            raise ValueError('Note and manifest source mismatch')
        out = Path(args.output)
        out.write_text(markdown(note, manifest['source']), encoding='utf-8')
        return {'output': str(out.resolve())}
    if cmd == 'cleanup-local':
        plan = plan_cleanup(args.output_root, args.record_id, args.version, args.status,
                            include_legacy=args.include_legacy)
        return apply_cleanup(plan) if args.apply else plan
    config = Config.load(args.config)
    if cmd == 'video':
        if args.action == 'install-upstream':
            return install_upstream(config, args.target)
        if args.output:
            if args.record_id or args.version:
                raise ValueError('Use --output or --record-id/--version, not both')
            output = args.output
        else:
            if not args.record_id or args.version is None:
                raise ValueError('video prepare requires --output or both --record-id and --version')
            output = str(canonical_version_dir(
                config.local_path('output_root', args.output_root), args.record_id, args.version))
        return prepare(config, args.url, output, args.transcribe, args.screenshots,
                       args.max_chunks_per_run)
    api = Feishu(config)
    tasks = Tasks(api, config)
    if cmd == 'doctor':
        mode = config.get('publish_target', 'folder')
        destination_keys = []
        if mode in ('wiki', 'both'):
            destination_keys.extend(('wiki_space_id', 'wiki_parent_node_token'))
        if mode in ('folder', 'both'):
            destination_keys.append('folder_token')
        configured = all(config.get(k) and not str(config.get(k)).startswith('YOUR_')
                         for k in ('app_token', 'table_id', *destination_keys))
        auth = bool((os.getenv('FEISHU_APP_ID') and os.getenv('FEISHU_APP_SECRET')) or
                    os.getenv('FEISHU_USER_ACCESS_TOKEN') or os.getenv('FEISHU_TENANT_ACCESS_TOKEN'))
        report = {'python': sys.version.split()[0], 'python_executable': sys.executable,
                  'config_ready': configured, 'auth_configured': auth,
                  'publish_target': config.get('publish_target', 'folder'),
                  'upstream_installed': (config.local_path('upstream_path', 'cache/upstream') / 'scripts/bili_fetch.py').is_file(),
                  'optional_modules': {m: importlib.util.find_spec(m) is not None for m in ('av', 'faster_whisper', 'gmssl', 'numpy', 'PIL')},
                  'remote_checked': False}
        if args.remote:
            report['schema'] = tasks.schema()
            report['destination'] = api.check_destination()
            report['remote_checked'] = True
        return report
    if cmd == 'schema':
        return tasks.schema(args.apply)
    if cmd == 'cleanup-empty':
        return tasks.cleanup_empty(args.apply, args.limit)
    if cmd == 'wiki-node':
        return {'node': api.wiki_node(args.token, args.obj_type)}
    if cmd == 'tasks':
        return {'tasks': tasks.select(args.limit, args.category, args.retry)}
    if cmd == 'show':
        record = api.record(args.record_id)
        return {'record': record, 'checkpoint': tasks.state(record)}
    if cmd == 'enqueue':
        from .video import classify
        classify(args.url)
        fields = {'task_name': args.url.strip()[:200], 'url': args.url, 'enabled': args.enabled, 'status': '待处理',
                  'requirements': args.requirements, 'category': args.category}
        token = str(uuid.uuid4())
        record = api.create_record({config.field(k): v for k, v in fields.items()}, token)
        return {'record_id': record['record_id'], 'enabled': args.enabled, 'client_token': token}
    if cmd == 'claim':
        return tasks.claim(args.record_id, requested=args.requested, retry=args.retry,
                           regenerate=args.regenerate, resume=args.resume)
    if cmd == 'stage':
        return tasks.stage(args.record_id, args.run_id, args.stage)
    if cmd == 'fail':
        return tasks.fail(args.record_id, args.run_id, args.message, args.manual)
    if cmd == 'bind-source':
        manifest = json.loads(Path(args.manifest).read_text(encoding='utf-8-sig'))
        return tasks.bind(args.record_id, args.run_id, manifest)
    if cmd == 'publish':
        # Publisher enforces the image requirement for new drafts, preserving frozen recovery plans.
        note, base = load_note(args.note, require_images=False)
        return Publisher(tasks).publish(args.record_id, args.run_id, note, base)
    if cmd == 'cleanup-versions':
        from .cleanup import VersionCleanup
        note, base = load_note(args.note, require_images=False)
        return VersionCleanup(tasks).run(args.record_id, args.run_id, note, base)
    if cmd == 'attach-document':
        return Publisher(tasks).attach(args.record_id, args.run_id, args.document_id, args.target)
    raise ValueError('Unknown command')


def main(argv=None):
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    args = parser().parse_args(argv)
    try:
        load_credentials(args.env_file)
        result = execute(args)
    except Exception as exc:
        print(json.dumps({'ok': False, 'error_type': type(exc).__name__, 'error': str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps({'ok': True, 'data': result}, ensure_ascii=False, indent=2))
    return 0
