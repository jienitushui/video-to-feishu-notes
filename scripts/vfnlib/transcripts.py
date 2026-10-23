from __future__ import annotations

"""Local transcript provenance, structural QC and optional text corrections.

The upstream text contains start times only. Do not invent segment end times or
claim that structural checks prove recognition accuracy.
"""

import hashlib
import json
import math
import re
from pathlib import Path

from .transcription import atomic_json, atomic_write

LINE = re.compile(r'^\[(\d+):(\d{2})(?::(\d{2}))?\]\s*(.*)$')


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def parse_transcript(text):
    segments, failures = [], []
    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        match = LINE.fullmatch(line.strip())
        if not match:
            failures.append(f'unparseable_line:{number}')
            continue
        first, second, third, content = match.groups()
        if int(second) >= 60 or (third is not None and int(third) >= 60):
            failures.append(f'invalid_timestamp:{number}')
            continue
        start = int(first) * 60 + int(second)
        if third is not None:
            start = int(first) * 3600 + int(second) * 60 + int(third)
        segments.append({'id': len(segments) + 1, 'start': start, 'text': content.strip()})
    return segments, failures


def quality_report(segments, duration=None):
    failures, warnings = [], []
    if not isinstance(segments, list) or not segments:
        return {'passed': False, 'hard_failures': ['empty_output'], 'warnings': [],
                'segment_count': 0, 'character_count': 0}
    previous = -1
    chars, empty, repeated = 0, 0, 0
    for index, segment in enumerate(segments, 1):
        if not isinstance(segment, dict):
            failures.append('invalid_segment')
            continue
        start, text = segment.get('start'), segment.get('text')
        if segment.get('id') != index or isinstance(segment.get('id'), bool):
            failures.append('non_contiguous_ids')
        if (isinstance(start, bool) or not isinstance(start, (int, float)) or
                not math.isfinite(start) or start < 0):
            failures.append('invalid_timestamp')
        else:
            if start < previous:
                failures.append('non_monotonic_timeline')
            if duration and start > duration + 1:
                failures.append('timestamp_out_of_range')
            previous = start
        if not isinstance(text, str) or not text.strip():
            empty += 1
        else:
            chars += len(text.strip())
            if '\n' in text or '\r' in text:
                failures.append('multiline_segment_text')
            if index > 1 and isinstance(segments[index - 2], dict) and text == segments[index - 2].get('text'):
                repeated += 1
    if empty:
        failures.append('empty_segment_text')
    if repeated:
        warnings.append('consecutive_repeated_text: check against audio')
    # A quiet introduction/outro or pause is possible; these are review hints.
    if duration and previous >= 0 and duration - previous > 120:
        warnings.append('last_segment_starts_over_120s_before_end: review audio tail')
    return {'passed': not failures, 'hard_failures': sorted(set(failures)), 'warnings': warnings,
            'segment_count': len(segments), 'character_count': chars,
            'empty_segment_count': empty, 'consecutive_repeated_count': repeated}


def inspect_text(text, duration=None):
    segments, failures = parse_transcript(text)
    report = quality_report(segments, duration)
    report['hard_failures'] = sorted(set(report['hard_failures'] + failures))
    report['passed'] = not report['hard_failures']
    return segments, report


def _within(root, value):
    path = (root / value).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError('Transcript artifact must stay inside its work directory')
    return path


def _artifacts(root, info, prefix):
    text = _within(root, info[prefix + '_text'])
    segments = _within(root, info[prefix + '_segments'])
    if sha256(text) != info[prefix + '_text_sha256'] or sha256(segments) != info[prefix + '_segments_sha256']:
        raise ValueError(prefix + ' transcript changed; restore the retained files or use a new version')
    return text, read_json(segments)


def retain_transcript(path, root, source, origin):
    """Snapshot a complete source transcript once; re-check it on every use."""
    root = Path(root).resolve()
    directory = root / 'transcripts'
    info_path = directory / 'provenance.json'
    if info_path.exists():
        info = read_json(info_path)
        if info['source_key'] != source['key'] or info['duration'] != source.get('duration', 0):
            raise ValueError('Retained transcript belongs to a different source')
        raw, segments = _artifacts(root, info, 'raw')
        # The acquisition output must not silently replace an archived original.
        if Path(path).read_text(encoding='utf-8-sig') != raw.read_text(encoding='utf-8'):
            raise ValueError('Acquired transcript changed after retention; use a new version')
        report = quality_report(segments, info['duration'])
        if info.get('corrected_text'):
            active, corrected = _artifacts(root, info, 'corrected')
            report = correction_quality(segments, corrected, info['duration'])
        else:
            active = raw
    else:
        text = Path(path).read_text(encoding='utf-8-sig')
        segments, report = inspect_text(text, source.get('duration'))
        atomic_json(directory / 'quality.json', report)
        if not report['passed']:
            raise ValueError('Transcript quality check failed: ' + ', '.join(report['hard_failures']))
        atomic_write(directory / 'raw.txt', text)
        atomic_json(directory / 'raw.segments.json', segments)
        info = {'schema_version': 1, 'source_key': source['key'], 'duration': source.get('duration', 0),
                'origin': origin, 'raw_text': 'transcripts/raw.txt',
                'raw_segments': 'transcripts/raw.segments.json',
                'raw_text_sha256': sha256(directory / 'raw.txt'),
                'raw_segments_sha256': sha256(directory / 'raw.segments.json')}
        atomic_json(info_path, info)
        active = directory / 'raw.txt'
    if not report['passed']:
        raise ValueError('Transcript quality check failed: ' + ', '.join(report['hard_failures']))
    return {'transcript': str(active), 'raw_transcript': str(directory / 'raw.txt'),
            'transcript_provenance': str(info_path), 'transcript_quality': report,
            'corrected_transcript': str(active) if info.get('corrected_text') else None}


def correction_quality(raw, corrected, duration=None):
    report = quality_report(corrected, duration)
    if not isinstance(corrected, list) or len(raw) != len(corrected):
        report['hard_failures'].append('segment_count_mismatch')
    elif any(not isinstance(b, dict) or a['id'] != b.get('id') or a['start'] != b.get('start')
             for a, b in zip(raw, corrected)):
        report['hard_failures'].append('segment_identity_or_timestamp_changed')
    original_chars = sum(len(s['text'].strip()) for s in raw)
    retention = report['character_count'] / original_chars if original_chars else 0
    report['character_retention'] = round(retention, 4)
    if retention < 0.5:
        report['hard_failures'].append('character_retention_below_50_percent')
    elif retention < 0.7:
        report['warnings'].append('character_retention_below_70_percent')
    report['passed'] = not report['hard_failures']
    return report


def finalize_transcript(manifest_path, corrections_path):
    manifest_path = Path(manifest_path).resolve()
    root = manifest_path.parent
    manifest = read_json(manifest_path)
    # Re-running after interruption between provenance and manifest commits
    # revalidates both files and repairs the active pointer below.
    verify_material_transcript(manifest, root, verify_active=False)
    info_path = root / 'transcripts/provenance.json'
    info = read_json(info_path)
    _, raw = _artifacts(root, info, 'raw')
    corrections = read_json(corrections_path)
    if not isinstance(corrections, dict) or corrections.get('source_key') != info['source_key']:
        raise ValueError('Corrections require the matching source_key')
    corrected = corrections.get('segments')
    report = correction_quality(raw, corrected, info['duration'])
    atomic_json(root / 'transcripts/last-correction-check.json', report)
    if not report['passed']:
        raise ValueError('Correction quality check failed: ' + ', '.join(report['hard_failures']))
    # Keep timestamps and IDs from the original even if numeric JSON types vary.
    corrected = [dict(a, text=b['text'].strip()) for a, b in zip(raw, corrected)]
    text = '\n'.join(f"[{s['start'] // 60:02d}:{s['start'] % 60:02d}] {s['text']}" for s in corrected) + '\n'
    # A successful correction is immutable in this generation. This also keeps
    # retries from replacing files referenced by an already committed manifest.
    corrected_file = root / 'transcripts/corrected.txt'
    if corrected_file.exists() and corrected_file.read_text(encoding='utf-8') != text:
        raise ValueError('A different correction already exists; use a new version')
    atomic_write(corrected_file, text)
    atomic_json(root / 'transcripts/corrected.segments.json', corrected)
    atomic_json(root / 'transcripts/correction-quality.json', report)
    info.update(corrected_text='transcripts/corrected.txt', corrected_segments='transcripts/corrected.segments.json',
                corrected_text_sha256=sha256(corrected_file),
                corrected_segments_sha256=sha256(root / 'transcripts/corrected.segments.json'))
    atomic_json(info_path, info)
    manifest.update(transcript=str(corrected_file), corrected_transcript=str(corrected_file), transcript_quality=report)
    atomic_json(manifest_path, manifest)
    return dict(manifest, manifest=str(manifest_path))


def verify_material_transcript(manifest, root, verify_active=True):
    """Verify new-format artifacts; leave legacy manifests readable."""
    if manifest.get('transcription', {}).get('status') in ('transcribing', 'quality_failed', 'pending'):
        raise ValueError('Transcript is still transcribing or failed quality checks; resume before writing notes')
    if not manifest.get('transcript_provenance'):
        return
    root = Path(root).resolve()
    info = read_json(root / 'transcripts/provenance.json')
    if info['source_key'] != manifest['source']['key'] or info['duration'] != manifest['source'].get('duration', 0):
        raise ValueError('Transcript provenance does not match the source')
    raw_path, raw = _artifacts(root, info, 'raw')
    parsed, report = inspect_text(raw_path.read_text(encoding='utf-8'), info['duration'])
    if parsed != raw or not report['passed']:
        raise ValueError('Raw transcript quality or segment alignment failed')
    active = raw_path
    if info.get('corrected_text'):
        active, corrected = _artifacts(root, info, 'corrected')
        parsed, text_report = inspect_text(active.read_text(encoding='utf-8'), info['duration'])
        report = correction_quality(raw, corrected, info['duration'])
        if parsed != corrected or not text_report['passed']:
            raise ValueError('Corrected transcript text does not match its segments')
    if not report['passed'] or (verify_active and (not manifest.get('transcript') or Path(manifest['transcript']).resolve() != active)):
        raise ValueError('Active transcript failed quality or provenance verification')
