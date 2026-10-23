"""Media runtime subprocess entry; stdout contains one JSON result."""
import argparse
from contextlib import redirect_stdout
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from vfnlib.transcription import transcribe_page_chunked


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--metadata', required=True)
    parser.add_argument('--page', required=True, type=int)
    parser.add_argument('--output', required=True)
    parser.add_argument('--upstream-script', required=True)
    parser.add_argument('--options', required=True)
    args = parser.parse_args()
    try:
        with redirect_stdout(sys.stderr):
            result = transcribe_page_chunked(Path(args.metadata), args.page, Path(args.output),
                                            Path(args.upstream_script), **json.loads(args.options))
        payload, code = {'ok': True, 'data': result}, 0
    except Exception as exc:
        payload, code = {'ok': False, 'error': str(exc), 'error_type': type(exc).__name__}, 1
    print(json.dumps(payload, ensure_ascii=True))
    return code


if __name__ == '__main__':
    raise SystemExit(main())
