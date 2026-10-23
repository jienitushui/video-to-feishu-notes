"""Extract review candidates using the pinned upstream downloader and QC."""
from __future__ import annotations

import json
import math
import runpy
import sys
from pathlib import Path


def capture(video, times):
    import av

    results = []
    with av.open(str(video)) as container:
        stream = container.streams.video[0]
        for seconds in times:
            result = None
            try:
                container.seek(int(seconds / float(stream.time_base)), stream=stream, backward=True)
                for frame in container.decode(stream):
                    if frame.time is not None and frame.time >= seconds:
                        result = (frame.to_image(), float(frame.time))
                        break
            except (ValueError, RuntimeError, av.error.FFmpegError):
                pass
            results.append(result)
    return results


def extract(upstream, metadata_path, transcript_path, output):
    helpers = runpy.run_path(str(upstream))
    metadata_path, output = Path(metadata_path), Path(output)
    meta = json.loads(metadata_path.read_text(encoding='utf-8'))
    page = meta['pages'][0]
    duration = float(page.get('duration') or 0)
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError('A positive video duration is required for screenshots')
    output.mkdir(parents=True, exist_ok=True)
    transcript = Path(transcript_path)
    lines = helpers['parse_ts_seconds'](transcript.read_text(encoding='utf-8')) if transcript.is_file() else []
    jobs = helpers['auto_anchors'](lines)
    times = [float(job[-1]) for job in jobs if 0 <= float(job[-1]) < duration]
    method = 'transcript_anchors' if times else 'timeline'
    if not times:
        times = [duration * fraction for fraction in (0.15, 0.5, 0.85)]
    local = page.get('media_path')
    video = (metadata_path.parent / local).resolve() if local else output / '.video.m4s'
    if local and not video.is_file():
        raise ValueError('Local video is missing: ' + str(video))
    frames, failed = [], []
    try:
        if not local:
            helpers['download_video'](meta['bvid'], page['cid'], video, helpers['load_sessdata']())
        for index, (seconds, result) in enumerate(zip(times, capture(video, times)), 1):
            if result is None:
                failed.append({'page': page['page'], 't': seconds, 'reason': 'No decodable frame'})
                continue
            img, actual = result
            qc = helpers['qc_check'](img)
            if qc not in ('ok', 'skipped'):
                retry_time = seconds + min(5, duration * 0.05)
                retry = capture(video, [retry_time])[0] if retry_time < duration else None
                if retry is not None and helpers['qc_check'](retry[0]) == 'ok':
                    img, actual = retry
                    qc = 'ok'
            filename = 'p{:02d}_frame_{:02d}.png'.format(page['page'], index)
            img.save(output / filename)
            frames.append({'file': filename, 'page': page['page'], 't': actual, 'qc': qc,
                           'method': method, 'review_required': True,
                           'caption': '视频画面，需视觉复核后结合对应内容撰写说明'})
    finally:
        if not local:
            video.unlink(missing_ok=True)
    result = {'frames': frames, 'failed': failed, 'method': method}
    (output / 'manifest.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    return result


if __name__ == '__main__':
    extract(*sys.argv[1:])
