from __future__ import annotations

"""Discover or create an isolated media runtime, without changing the caller's Python."""

import json
import os
import re
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PACKAGES = ('faster-whisper==1.2.1', 'av==12.3.0', 'numpy>=1.26,<3', 'pillow>=10,<13', 'gmssl>=3.2,<4')
CHECK = """
import json, sys
from importlib.metadata import version
import av, faster_whisper, numpy, PIL, gmssl.sm3
if tuple(map(int, av.__version__.split('.')[:2])) != (12, 3):
    raise RuntimeError('Media adapter requires PyAV 12.3.x')
print(json.dumps({'ready': True, 'python': sys.executable,
                  'versions': {p: version(p) for p in ['faster-whisper','av','numpy','pillow','gmssl']}}))
"""


class RuntimeRecoveryError(RuntimeError):
    pass


def runtime_root(config):
    override = os.environ.get('VFN_MEDIA_RUNTIME')
    return Path(override).expanduser().resolve() if override else config.local_path('media_runtime_path', 'cache/media-runtime')


def python_in(directory):
    return Path(directory) / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')


def python_key(python):
    # Resolving symlinks would collapse Unix venv/bin/python into its base
    # interpreter, changing both dependency discovery and worker dispatch.
    return os.path.normcase(os.path.abspath(python))


def run_capture(command, *, timeout):
    env = dict(os.environ, PYTHONUTF8='1', PYTHONIOENCODING='utf-8')
    return subprocess.run([str(c) for c in command], capture_output=True, text=True,
                          encoding='utf-8', errors='replace', env=env, timeout=timeout)


def check_python(python):
    python = Path(python).expanduser().absolute()
    if not python.is_file():
        return {'ready': False, 'python': str(python), 'error': 'Python executable missing'}
    try:
        result = run_capture([python, '-c', CHECK], timeout=45)
        if result.returncode:
            return {'ready': False, 'python': str(python), 'error': (result.stderr or result.stdout)[-1500:]}
        report = json.loads(result.stdout)
        if report.get('ready') is not True:
            raise ValueError('Runtime probe returned invalid report')
        return report
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        return {'ready': False, 'python': str(python), 'error': str(exc)}


def candidates(config):
    explicit = os.environ.get('VFN_MEDIA_PYTHON') or config.get('media_python')
    if explicit:
        path = Path(explicit).expanduser()
        if not path.is_absolute():
            path = config.path.parent / path
        # Explicit selection is authoritative: don't silently install another environment.
        return [path.absolute()]
    root = runtime_root(config)
    managed = []
    pointer = root / 'active.json'
    if pointer.is_file():
        try:
            name = json.loads(pointer.read_text(encoding='utf-8'))['directory']
            if isinstance(name, str) and name.startswith('env-') and Path(name).name == name:
                managed.append(python_in(root / name))
        except (OSError, ValueError, KeyError, TypeError):
            pass
    paths = [Path(sys.executable), *managed, python_in(ROOT / '.venv')]
    seen, result = set(), []
    for path in paths:
        key = python_key(path)
        if key not in seen:
            seen.add(key)
            result.append(path)
    return result


def inspect_runtime(config):
    attempts = []
    for python in candidates(config):
        report = check_python(python)
        attempts.append(report)
        if report['ready']:
            return dict(report, checked_runtimes=attempts, recovered=False)
    return {'ready': False, 'python': None, 'checked_runtimes': attempts,
            'runtime_root': str(runtime_root(config)), 'recovered': False}


def _step(command, label, attempts=1):
    detail = ''
    for _ in range(attempts):
        try:
            result = run_capture(command, timeout=120 if label == 'venv' else 600)
            if result.returncode == 0:
                return
            detail = (result.stderr or result.stdout)[-2500:]
        except (OSError, subprocess.SubprocessError) as exc:
            detail = str(exc)
    raise RuntimeRecoveryError(f'Media runtime {label} failed: {detail}')


def recovery_python():
    """Find an installed interpreter supported by the pinned binary media wheels."""
    paths = [sys.executable]
    if os.name == 'nt' and shutil.which('py'):
        try:
            listing = run_capture(['py', '-0p'], timeout=15)
            for line in listing.stdout.splitlines():
                match = re.search(r'([A-Za-z]:\\.*?python(?:\d+(?:\.\d+)?)?\.exe)\s*\*?\s*$', line, re.I)
                if match:
                    paths.append(match.group(1))
        except (OSError, subprocess.SubprocessError):
            pass
    for name in ('python3.12', 'python3.11', 'python3.10', 'python3', 'python'):
        found = shutil.which(name)
        if found:
            paths.append(found)
    seen = set()
    for python in paths:
        key = python_key(python)
        if key in seen:
            continue
        seen.add(key)
        try:
            result = run_capture([python, '-c',
                'import sys, venv, ensurepip; sys.exit(0 if (3,10) <= sys.version_info[:2] <= (3,12) else 1)'], timeout=15)
            if result.returncode == 0:
                return str(python)
        except (OSError, subprocess.SubprocessError):
            continue
    raise RuntimeRecoveryError('No usable Python 3.10–3.12 with venv/ensurepip found; install one or set VFN_MEDIA_PYTHON to a ready media environment')


def ensure_runtime(config):
    report = inspect_runtime(config)
    if report['ready']:
        return report
    if os.environ.get('VFN_MEDIA_PYTHON') or config.get('media_python'):
        raise RuntimeRecoveryError('Configured media Python is unavailable: ' + json.dumps(report, ensure_ascii=False))
    if os.environ.get('VFN_DISABLE_RUNTIME_RECOVERY') == '1' or config.get('auto_recover_runtime', True) is False:
        raise RuntimeRecoveryError('Automatic media runtime recovery is disabled: ' + json.dumps(report, ensure_ascii=False))
    root = runtime_root(config)
    root.mkdir(parents=True, exist_ok=True)
    # Serialize installation. Do not remove another process's lock or assume a
    # quiet/stale process has died. Diagnostics give the exact lock to inspect.
    lock = root / 'install.lock'
    try:
        handle = lock.open('x', encoding='utf-8')
    except FileExistsError as exc:
        raise RuntimeRecoveryError(f'Media runtime installation lock exists; check the prior installer: {lock}') from exc
    build = root / ('env-' + uuid.uuid4().hex)
    try:
        with handle:
            handle.write(str(os.getpid()))
        # A competing installer may have completed before we acquired the lock.
        ready = inspect_runtime(config)
        if ready['ready']:
            return ready
        if shutil.disk_usage(root).free < 1024 ** 3:
            raise RuntimeRecoveryError('Less than 1 GiB free for media runtime installation: ' + str(root))
        # Venvs are not relocatable. Build at a unique final path and only
        # switch the small pointer after verification; never rename a venv.
        base = recovery_python()
        print('Recovering isolated media runtime: ' + str(build), file=sys.stderr, flush=True)
        _step([base, '-m', 'venv', build], 'venv')
        python = python_in(build)
        _step([python, '-m', 'pip', 'install', '--disable-pip-version-check', '--no-cache-dir',
               '--only-binary=av,ctranslate2', *PACKAGES], 'pip', attempts=2)
        checked = check_python(python)
        if not checked['ready']:
            raise RuntimeRecoveryError('Installed media runtime failed verification: ' + json.dumps(checked, ensure_ascii=False))
        pointer = root / 'active.json'
        temporary = root / ('active-' + uuid.uuid4().hex + '.tmp')
        try:
            temporary.write_text(json.dumps({'directory': build.name, 'packages': PACKAGES}, indent=2), encoding='utf-8')
            os.replace(temporary, pointer)
        finally:
            temporary.unlink(missing_ok=True)
        return dict(checked, recovered=True, checked_runtimes=report['checked_runtimes'], runtime_root=str(root))
    except Exception:
        # Only this invocation's freshly-created directory is removable. Never
        # touch an older runtime, the user's environment or task artifacts.
        if build.parent.resolve() == root.resolve() and build.name.startswith('env-') and build.exists():
            shutil.rmtree(build, ignore_errors=True)
        raise
    finally:
        lock.unlink(missing_ok=True)


def transcribe_with_runtime(config, meta, page, output, upstream_script, **options):
    report = ensure_runtime(config)
    python = Path(report['python'])
    if python_key(python) == python_key(sys.executable):
        from .transcription import transcribe_page_chunked
        result = transcribe_page_chunked(meta, page, output, upstream_script, **options)
    else:
        worker = Path(__file__).with_name('runtime_worker.py')
        command = [python, worker, '--metadata', meta, '--page', str(page), '--output', output,
                   '--upstream-script', upstream_script, '--options', json.dumps(options, ensure_ascii=False)]
        # One bounded ASR call; don't replay recognition failures or bootstrap
        # on unrelated network/model/audio errors.
        env = dict(os.environ, PYTHONUTF8='1', PYTHONIOENCODING='utf-8')
        completed = subprocess.run([str(c) for c in command], stdout=subprocess.PIPE, stderr=None,
                                   text=True, encoding='utf-8', env=env)
        try:
            payload = json.loads(completed.stdout)
        except ValueError as exc:
            raise RuntimeRecoveryError('Media worker returned invalid output: ' + completed.stdout[-1000:]) from exc
        if completed.returncode or not payload.get('ok'):
            if payload.get('error_type') == 'ChunkedTranscriptionUnavailable':
                from .transcription import ChunkedTranscriptionUnavailable
                raise ChunkedTranscriptionUnavailable(str(payload.get('error')))
            raise RuntimeRecoveryError('Media worker failed: ' + str(payload.get('error')))
        result = payload['data']
    result['runtime'] = {key: report[key] for key in ('python', 'recovered', 'versions') if key in report}
    return result
