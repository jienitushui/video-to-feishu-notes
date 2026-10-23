from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import requests

from .transcription import (ChunkedTranscriptionUnavailable,
                            normalize_language, build_hotwords, atomic_json)
from .transcripts import retain_transcript, verify_material_transcript
from .runtime import ensure_runtime, transcribe_with_runtime

UPSTREAM_URL = 'https://github.com/lucky-Luor/bilibili-douyin-notes.git'
UPSTREAM_REVISION = '2497a7f564670c840f5e1c4055c8a536237cd5f3'


def run(command, cwd=None):
    env = dict(os.environ, PYTHONUTF8='1', PYTHONIOENCODING='utf-8')
    result = subprocess.run(command, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, encoding='utf-8', errors='replace')
    if result.returncode != 0:
        raise RuntimeError('Tool failed: ' + str(command[0]) + '\n' + (result.stderr + result.stdout)[-4000:])
    if result.stderr:
        print(result.stderr[-4000:], file=sys.stderr)
    return result.stdout


def install_upstream(config, target=None):
    path = Path(target).resolve() if target else config.local_path('upstream_path', 'cache/upstream')
    revision = config.get('upstream_revision', UPSTREAM_REVISION)
    if not re.fullmatch(r'[0-9a-f]{40}', revision):
        raise ValueError('upstream_revision must be a full commit SHA')
    if path.exists():
        raise ValueError('Target exists; inspect it rather than overwriting: ' + str(path))
    path.parent.mkdir(parents=True, exist_ok=True)
    run(['git', 'init', str(path)])
    run(['git', '-C', str(path), 'remote', 'add', 'origin', UPSTREAM_URL])
    run(['git', '-C', str(path), 'fetch', '--depth', '1', 'origin', revision])
    run(['git', '-C', str(path), 'checkout', '--detach', 'FETCH_HEAD'])
    actual = run(['git', '-C', str(path), 'rev-parse', 'HEAD']).strip()
    if actual != revision:
        raise RuntimeError('Upstream revision mismatch')
    return {'path': str(path), 'revision': actual,
            'next': 'Run video bootstrap to check or recover the media runtime'}


def classify(source):
    if re.fullmatch(r'BV[0-9A-Za-z]{10}', source.strip()):
        return 'bilibili', source.strip(), 1
    match = re.search(r'https?://[^\s<>]+', source)
    if not match:
        raise ValueError('Expected a Bilibili or Douyin video URL or BV ID')
    url = match.group().rstrip('。，、；;）)】]')
    parts = urlsplit(url)
    host = (parts.hostname or '').lower()
    if host == 'b23.tv' or host == 'bilibili.com' or host.endswith('.bilibili.com'):
        pages = parse_qs(parts.query).get('p', ['1'])
        if len(pages) != 1 or not pages[0].isdigit() or int(pages[0]) < 1:
            raise ValueError('Bilibili p must identify one positive page')
        return 'bilibili', url, int(pages[0])
    if host in ('douyin.com', 'iesdouyin.com') or host.endswith(('.douyin.com', '.iesdouyin.com')):
        return 'douyin', url, 1
    raise ValueError('Only Bilibili and Douyin are enabled in this release')


def safe_part(part):
    return re.sub(r'[\\/:*?"<>|]', '_', part).strip()[:60]


def choose_page(metadata, page):
    matches = [p for p in metadata.get('pages', []) if p['page'] == page]
    if len(matches) != 1:
        raise ValueError('Requested page does not exist: ' + str(page))
    return matches[0]


def resolve_input(request):
    platform, url, page = classify(request)
    if urlsplit(url).hostname == 'b23.tv':
        response = requests.get(url, timeout=(10, 30), headers={'User-Agent': 'Mozilla/5.0'})
        response.raise_for_status()
        resolved_platform, resolved_url, resolved_page = classify(response.url)
        if resolved_platform != 'bilibili' or urlsplit(resolved_url).hostname == 'b23.tv':
            raise ValueError('Bilibili short URL did not resolve to a video')
        page = page if 'p' in parse_qs(urlsplit(url).query) else resolved_page
        url = resolved_url
    return platform, url, page


def prepare(config, request, output, transcribe=False, screenshots=True, max_chunks_per_run=3,
            asr_language=None, hotwords=None):
    if max_chunks_per_run < 0:
        raise ValueError('max_chunks_per_run must be nonnegative')
    language = normalize_language(asr_language if asr_language is not None else config.get('asr_language', 'auto'))
    configured_terms = config.get('asr_hotwords', [])
    if not isinstance(configured_terms, list) or any(not isinstance(t, str) for t in configured_terms):
        raise ValueError('asr_hotwords must be a list of strings')
    terms = configured_terms + (hotwords or [])
    if sys.version_info < (3, 10):
        raise ValueError('Video processing requires Python 3.10 or newer')
    upstream = config.local_path('upstream_path', 'cache/upstream')
    revision = config.get('upstream_revision', UPSTREAM_REVISION)
    if not (upstream / 'scripts/bili_fetch.py').is_file():
        raise ValueError('Upstream is not installed; run video install-upstream')
    actual = run(['git', '-C', str(upstream), 'rev-parse', 'HEAD']).strip()
    if actual != revision:
        raise ValueError('Upstream version differs from configured pin; review before changing upstream_revision')
    platform, url, page = resolve_input(request)
    out = Path(output).resolve()
    out.mkdir(parents=True, exist_ok=True)
    request_file = out / 'request.json'
    identity = {'request': request, 'platform': platform, 'page': page}
    if request_file.exists() and json.loads(request_file.read_text(encoding='utf-8')) != identity:
        raise ValueError('Working directory belongs to a different input; use a separate output directory')
    if not request_file.exists() and any(out.iterdir()):
        raise ValueError('Use an empty working directory, or one created by this command')
    request_file.write_text(json.dumps(identity, ensure_ascii=False, indent=2), encoding='utf-8')
    scripts = upstream / 'scripts'
    full_meta = out / 'metadata.json'
    if not full_meta.exists():
        if platform == 'bilibili':
            helper = Path(__file__).with_name('bili_one.py')
            run([sys.executable, str(helper), str(scripts / 'bili_fetch.py'), url, str(out), str(page)])
        else:
            media = ensure_runtime(config)
            run([media['python'], str(scripts / 'douyin_fetch.py'), url, str(out)])
    metadata = json.loads(full_meta.read_text(encoding='utf-8'))
    selected = choose_page(metadata, page)
    selected_meta = dict(metadata, platform=platform, pages=[selected], duration=selected.get('duration', 0))
    meta_file = out / 'selected-metadata.json'
    meta_file.write_text(json.dumps(selected_meta, ensure_ascii=False, indent=2), encoding='utf-8')
    settings_file = out / 'asr-settings.json'
    if settings_file.exists():
        settings = json.loads(settings_file.read_text(encoding='utf-8'))
        if settings['language'] != language or settings['hotwords'] != build_hotwords(metadata, selected, terms):
            raise ValueError('ASR settings changed; restore language/hotwords or use a new version')
    transcript = out / ('{:02d}_{}.txt'.format(page, safe_part(selected['part'])))
    warnings = []
    previous_manifest = out / 'manifest.json'
    previous = json.loads(previous_manifest.read_text(encoding='utf-8')) if previous_manifest.exists() else {}
    transcription = previous.get('transcription') or {'mode': 'existing_or_upstream', 'status': 'not_requested',
                                                     'max_chunks_per_run': max_chunks_per_run}
    chunk_manifest = out / 'chunk-manifest.json'
    chunk_pending = False
    if chunk_manifest.exists():
        try:
            chunk_state = json.loads(chunk_manifest.read_text(encoding='utf-8'))
            chunk_pending = chunk_state.get('status') != 'prepared'
            transcription = dict(transcription, status=chunk_state['status'],
                                 completed=len(chunk_state.get('completed', [])),
                                 remaining=chunk_state.get('remaining'), manifest=str(chunk_manifest))
        except (OSError, ValueError, TypeError):
            chunk_pending = True
            transcription = dict(transcription, status='transcribing')
    if transcribe and (not transcript.exists() or not transcript.read_text(encoding='utf-8').strip() or chunk_pending):
        # 长内容优先走可恢复的分片 ASR；短内容保留上游路径，避免额外的音频切片开销。
        duration = float(selected.get('duration') or 0)
        if duration > 300:
            try:
                transcription = transcribe_with_runtime(
                    config, meta_file, page, out, scripts / 'bili_transcribe.py',
                    max_chunks_per_run=max_chunks_per_run, language=language, hotwords=terms)
                transcription['mode'] = 'chunked'
            except ChunkedTranscriptionUnavailable as exc:
                if chunk_manifest.exists():
                    raise RuntimeError('Cannot switch an existing chunk plan to whole-file ASR; restore ASR dependencies') from exc
                transcription = transcribe_with_runtime(
                    config, meta_file, page, out, scripts / 'bili_transcribe.py', max_chunks_per_run=max_chunks_per_run,
                    language=language, hotwords=terms, whole_file=True)
                transcription.update(mode='upstream_fallback', reason=str(exc))
                warnings.append('Chunked ASR unavailable; whole-file ASR retained language/hotwords: ' + str(exc))
            except Exception:
                # ASR 本身的错误应保留给调用方处理，不能把部分 checkpoint 误报成完整结果。
                raise
        else:
            transcription = transcribe_with_runtime(
                config, meta_file, page, out, scripts / 'bili_transcribe.py', max_chunks_per_run=max_chunks_per_run,
                language=language, hotwords=terms, whole_file=True)
            transcription['mode'] = 'upstream_whole_file'
    available = transcript.exists() and bool(transcript.read_text(encoding='utf-8').strip())
    if chunk_pending and not transcribe:
        available = False
    if transcription.get('status') in ('transcribing', 'quality_failed'):
        available = False
    if transcribe and not available and transcription.get('status') != 'transcribing':
        raise RuntimeError('Upstream returned without producing a usable transcript; inspect upstream output')
    if not available:
        warnings.append('No usable transcript; rerun with --transcribe after installing ASR dependencies')
    if platform == 'bilibili':
        video_id = metadata['bvid']
        canonical = 'https://www.bilibili.com/video/{}?p={}'.format(video_id, page)
        key = 'bilibili:{}:p{}'.format(video_id, page)
    else:
        video_id = metadata['video_id']
        canonical = 'https://www.douyin.com/video/' + video_id
        key = 'douyin:' + video_id
    source = {'key': key, 'platform': platform, 'video_id': video_id, 'page': page, 'url': canonical,
              'title': metadata['title'] + (' / ' + selected['part'] if page != 1 else ''),
              'owner': metadata.get('owner', ''), 'duration': selected.get('duration', 0)}
    retained = retain_transcript(transcript, out, source, transcription.get('mode')) if available else {}
    warnings.extend(retained.get('transcript_quality', {}).get('warnings', []))
    warnings.extend(transcription.get('quality', {}).get('warnings', []))
    image_manifest = None
    screenshot_status = 'not_requested'
    if screenshots:
        screenshot_status = 'unavailable'
        try:
            helper = Path(__file__).with_name('screenshots.py')
            media = ensure_runtime(config)
            run([media['python'], str(helper), str(scripts / 'bili_screenshot.py'), str(meta_file),
                 retained.get('transcript', str(transcript)), str(out / 'images')])
            candidate = out / 'images/manifest.json'
            if candidate.exists():
                image_manifest = str(candidate)
                frames = json.loads(candidate.read_text(encoding='utf-8')).get('frames', [])
                screenshot_status = 'review_required' if frames else 'unavailable'
                if not frames:
                    warnings.append('No screenshots were produced; inspect the video or explain a text-only result')
                if frames:
                    warnings.append('Review screenshot candidates visually and add relevant image blocks to note.json')
            else:
                warnings.append('Screenshot extraction did not produce a manifest')
        except (RuntimeError, OSError, ValueError) as exc:
            warnings.append('Screenshot extraction failed: ' + str(exc))
    manifest = {'schema_version': 1, 'request': request, 'source': source, 'upstream_revision': actual,
                'metadata': str(meta_file), 'transcript': str(transcript) if available else None,
                'image_manifest': image_manifest, 'screenshot_status': screenshot_status,
                'transcription': transcription, 'warnings': warnings}
    manifest.update(retained)
    if available:
        verify_material_transcript(manifest, out)
    target = out / 'manifest.json'
    atomic_json(target, manifest)
    return dict(manifest, manifest=str(target))
