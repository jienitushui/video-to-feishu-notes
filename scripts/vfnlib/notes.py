from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path


def load_note(path):
    path = Path(path).resolve()
    note = json.loads(path.read_text(encoding='utf-8-sig'))
    validate_note(note, path.parent)
    return note, path.parent


def image_path(base, value):
    if Path(value).is_absolute():
        raise ValueError('Image path must be relative to the note working directory')
    path = (base / value).resolve()
    if not path.is_relative_to(base.resolve()):
        raise ValueError('Image must be inside the note working directory: ' + value)
    if not path.is_file() or path.stat().st_size == 0:
        raise ValueError('Image does not exist or is empty: ' + value)
    if path.suffix.lower() not in ('.png', '.jpg', '.jpeg', '.webp'):
        raise ValueError('Unsupported image extension: ' + value)
    if path.stat().st_size > 20 * 1024 * 1024:
        raise ValueError('Image exceeds the 20 MiB local upload limit')
    return path


def validate_note(note, base):
    if not isinstance(note, dict) or note.get('schema_version') != 1:
        raise ValueError('note.json requires schema_version: 1')
    for key, maximum in [('title', 200), ('summary', 2000), ('source_key', 250)]:
        if not isinstance(note.get(key), str) or not note[key].strip() or len(note[key]) > maximum:
            raise ValueError('Invalid note ' + key)
    sections = note.get('sections')
    if not isinstance(sections, list) or not sections or len(sections) > 100:
        raise ValueError('sections must contain 1..100 sections')
    ids = set()
    for section in sections:
        if not isinstance(section, dict):
            raise ValueError('Each section must be an object')
        sid = section.get('id')
        if not isinstance(sid, str) or not sid or sid in ids:
            raise ValueError('Section IDs must be unique nonempty strings')
        ids.add(sid)
        if not isinstance(section.get('title'), str) or not section['title'].strip() or len(section['title']) > 200:
            raise ValueError('Section title is required')
        if not isinstance(section.get('blocks'), list) or not section['blocks']:
            raise ValueError('Section blocks must not be empty')
        for block in section['blocks']:
            kind = block.get('type') if isinstance(block, dict) else None
            if kind not in ('text', 'bullet', 'code', 'image'):
                raise ValueError('Block type must be text, bullet, code, or image')
            if kind == 'image':
                if not isinstance(block.get('path'), str):
                    raise ValueError('Image path is required')
                image_path(base, block['path'])
                if not isinstance(block.get('caption'), str) or not block['caption'].strip() or len(block['caption']) > 1800:
                    raise ValueError('Image caption is required')
                if 'seconds' not in block:
                    raise ValueError('Image timestamp is required')
            elif not isinstance(block.get('text'), str) or not block['text'].strip():
                raise ValueError('Text content is required')
            if 'seconds' in block and (isinstance(block['seconds'], bool) or
                                       not isinstance(block['seconds'], (int, float)) or
                                       not math.isfinite(block['seconds']) or block['seconds'] < 0):
                raise ValueError('seconds must be a nonnegative number')
    return {'valid': True, 'sections': len(sections),
            'images': sum(b['type'] == 'image' for s in sections for b in s['blocks'])}


def stamp(seconds):
    seconds = int(seconds)
    return '{:02d}:{:02d}:{:02d}'.format(seconds // 3600, seconds // 60 % 60, seconds % 60) if seconds >= 3600 else '{:02d}:{:02d}'.format(seconds // 60, seconds % 60)


def timestamp_url(source, seconds):
    if source['platform'] == 'bilibili':
        return source['url'] + '&t=' + str(int(seconds))
    return source['url']  # Douyin has no verified timestamp deep link.


def text_block(content, kind='text', link=None):
    types = {'text': 2, 'heading1': 3, 'heading2': 4, 'bullet': 12, 'code': 14, 'quote': 15}
    run = {'content': content}
    if link:
        run['text_element_style'] = {'link': {'url': link}}
    payload = {'elements': [{'text_run': run}]}
    if kind == 'code':
        payload['style'] = {'language': 1, 'wrap': True}
    return {'block_type': types[kind], kind: payload}


def plan_note(note, base, source, marker):
    plan = [{'block': text_block(marker)}, {'block': text_block(note['title'], 'heading1')},
            {'block': text_block('来源：' + source['title'], link=source['url'])},
            {'block': text_block(note['summary'], 'quote')}]
    for section in note['sections']:
        plan.append({'block': text_block(section['title'], 'heading2')})
        for block in section['blocks']:
            seconds = block.get('seconds')
            if seconds is not None:
                if source.get('duration') and seconds > source['duration']:
                    raise ValueError('Timestamp is beyond video duration')
                plan.append({'block': text_block('回看 ' + stamp(seconds), link=timestamp_url(source, seconds))})
            if block['type'] == 'image':
                path = image_path(base, block['path'])
                plan.append({'block': {'block_type': 27, 'image': {}}, 'path': str(path),
                             'sha256': hashlib.sha256(path.read_bytes()).hexdigest()})
                plan.append({'block': text_block(block['caption'])})
            else:
                # Keep individual blocks within a conservative text size.
                content = block['text']
                for offset in range(0, len(content), 1800):
                    plan.append({'block': text_block(content[offset:offset + 1800], block['type'])})
    if len(plan) > 1500:
        raise ValueError('Note is too large: split into fewer or shorter blocks')
    return plan


def markdown(note, source):
    lines = ['# ' + note['title'], '', '> ' + note['summary'], '', '[原视频](' + source['url'] + ')', '']
    for section in note['sections']:
        lines.extend(['## ' + section['title'], ''])
        for block in section['blocks']:
            if 'seconds' in block:
                lines.extend(['[回看 ' + stamp(block['seconds']) + '](' + timestamp_url(source, block['seconds']) + ')', ''])
            kind = block['type']
            if kind == 'image':
                lines.append('![' + block['caption'] + '](' + block['path'] + ')')
            elif kind == 'code':
                fence = '```'
                while fence in block['text']:
                    fence += '`'
                lines.extend([fence + block.get('language', ''), block['text'], fence])
            else:
                lines.append(('- ' if kind == 'bullet' else '') + block['text'])
            lines.append('')
    return '\n'.join(lines)
