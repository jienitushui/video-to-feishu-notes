from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import requests

from .transcription import ChunkedTranscriptionUnavailable, transcribe_page_chunked

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
            'next': 'Install requirements from the upstream checkout with the same Python interpreter'}


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


def prepare(config, request, output, transcribe=False, screenshots=True, max_chunks_per_run=3):
    if sys.version_info < (3, 10):
        raise ValueError('Video processing requires Python 3.10 or newer')
    upstream = config.local_path('upstream_path', 'cache/upstream')
    revision = config.get('upstream_revision', UPSTREAM_REVISION)
    if not (upstream / 'scripts/bili_fetch.py').is_file():
        raise ValueError('Upstream is not installed; run video install-upstream')
    actual = run(['git', '-C', str(upstream), 'rev-parse', 'HEAD']).strip()
    if actual != revision:
        raise ValueError('Upstream version differs from configured pin; review before changing upstream_revision')
    platform, url, page = classify(request)
    if urlsplit(url).hostname == 'b23.tv':
        response = requests.get(url, timeout=(10, 30), headers={'User-Agent': 'Mozilla/5.0'})
        response.raise_for_status()
        resolved_platform, resolved_url, resolved_page = classify(response.url)
        if resolved_platform != 'bilibili' or urlsplit(resolved_url).hostname == 'b23.tv':
            raise ValueError('Bilibili short URL did not resolve to a video')
        # A page explicitly present on the short URL takes precedence.
        page = page if 'p' in parse_qs(urlsplit(url).query) else resolved_page
        url = resolved_url
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
            run([sys.executable, str(scripts / 'douyin_fetch.py'), url, str(out)])
    metadata = json.loads(full_meta.read_text(encoding='utf-8'))
    selected = choose_page(metadata, page)
    selected_meta = dict(metadata, platform=platform, pages=[selected], duration=selected.get('duration', 0))
    meta_file = out / 'selected-metadata.json'
    meta_file.write_text(json.dumps(selected_meta, ensure_ascii=False, indent=2), encoding='utf-8')
    transcript = out / ('{:02d}_{}.txt'.format(page, safe_part(selected['part'])))
    warnings = []
    transcription = {'mode': 'existing_or_upstream', 'status': 'not_requested',
                     'max_chunks_per_run': max_chunks_per_run}
    chunk_manifest = out / 'chunk-manifest.json'
    chunk_pending = False
    if chunk_manifest.exists():
        try:
            chunk_pending = json.loads(chunk_manifest.read_text(encoding='utf-8')).get('status') != 'prepared'
        except (OSError, ValueError, TypeError):
            chunk_pending = True
    if transcribe and (not transcript.exists() or not transcript.read_text(encoding='utf-8').strip() or chunk_pending):
        # 长内容优先走可恢复的分片 ASR；短内容保留上游路径，避免额外的音频切片开销。
        duration = float(selected.get('duration') or 0)
        if duration > 300 and max_chunks_per_run >= 0:
            try:
                transcription = transcribe_page_chunked(
                    meta_file, page, out, scripts / 'bili_transcribe.py',
                    max_chunks_per_run=max_chunks_per_run)
                transcription['mode'] = 'chunked'
            except ChunkedTranscriptionUnavailable as exc:
                transcription = {'mode': 'upstream_fallback', 'status': 'fallback',
                                 'reason': str(exc), 'max_chunks_per_run': max_chunks_per_run}
                warnings.append('Chunked ASR unavailable; fell back to upstream whole-file transcription: ' + str(exc))
                run([sys.executable, str(scripts / 'bili_transcribe.py'), str(meta_file), '--page', str(page)])
            except Exception:
                # ASR 本身的错误应保留给调用方处理，不能把部分 checkpoint 误报成完整结果。
                raise
        else:
            run([sys.executable, str(scripts / 'bili_transcribe.py'), str(meta_file), '--page', str(page)])
            transcription['mode'] = 'upstream_whole_file'
            transcription['status'] = 'completed'
    available = transcript.exists() and bool(transcript.read_text(encoding='utf-8').strip())
    if transcription.get('mode') == 'chunked' and transcription.get('status') != 'prepared':
        available = False
    if transcribe and not available and not (transcription.get('mode') == 'chunked' and
                                             transcription.get('status') == 'transcribing'):
        raise RuntimeError('Upstream returned without producing a usable transcript; inspect upstream output')
    if not available:
        warnings.append('No usable transcript; rerun with --transcribe after installing ASR dependencies')
    image_manifest = None
    screenshot_status = 'not_requested'
    if screenshots:
        screenshot_status = 'unavailable'
        try:
            helper = Path(__file__).with_name('screenshots.py')
            run([sys.executable, str(helper), str(scripts / 'bili_screenshot.py'), str(meta_file),
                 str(transcript), str(out / 'images')])
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
    manifest = {'schema_version': 1, 'request': request, 'source': source, 'upstream_revision': actual,
                'metadata': str(meta_file), 'transcript': str(transcript) if available else None,
                'image_manifest': image_manifest, 'screenshot_status': screenshot_status,
                'transcription': transcription, 'warnings': warnings}
    target = out / 'manifest.json'
    target.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    return dict(manifest, manifest=str(target))
