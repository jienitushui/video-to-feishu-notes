from __future__ import annotations

"""Metadata-only workload and disk-space report; does not reserve resources."""

import json
import math
import shutil
import sys
from pathlib import Path

from .video import UPSTREAM_REVISION, classify, resolve_input, run


def disk_report(output_root):
    target = Path(output_root).expanduser().resolve()
    probe = target
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    if probe.is_file():
        probe = probe.parent
    try:
        usage = shutil.disk_usage(probe)
        return {'output_root': str(target), 'checked_path': str(probe),
                'free_bytes': usage.free, 'free_gib': round(usage.free / 1024 ** 3, 2),
                'error': None}
    except OSError as exc:
        return {'output_root': str(target), 'checked_path': str(probe),
                'free_bytes': None, 'free_gib': None, 'error': str(exc)}


def valid_duration(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        return None
    return value


def preflight(config, sources, output_root=None):
    if not sources:
        raise ValueError('Preflight requires at least one video')
    disk = disk_report(output_root if output_root is not None else config.local_path('output_root', 'output'))
    upstream = config.local_path('upstream_path', 'cache/upstream')
    setup_error = None
    try:
        if not (upstream / 'scripts/bili_fetch.py').is_file():
            raise ValueError('Upstream is not installed; run video install-upstream')
        revision = run(['git', '-C', str(upstream), 'rev-parse', 'HEAD']).strip()
        if revision != config.get('upstream_revision', UPSTREAM_REVISION):
            raise ValueError('Upstream version differs from configured pin')
    except (OSError, RuntimeError, ValueError) as exc:
        setup_error = str(exc)
    entries = []
    for source in sources:
        entry = {'request': source, 'title': None, 'duration_seconds': None,
                 'duration_minutes': None, 'error': None}
        try:
            platform, url, page = classify(source)
            entry.update(platform=platform, page=page)
            if setup_error:
                raise ValueError(setup_error)
            platform, url, page = resolve_input(source)
            entry.update(platform=platform, page=page)
            helper = Path(__file__).with_name('probe_video.py')
            result = json.loads(run([sys.executable, str(helper), str(upstream / 'scripts'),
                                     platform, url, str(page)]))
            duration = valid_duration(result.get('duration_seconds'))
            entry.update(title=result.get('title'), source_key=result['source_key'],
                         duration_seconds=duration,
                         duration_minutes=round(duration / 60, 2) if duration is not None else None)
            if duration is None:
                entry['error'] = 'Video duration is unavailable'
        except (OSError, RuntimeError, ValueError, KeyError, TypeError) as exc:
            entry['error'] = str(exc)
        entries.append(entry)
    known = sum(e['duration_seconds'] for e in entries if e['duration_seconds'] is not None)
    unknown = sum(e['duration_seconds'] is None for e in entries)
    return {'videos': entries, 'video_count': len(entries), 'unknown_duration_count': unknown,
            'known_total_seconds': known, 'known_total_minutes': round(known / 60, 2),
            'total_seconds': known if unknown == 0 else None,
            'total_minutes': round(known / 60, 2) if unknown == 0 else None,
            'disk': disk, 'complete': unknown == 0 and disk['free_bytes'] is not None}
