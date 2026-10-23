"""Read metadata through pinned upstream functions without running its fetch main."""
from __future__ import annotations

import json
import importlib.util
import runpy
import sys
from pathlib import Path
from urllib.parse import urlencode

try:
    from .bili_one import fallback_view
except ImportError:  # direct subprocess entry point
    from bili_one import fallback_view


def probe(scripts, platform, source, page):
    scripts = Path(scripts)
    if platform == 'bilibili':
        ns = runpy.run_path(str(scripts / 'bili_fetch.py'))
        bvid = ns['extract_bvid'](source)
        url = 'https://api.bilibili.com/x/web-interface/view?' + urlencode({'bvid': bvid})
        try:
            data = ns['api'](url)['data']
        except (OSError, RuntimeError, ValueError) as view_error:
            try:
                data = fallback_view(url, ns['api'], ns['http_get'])['data']
            except (OSError, RuntimeError, ValueError, KeyError, TypeError) as fallback_error:
                raise RuntimeError(f'Metadata fetch failed; view: {view_error}; fallback: {fallback_error}') from fallback_error
        if data.get('bvid') != bvid:
            raise ValueError('Video metadata does not match requested BV ID')
        pages = [item for item in data['pages'] if item['page'] == page]
        if len(pages) != 1:
            raise ValueError('Requested page does not exist')
        selected = pages[0]
        title = data['title'] + (' / ' + selected['part'] if page != 1 else '')
        return {'source_key': f'bilibili:{bvid}:p{page}', 'title': title,
                'duration_seconds': selected.get('duration')}
    if platform == 'douyin':
        # The upstream import otherwise attempts to install its signature helper.
        # A read-only preflight must never trigger that installation.
        if not (scripts / 'abogus.py').is_file():
            raise ValueError('Douyin metadata needs the existing upstream abogus.py; preflight does not install it')
        # Import the existing helper first. Missing transitive dependencies or
        # a missing ABogus class must fail before upstream's installer fallback.
        spec = importlib.util.spec_from_file_location('abogus', scripts / 'abogus.py')
        helper = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(helper)
        if not hasattr(helper, 'ABogus'):
            raise ValueError('Existing abogus.py has no ABogus class')
        previous = sys.modules.get('abogus')
        sys.modules['abogus'] = helper
        try:
            ns = runpy.run_path(str(scripts / 'douyin_fetch.py'))
        finally:
            if previous is None:
                sys.modules.pop('abogus', None)
            else:
                sys.modules['abogus'] = previous
        video_id = ns['extract_video_id'](source)
        detail = ns['fetch_aweme_detail'](video_id)
        if str(detail.get('aweme_id')) != video_id:
            raise ValueError('Video metadata does not match requested Douyin ID')
        ns['check_not_album'](detail)
        value = detail.get('video', {}).get('duration')
        duration = value / 1000 if isinstance(value, (int, float)) and not isinstance(value, bool) else None
        return {'source_key': 'douyin:' + video_id, 'title': detail.get('desc') or detail.get('item_title'),
                'duration_seconds': duration}
    raise ValueError('Unsupported platform')


if __name__ == '__main__':
    try:
        result = probe(sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4]))
    except SystemExit as exc:
        raise RuntimeError('Metadata lookup failed: ' + str(exc)) from exc
    print(json.dumps(result, ensure_ascii=True))
