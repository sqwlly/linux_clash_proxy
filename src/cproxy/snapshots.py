from __future__ import annotations

import os
import shutil
from datetime import datetime, timezone
from pathlib import Path

from .config import AppPaths, config_file, runtime_file

SNAPSHOT_KEEP = 10
SNAPSHOT_DIR_NAME = "snapshots"

# kind -> 快照恢复时的目标文件
_KIND_TARGETS = {
    "runtime": runtime_file,
    "config": config_file,
}


def snapshots_dir(paths: AppPaths) -> Path:
    return paths.state_dir / SNAPSHOT_DIR_NAME


def snapshot_file(paths: AppPaths, source: Path, kind: str) -> Path | None:
    """把 source 复制为一份快照；source 不存在时返回 None。"""
    if kind not in _KIND_TARGETS:
        raise ValueError(f"未知快照类型: {kind}")
    if not source.exists():
        return None

    target_dir = snapshots_dir(paths)
    target_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    # 强制 .yaml 后缀，与 _prune/list_snapshots 的 glob 保持一致
    target = target_dir / f"{kind}-{stamp}.yaml"
    shutil.copyfile(source, target)
    # 快照可能含明文 secret，权限收紧
    os.chmod(target, 0o600)
    _prune(target_dir, kind)
    return target


def _prune(target_dir: Path, kind: str) -> None:
    items = sorted(target_dir.glob(f"{kind}-*.yaml"))
    for old in items[:-SNAPSHOT_KEEP]:
        old.unlink()


def list_snapshots(paths: AppPaths, kind: str | None = None) -> list[Path]:
    target_dir = snapshots_dir(paths)
    if not target_dir.is_dir():
        return []
    pattern = f"{kind}-*.yaml" if kind else "*.yaml"
    return sorted(target_dir.glob(pattern), reverse=True)


def snapshot_kind(snapshot: Path) -> str:
    return snapshot.name.split("-", 1)[0]


def restore_snapshot(paths: AppPaths, snapshot: Path) -> Path:
    """把快照恢复到其对应的目标路径，返回目标路径。"""
    kind = snapshot_kind(snapshot)
    target_getter = _KIND_TARGETS.get(kind)
    if target_getter is None:
        raise ValueError(f"未知快照类型: {snapshot.name}")
    if not snapshot.is_file():
        raise FileNotFoundError(f"快照不存在: {snapshot}")
    target = target_getter(paths)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(snapshot, target)
    return target
