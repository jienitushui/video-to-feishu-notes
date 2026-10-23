from __future__ import annotations

"""Plan and apply cleanup of local per-task work directories.

Canonical layout:
    output/<record_id>/v<generation-number>/

The cleanup is deliberately local-only. It never deletes Feishu documents or
changes task state, and it refuses to remove the current version.
"""

import re
import shutil
from pathlib import Path


VERSION_RE = re.compile(r"^v([1-9][0-9]*)$")
LEGACY_VERSION_RE = re.compile(r"-v([1-9][0-9]*)(?:-|$)")
RECORD_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def validate_record_id(record_id: str) -> str:
    if not isinstance(record_id, str) or not RECORD_RE.fullmatch(record_id):
        raise ValueError("record_id contains unsupported path characters")
    return record_id


def canonical_task_root(output_root: str | Path, record_id: str) -> Path:
    return Path(output_root).expanduser().resolve() / validate_record_id(record_id)


def canonical_version_dir(output_root: str | Path, record_id: str, version: int) -> Path:
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise ValueError("version must be a positive integer")
    return canonical_task_root(output_root, record_id) / f"v{version}"


def _version_dirs(task_root: Path) -> list[tuple[int, Path]]:
    result = []
    if not task_root.is_dir():
        return result
    for child in task_root.iterdir():
        if not child.is_dir():
            continue
        match = VERSION_RE.fullmatch(child.name)
        if match:
            result.append((int(match.group(1)), child))
    return sorted(result)


def plan_cleanup(output_root: str | Path, record_id: str, current_version: int,
                 status: str, *, include_legacy: bool = False) -> dict:
    """Return a safe local cleanup plan without changing the filesystem."""
    task_root = canonical_task_root(output_root, record_id)
    if status != "已完成":
        return {
            "record_id": record_id,
            "output_root": str(Path(output_root).expanduser().resolve()),
            "task_root": str(task_root),
            "status": status,
            "current_version": current_version,
            "applied": False,
            "blocked": "Task is not completed; local cleanup is protected",
            "current": [],
            "candidates": [],
            "skipped": [],
        }
    if isinstance(current_version, bool) or not isinstance(current_version, int) or current_version < 1:
        raise ValueError("current_version must be a positive integer")

    versions = _version_dirs(task_root)
    candidates = [path for version, path in versions if version < current_version]
    current = [str(path) for version, path in versions if version == current_version]
    skipped = [str(path) for version, path in versions if version > current_version]

    legacy = []
    root = Path(output_root).expanduser().resolve()
    legacy_prefix = f"{record_id}-v"
    if root.is_dir():
        for child in root.iterdir():
            if child.is_dir() and child.name.startswith(legacy_prefix):
                legacy.append(str(child))
    if legacy:
        for raw in legacy:
            match = LEGACY_VERSION_RE.search(Path(raw).name)
            legacy_version = int(match.group(1)) if match else None
            # Even with explicit compatibility cleanup, protect the current
            # generation and any directory carrying its suffix (e.g. v3-illustrated).
            if include_legacy and legacy_version is not None and legacy_version < current_version:
                candidates.append(raw)
            else:
                skipped.append(raw)

    return {
        "record_id": record_id,
        "output_root": str(root),
        "task_root": str(task_root),
        "status": status,
        "current_version": current_version,
        "applied": False,
        "blocked": None,
        "current": current,
        "candidates": [str(path) for path in candidates],
        "skipped": skipped,
        "legacy": legacy,
    }


def apply_cleanup(plan: dict) -> dict:
    """Delete only planned old canonical directories, then verify absence."""
    if plan.get("blocked"):
        return dict(plan, applied=False, deleted=[])
    deleted = []
    root = Path(plan["output_root"]).resolve()
    task_root = Path(plan["task_root"]).resolve()
    if not task_root.is_relative_to(root):
        raise ValueError("Task output directory is outside output root")
    for raw in plan.get("candidates", []):
        target = Path(raw).resolve()
        canonical = target.is_relative_to(task_root) and target.parent == task_root and VERSION_RE.fullmatch(target.name)
        legacy = (target.parent == root and target.name.startswith(plan["record_id"] + "-v") and
                  target in {Path(item).resolve() for item in plan.get("candidates", [])})
        if not (canonical or legacy):
            raise ValueError("Refusing to delete a path outside the task version directory")
        if target.exists():
            shutil.rmtree(target)
            if target.exists():
                raise RuntimeError("Local version directory still exists after deletion: " + str(target))
            deleted.append(str(target))
    return dict(plan, applied=True, deleted=deleted)
