"""Run pinned upstream fetch for one page, without copying its implementation."""
from __future__ import annotations

import gzip
import json
import re
import runpy
import sys
from urllib.parse import parse_qs, urlencode, urlsplit


def fallback_view(url, original_api, http_get, *args, **kwargs):
    bvids = parse_qs(urlsplit(url).query).get('bvid', [])
    if len(bvids) != 1 or not re.fullmatch(r'BV[0-9A-Za-z]{10}', bvids[0]):
        raise ValueError('View fallback requires one valid BV ID')
    bvid = bvids[0]
    raw = http_get('https://www.bilibili.com/video/' + bvid, *args, **kwargs)
    # Video pages can be gzip-encoded even without an Accept-Encoding header.
    if raw.startswith(b'\x1f\x8b'):
        raw = gzip.decompress(raw)
    html = raw.decode('utf-8')
    match = re.search(r'\bwindow\.__INITIAL_STATE__\s*=\s*', html)
    if not match:
        raise ValueError('Video page has no initial metadata')
    state, _ = json.JSONDecoder().raw_decode(html[match.end():])
    data = state.get('videoData') if isinstance(state, dict) else None
    if not isinstance(data, dict) or data.get('bvid') != bvid:
        raise ValueError('Video page metadata does not match the requested BV ID')
    if (not isinstance(data.get('aid'), int) or data['aid'] <= 0
            or not isinstance(data.get('title'), str) or not data['title'].strip()
            or not isinstance(data.get('owner'), dict)
            or not isinstance(data['owner'].get('name'), str)
            or not data['owner']['name'].strip()):
        raise ValueError('Video page metadata is missing aid, title or owner')
    pages = original_api('https://api.bilibili.com/x/player/pagelist?'
                         + urlencode({'bvid': bvid}), *args, **kwargs)['data']
    if not isinstance(pages, list) or not pages:
        raise ValueError('Pagelist returned no video pages')
    seen = set()
    for item in pages:
        if (not isinstance(item, dict)
                or not isinstance(item.get('page'), int) or item['page'] <= 0
                or item['page'] in seen
                or not isinstance(item.get('cid'), int) or item['cid'] <= 0
                or not isinstance(item.get('part'), str)
                or not isinstance(item.get('duration'), (int, float))
                or item['duration'] < 0):
            raise ValueError('Pagelist returned invalid video page metadata')
        seen.add(item['page'])
    data = dict(data, pages=pages, duration=sum(p['duration'] for p in pages))
    data.setdefault('desc', '')
    return {'code': 0, 'data': data}


def main():
    script, source, output, page = sys.argv[1:]
    page = int(page)
    namespace = runpy.run_path(script)
    fetch_main = namespace['main']
    original_api = fetch_main.__globals__['api']

    def api(url, *args, **kwargs):
        if urlsplit(url).path == '/x/web-interface/view':
            try:
                result = original_api(url, *args, **kwargs)
            except (OSError, RuntimeError, ValueError) as view_error:
                print('Bilibili view failed; trying video page metadata and pagelist',
                      file=sys.stderr)
                try:
                    result = fallback_view(url, original_api,
                                           fetch_main.__globals__['http_get'], *args, **kwargs)
                except (OSError, RuntimeError, ValueError, KeyError, TypeError) as fallback_error:
                    raise RuntimeError('Bilibili metadata fetch failed; view: {}; fallback: {}'
                                       .format(view_error, fallback_error)) from fallback_error
            data = dict(result['data'])
            pages = [p for p in data['pages'] if p['page'] == page]
            if not pages:
                raise ValueError('Requested page does not exist')
            data['pages'] = pages
            data['duration'] = sum(p['duration'] for p in pages)
            result = dict(result, data=data)
            return result
        return original_api(url, *args, **kwargs)

    fetch_main.__globals__['api'] = api
    original_argv = sys.argv
    try:
        sys.argv = [script, source, output]
        fetch_main()
    finally:
        sys.argv = original_argv
        fetch_main.__globals__['api'] = original_api


if __name__ == '__main__':
    main()
