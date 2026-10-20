"""Run pinned upstream fetch for one page, without copying its implementation."""
from __future__ import annotations

import runpy
import sys


def main():
    script, source, output, page = sys.argv[1:]
    page = int(page)
    namespace = runpy.run_path(script)
    fetch_main = namespace['main']
    original_api = fetch_main.__globals__['api']

    def api(url, *args, **kwargs):
        result = original_api(url, *args, **kwargs)
        if '/x/web-interface/view?' in url:
            data = dict(result['data'])
            pages = [p for p in data['pages'] if p['page'] == page]
            if not pages:
                raise ValueError('Requested page does not exist')
            data['pages'] = pages
            data['duration'] = sum(p['duration'] for p in pages)
            result = dict(result, data=data)
        return result

    fetch_main.__globals__['api'] = api
    sys.argv = [script, source, output]
    fetch_main()


if __name__ == '__main__':
    main()
