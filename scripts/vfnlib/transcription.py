from __future__ import annotations

"""可恢复的音频分片转写。

该模块只负责本地产物：分片计划、逐片 checkpoint 和最终合并文本。
实际 ASR 由调用方传入，因而可以复用上游 faster-whisper 实现，也便于离线测试。
"""

import hashlib
import importlib.util
import json
import math
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Callable, Iterable


class ChunkedTranscriptionUnavailable(RuntimeError):
    """当前环境不能使用分片转写，需要回退到上游整段转写。"""


TIMESTAMP_RE = re.compile(r"^\[(\d+):(\d{2})(?::(\d{2}))?\]\s*(.*)$")


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(content, encoding="utf-8")
    os.replace(temp, path)


def atomic_json(path: Path, value: dict) -> None:
    atomic_write(path, json.dumps(value, ensure_ascii=False, indent=2))


def _fingerprint(path: Path, chunk_seconds: float, overlap_seconds: float,
                 audio_identity: str | None = None) -> dict:
    stat = path.stat()
    return {
        "size": stat.st_size,
        # 下载到临时文件的音频每次续跑 mtime 都会变化；调用方有稳定来源身份时，
        # 用来源身份做绑定，否则保留 mtime 以保护通用调用者不误用旧 checkpoint。
        "identity": audio_identity,
        "mtime_ns": None if audio_identity else stat.st_mtime_ns,
        "chunk_seconds": chunk_seconds,
        "overlap_seconds": overlap_seconds,
    }


def parse_timestamped_text(text: str) -> list[tuple[float, str]]:
    """解析上游 [mm:ss] 文本，忽略空行和无法定位的行。"""
    result = []
    for line in text.splitlines():
        match = TIMESTAMP_RE.match(line.strip())
        if not match:
            continue
        first, second, third, content = match.groups()
        seconds = int(first) * 60 + int(second)
        if third is not None:
            seconds = int(first) * 3600 + int(second) * 60 + int(third)
        if content.strip():
            result.append((float(seconds), content.strip()))
    return result


def _format_timestamp(seconds: float) -> str:
    total = max(0, int(seconds))
    return f"[{total // 60:02d}:{total % 60:02d}]"


def _ranges(duration: float, chunk_seconds: float, overlap_seconds: float) -> list[dict]:
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError("Audio duration must be positive for chunked transcription")
    if not math.isfinite(chunk_seconds) or chunk_seconds <= 0 or not math.isfinite(overlap_seconds) or overlap_seconds < 0:
        raise ValueError('Chunk size must be positive and overlap nonnegative')
    ranges = []
    index = 0
    core_start = 0.0
    while core_start < duration:
        core_end = min(duration, core_start + chunk_seconds)
        ranges.append({
            "index": index,
            "core_start": round(core_start, 3),
            "core_end": round(core_end, 3),
            "window_start": round(max(0.0, core_start - overlap_seconds), 3),
            "window_end": round(min(duration, core_end + overlap_seconds), 3),
        })
        index += 1
        core_start = core_end
    return ranges


def _merge_checkpoints(checkpoints: Iterable[dict], output: Path) -> int:
    rows = []
    seen = set()
    for checkpoint in sorted(checkpoints, key=lambda item: item["index"]):
        for seconds, text in parse_timestamped_text(checkpoint.get("text", "")):
            global_seconds = seconds + checkpoint["window_start"]
            if global_seconds < checkpoint["core_start"] - 0.5:
                continue
            if global_seconds >= checkpoint["core_end"] + 0.5 and checkpoint["core_end"] < checkpoint.get("duration", checkpoint["core_end"]):
                continue
            key = (round(global_seconds, 1), re.sub(r"\s+", " ", text).strip())
            if key in seen:
                continue
            seen.add(key)
            rows.append((global_seconds, text))
    rows.sort(key=lambda item: item[0])
    lines = [f"{_format_timestamp(seconds)} {text}" for seconds, text in rows]
    atomic_write(output, "\n".join(lines) + ("\n" if lines else ""))
    return len(lines)


def run_chunked_transcription(
    audio_path: Path,
    output_path: Path,
    work_dir: Path,
    transcribe_window: Callable[[float, float, int], str],
    *,
    duration: float,
    chunk_seconds: float = 300.0,
    overlap_seconds: float = 0.3,
    max_chunks_per_run: int = 3,
    audio_identity: str | None = None,
    asr_options: dict | None = None,
) -> dict:
    """逐片执行 ASR，并在每片完成后原子保存 checkpoint。

    ``transcribe_window`` 接收窗口起止秒数和片段编号，返回带局部时间戳的文本。
    max_chunks_per_run=0 表示本次处理全部剩余片段。
    """
    audio_path = Path(audio_path)
    if max_chunks_per_run < 0:
        raise ValueError('max_chunks_per_run must be nonnegative')
    output_path = Path(output_path)
    work_dir = Path(work_dir)
    checkpoint_dir = work_dir / "chunk-checkpoints"
    checkpoint_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = work_dir / "chunk-manifest.json"
    fingerprint = _fingerprint(audio_path, chunk_seconds, overlap_seconds, audio_identity)
    if asr_options is not None:
        fingerprint['asr_options'] = asr_options
    ranges = _ranges(float(duration), chunk_seconds, overlap_seconds)
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        # Duration is not implied by file size or stable source identity. Compare
        # the full plan so a changed duration cannot silently reuse old chunks.
        if (manifest.get("fingerprint") != fingerprint or
                manifest.get("duration") != duration or manifest.get("ranges") != ranges):
            raise ValueError("Existing chunk checkpoints belong to a different audio or chunk plan")
    else:
        manifest = {
            "schema_version": 1,
            "audio": str(audio_path),
            "duration": duration,
            "fingerprint": fingerprint,
            "ranges": ranges,
            "completed": [],
            "status": "pending",
        }
        atomic_json(manifest_path, manifest)

    from .transcripts import inspect_text
    # Reconcile all files before applying the budget. A missing checkpoint must
    # never count as completed simply because the manifest still lists it.
    completed = set()
    silence = []
    for item in ranges:
        checkpoint = checkpoint_dir / f"chunk-{item['index']:03d}.json"
        if not checkpoint.exists():
            continue
        try:
            saved = json.loads(checkpoint.read_text(encoding='utf-8'))
            text = saved.get('text')
            _, report = inspect_text(text, item['window_end'] - item['window_start']) if isinstance(text, str) else ([], {'passed': False})
            if (all(saved.get(key) == item[key] for key in item) and
                    (report['passed'] or text == '')):
                completed.add(item['index'])
                if text == '':
                    silence.append(item['index'])
        except (OSError, ValueError, TypeError, AttributeError):
            continue
    added = 0
    for item in ranges:
        index = item["index"]
        checkpoint = checkpoint_dir / f"chunk-{index:03d}.json"
        if index in completed:
            continue
        if max_chunks_per_run and added >= max_chunks_per_run:
            break
        text = transcribe_window(item["window_start"], item["window_end"], index)
        if not isinstance(text, str):
            raise ValueError('ASR returned a non-text transcript')
        _, report = inspect_text(text, item['window_end'] - item['window_start'])
        if text.strip() and not report['passed']:
            atomic_json(work_dir / 'chunk-quality-failure.json', dict(index=index, **report))
            raise ValueError('Chunk transcript quality check failed: ' + ', '.join(report['hard_failures']))
        if not text.strip():
            text = ''
            silence.append(index)
        payload = dict(item, text=text, completed_at=round(time.time(), 3), duration=duration)
        atomic_json(checkpoint, payload)
        completed.add(index)
        manifest["completed"] = sorted(completed)
        manifest["status"] = "transcribing"
        manifest["last_completed"] = index
        atomic_json(manifest_path, manifest)
        added += 1

    checkpoint_values = []
    for index in sorted(completed):
        checkpoint = checkpoint_dir / f"chunk-{index:03d}.json"
        if checkpoint.exists():
            checkpoint_values.append(json.loads(checkpoint.read_text(encoding="utf-8")))
    _merge_checkpoints(checkpoint_values, output_path)
    complete = len(completed) == len(ranges)
    _, quality = inspect_text(output_path.read_text(encoding='utf-8'), duration)
    quality['silent_chunk_candidates'] = sorted(silence)
    if silence:
        quality['warnings'].append('empty_chunks_may_be_silent: review audio')
    atomic_json(work_dir / 'transcription-quality.json', quality)
    if complete and not quality['passed']:
        output_path.with_name(output_path.name + '.done').unlink(missing_ok=True)
        manifest.update(status='quality_failed', completed=sorted(completed), quality=quality)
        atomic_json(manifest_path, manifest)
        raise ValueError('Transcript quality check failed: ' + ', '.join(quality['hard_failures']))
    manifest["status"] = "prepared" if complete else "transcribing"
    manifest["completed"] = sorted(completed)
    manifest["remaining"] = max(0, len(ranges) - len(completed))
    manifest["output"] = str(output_path)
    atomic_json(manifest_path, manifest)
    if complete:
        done = output_path.with_name(output_path.name + ".done")
        atomic_json(done, {"size": output_path.stat().st_size, "done_at": round(time.time(), 3), "marker_version": 1})
    else:
        output_path.with_name(output_path.name + '.done').unlink(missing_ok=True)
    return {
        "status": manifest["status"],
        "manifest": str(manifest_path),
        "output": str(output_path),
        "completed": len(completed),
        "total": len(ranges),
        "remaining": manifest["remaining"],
        "quality": quality,
    }


def _load_upstream(path: Path):
    spec = importlib.util.spec_from_file_location("vfn_upstream_transcriber", path)
    if spec is None or spec.loader is None:
        raise ChunkedTranscriptionUnavailable("Unable to load upstream transcriber")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _audio_duration(audio_path: Path) -> float:
    try:
        import av
    except ImportError as exc:
        raise ChunkedTranscriptionUnavailable("PyAV is required for chunked transcription") from exc
    try:
        with av.open(str(audio_path)) as container:
            stream = next(iter(container.streams.audio), None)
            if stream is None or not stream.duration or not stream.time_base:
                raise ChunkedTranscriptionUnavailable("Audio duration is unavailable")
            return float(stream.duration * stream.time_base)
    except Exception as exc:
        if isinstance(exc, ChunkedTranscriptionUnavailable):
            raise
        raise ChunkedTranscriptionUnavailable(f"Cannot inspect audio duration: {exc}") from exc


def _window_wav(audio_path: Path, start: float, end: float, destination: Path) -> None:
    try:
        import av
        import wave
    except ImportError as exc:
        raise ChunkedTranscriptionUnavailable("PyAV is required for chunked transcription") from exc
    destination.parent.mkdir(parents=True, exist_ok=True)
    with av.open(str(audio_path)) as container:
        stream = next(iter(container.streams.audio), None)
        if stream is None:
            raise ChunkedTranscriptionUnavailable("No audio stream found")
        resampler = av.audio.resampler.AudioResampler(format="s16", layout="mono", rate=16000)
        with wave.open(str(destination), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(16000)
            for frame in container.decode(stream):
                timestamp = float(frame.time or 0.0)
                frame_end = timestamp + float(frame.samples / (frame.sample_rate or 16000))
                if frame_end < start:
                    continue
                if timestamp > end:
                    break
                for converted in resampler.resample(frame):
                    wav.writeframes(converted.to_ndarray().tobytes())


def normalize_language(language):
    if not isinstance(language, str) or not re.fullmatch(r'auto|[a-z]{2,3}', language):
        raise ValueError('ASR language must be auto or a Whisper language code such as zh/en/ja')
    # Check against the installed ASR vocabulary without loading a model.
    try:
        from faster_whisper.tokenizer import _LANGUAGE_CODES
    except (ImportError, OSError, RuntimeError):
        # Import-time DLL/ABI errors must not prevent the media runtime from
        # recovering. The selected runtime validates the language again.
        _LANGUAGE_CODES = None
    if language != 'auto' and _LANGUAGE_CODES is not None and language not in _LANGUAGE_CODES:
        raise ValueError('Unsupported Whisper language: ' + language)
    return language


def build_hotwords(metadata, selected, terms=None):
    if terms is not None and (not isinstance(terms, list) or any(not isinstance(t, str) for t in terms)):
        raise ValueError('asr_hotwords must be a list of strings')
    owner = metadata.get('owner', '')
    if isinstance(owner, dict):
        owner = owner.get('name', '')
    words, seen, length = [], set(), 0
    # Explicit terminology first so long titles cannot crowd it out.
    for value in [*(terms or []), metadata.get('title'), owner, selected.get('part')]:
        value = re.sub(r'https?://\S+', '', str(value or ''))
        value = re.sub(r'\s+', ' ', value).strip()[:100]
        if value and value.casefold() not in seen and length + len(value) <= 400:
            seen.add(value.casefold())
            words.append(value)
            length += len(value)
        if len(words) >= 20:
            break
    return words


def transcribe_page_chunked(meta_file: Path, page: int, output_dir: Path, upstream_script: Path,
                            *, max_chunks_per_run: int = 3, language: str = 'auto', hotwords=None,
                            whole_file: bool = False) -> dict:
    """对一个已选中的分P执行可恢复 ASR。"""
    try:
        module = _load_upstream(upstream_script)
        if not whole_file:
            import av  # noqa: F401
    except (ImportError, ModuleNotFoundError) as exc:
        raise ChunkedTranscriptionUnavailable(str(exc)) from exc
    meta = json.loads(Path(meta_file).read_text(encoding="utf-8"))
    pages = [item for item in meta.get("pages", []) if item.get("page") == page]
    if len(pages) != 1:
        raise ValueError(f"Selected page does not exist: {page}")
    selected = pages[0]
    output_dir = Path(output_dir)
    language = normalize_language(language)
    terms = build_hotwords(meta, selected, hotwords)
    model = module._cfg("asr_model", "small")
    device = module._cfg("asr_device", "auto")
    options = {'language': language, 'hotwords': terms, 'model': model, 'device': device}
    settings_file = output_dir / 'asr-settings.json'
    if settings_file.exists():
        if json.loads(settings_file.read_text(encoding='utf-8')) != options:
            raise ValueError('ASR settings changed; restore language/hotwords or use a new version')
    else:
        legacy_manifest = output_dir / 'chunk-manifest.json'
        if legacy_manifest.exists():
            prior = json.loads(legacy_manifest.read_text(encoding='utf-8'))
            if 'asr_options' not in prior.get('fingerprint', {}):
                raise ValueError('Legacy ASR checkpoints lack language/hotword identity; use a new version')
        atomic_json(settings_file, options)
    audio = output_dir / f".chunk-audio-{page:02d}.m4s"
    if selected.get("media_path"):
        audio = output_dir / selected["media_path"]
    elif not audio.is_file() or audio.stat().st_size == 0:
        cookie = module.load_sessdata()
        module.download_audio(meta["bvid"], selected["cid"], audio, cookie)
    output = output_dir / f"{page:02d}_{module.safe_name(selected['part'])}.txt"
    duration = float(selected.get("duration") or _audio_duration(audio))
    state = module.load_model(model, device)

    def transcribe_window(start: float, end: float, index: int) -> str:
        if whole_file:
            return module.transcribe(audio, model, None if language == 'auto' else language, device, terms=terms)
        with tempfile.TemporaryDirectory(prefix=f"vfn-chunk-{index:03d}-", dir=output_dir) as temp:
            wav = Path(temp) / "window.wav"
            _window_wav(audio, start, end, wav)
            return module.transcribe(wav, model, None if language == 'auto' else language, device, terms=terms)

    result = run_chunked_transcription(
        audio, output, output_dir, transcribe_window, duration=duration,
        chunk_seconds=max(duration, 300) if whole_file else 300,
        max_chunks_per_run=max_chunks_per_run,
        audio_identity=f"{meta.get('bvid') or meta.get('video_id')}:{page}", asr_options=options)
    # A bounded run must retain its downloaded audio for the next invocation;
    # remove it only after every chunk has been committed.
    if result.get("status") == "prepared" and audio.name.startswith(".chunk-audio-"):
        audio.unlink(missing_ok=True)
    result["model_used"] = state.get("model_used")
    result["device_used"] = state.get("device_used")
    result['asr_options'] = options
    result['fallback_reasons'] = state.get('fallback_reasons', [])
    return result
