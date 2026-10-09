from __future__ import annotations

import hashlib
import os
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import AppPaths, config_file, runtime_file

SNAPSHOT_KEEP = 10
SNAPSHOT_DIR_NAME = "snapshots"

# kind -> 快照恢复时的目标文件
_KIND_TARGETS = {
    "runtime": runtime_file,
    "config": config_file,
}


@dataclass
class CompactionReport:
    """快照压缩与分级清理报告。"""

    total_before: int = 0
    total_after: int = 0
    kept: list[str] = field(default_factory=list)
    pruned: list[str] = field(default_factory=list)
    bytes_reclaimed: int = 0
    dry_run: bool = False


def snapshots_dir(paths: AppPaths) -> Path:
    return paths.state_dir / SNAPSHOT_DIR_NAME


def parse_snapshot_time(snapshot: Path | str) -> datetime | None:
    """解析快照文件名中的 UTC 时间戳，解析失败时返回 None。"""
    name = snapshot.name if isinstance(snapshot, Path) else snapshot
    stem = Path(name).stem
    parts = stem.split("-", 1)
    if len(parts) != 2:
        return None
    raw = parts[1]
    try:
        if "." in raw:
            return datetime.strptime(raw, "%Y%m%dT%H%M%S.%fZ").replace(tzinfo=timezone.utc)
        return datetime.strptime(raw, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _file_hash(path: Path) -> str:
    """计算文件的 SHA256 哈希值。"""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _latest_snapshot(target_dir: Path, kind: str) -> Path | None:
    items = sorted(target_dir.glob(f"{kind}-*.yaml"), reverse=True)
    return items[0] if items else None


def snapshot_file(
    paths: AppPaths,
    source: Path,
    kind: str,
    deduplicate: bool = True,
) -> Path | None:
    """把 source 复制为一份快照；source 不存在时返回 None。

    deduplicate 为 True 时，若最新快照内容与 source 完全一致，则跳过创建并直接返回该快照。
    """
    if kind not in _KIND_TARGETS:
        raise ValueError(f"未知快照类型: {kind}")
    if not source.exists():
        return None

    target_dir = snapshots_dir(paths)
    target_dir.mkdir(parents=True, exist_ok=True)

    if deduplicate:
        latest = _latest_snapshot(target_dir, kind)
        if latest is not None and latest.is_file():
            try:
                if source.read_bytes() == latest.read_bytes():
                    return latest
            except OSError:
                pass

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    # 强制 .yaml 后缀，与 _prune/list_snapshots 的 glob 保持一致
    target = target_dir / f"{kind}-{stamp}.yaml"
    shutil.copyfile(source, target)
    # 快照可能含明文 secret，权限收紧
    os.chmod(target, 0o600)
    _prune(target_dir, kind)
    return target


def _evaluate_tier_retention(
    items: list[Path],
    now: datetime | None = None,
) -> tuple[list[Path], list[Path]]:
    """分级阶梯保留判定算法（Tiered Retention Ladder）。

    items: 按时间由新到旧排序的单类型快照列表 (newest first).
    返回 (kept_items, pruned_items).
    """
    if not items:
        return [], []

    current_time = now or datetime.now(timezone.utc)
    kept: list[Path] = []
    pruned: list[Path] = []

    # 阶段 1：相邻内容哈希去重（若连续两个快照内容完全相同，较旧的视为冗余并修剪）
    deduped_candidates: list[Path] = []
    prev_hash: str | None = None
    for item in items:
        try:
            curr_hash = _file_hash(item)
            if prev_hash is not None and curr_hash == prev_hash:
                pruned.append(item)
                continue
            prev_hash = curr_hash
            deduped_candidates.append(item)
        except OSError:
            deduped_candidates.append(item)

    # 阶段 2：时间阶梯采样（Tiered Retention Ladder）
    # - 最近 SNAPSHOT_KEEP (10) 个：基础受保护层（无论生成多频繁都保留）
    # - 1 小时内：全量保留
    # - 1 小时 ~ 24 小时：每小时保留最新的 1 个
    # - 24 小时 ~ 7 天：每天保留最新的 1 个
    # - 7 天 ~ 30 天：每周保留最新的 1 个
    # - 超过 30 天：淘汰清理
    hourly_seen: set[tuple[int, int, int, int]] = set()
    daily_seen: set[tuple[int, int, int]] = set()
    weekly_seen: set[tuple[int, int, int]] = set()

    for idx, item in enumerate(deduped_candidates):
        item_time = parse_snapshot_time(item)
        if item_time is None:
            try:
                item_time = datetime.fromtimestamp(item.stat().st_mtime, tzinfo=timezone.utc)
            except OSError:
                item_time = current_time

        age = current_time - item_time

        # 保护层 1：最新 SNAPSHOT_KEEP 个快照无条件保留
        if idx < SNAPSHOT_KEEP:
            kept.append(item)
            hourly_seen.add((item_time.year, item_time.month, item_time.day, item_time.hour))
            daily_seen.add((item_time.year, item_time.month, item_time.day))
            weekly_seen.add((item_time.year, item_time.isocalendar()[1]))
            continue

        # 保护层 2：1 ~ 24 小时内，每小时保留最新 1 个
        if age <= timedelta(hours=24):
            hour_bucket = (item_time.year, item_time.month, item_time.day, item_time.hour)
            if hour_bucket not in hourly_seen:
                hourly_seen.add(hour_bucket)
                daily_seen.add((item_time.year, item_time.month, item_time.day))
                weekly_seen.add((item_time.year, item_time.isocalendar()[1]))
                kept.append(item)
            else:
                pruned.append(item)
            continue

        # 保护层 3：1 天 ~ 7 天内，每天保留最新 1 个
        if age <= timedelta(days=7):
            day_bucket = (item_time.year, item_time.month, item_time.day)
            if day_bucket not in daily_seen:
                daily_seen.add(day_bucket)
                weekly_seen.add((item_time.year, item_time.isocalendar()[1]))
                kept.append(item)
            else:
                pruned.append(item)
            continue

        # 保护层 4：7 天 ~ 30 天内，每周保留最新 1 个
        if age <= timedelta(days=30):
            week_bucket = (item_time.year, item_time.isocalendar()[1])
            if week_bucket not in weekly_seen:
                weekly_seen.add(week_bucket)
                kept.append(item)
            else:
                pruned.append(item)
            continue

        # 超过 30 天：清理
        pruned.append(item)

    return kept, pruned


def compact_snapshots(
    paths: AppPaths,
    kind: str | None = None,
    dry_run: bool = False,
    now: datetime | None = None,
) -> CompactionReport:
    """对快照执行分级压缩与轮转清理。

    kind: 仅压缩指定类型（'runtime' 或 'config'）；为 None 时压缩所有类型。
    dry_run: 为 True 时仅分析并返回报告，不实际删除文件。
    now: 供测试注入的当前基准时间。
    """
    target_dir = snapshots_dir(paths)
    if not target_dir.is_dir():
        return CompactionReport(dry_run=dry_run)

    kinds_to_process = [kind] if kind else sorted(_KIND_TARGETS.keys())
    if kind is None:
        all_kinds = {snapshot_kind(p) for p in target_dir.glob("*.yaml")}
        kinds_to_process = sorted(set(kinds_to_process) | all_kinds)

    report = CompactionReport(dry_run=dry_run)

    for k in kinds_to_process:
        items = sorted(target_dir.glob(f"{k}-*.yaml"), reverse=True)
        report.total_before += len(items)
        kept, pruned = _evaluate_tier_retention(items, now=now)

        report.kept.extend(p.name for p in kept)
        for p in pruned:
            report.pruned.append(p.name)
            try:
                size = p.stat().st_size
                report.bytes_reclaimed += size
            except OSError:
                pass
            if not dry_run:
                try:
                    p.unlink()
                except OSError:
                    pass

    report.total_after = len(report.kept)
    return report


def _prune(target_dir: Path, kind: str) -> None:
    """创建快照后的快速自动清理。"""
    items = sorted(target_dir.glob(f"{kind}-*.yaml"), reverse=True)
    _, pruned = _evaluate_tier_retention(items)
    for old in pruned:
        try:
            old.unlink()
        except OSError:
            pass


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


def format_relative_time(dt: datetime | None, now: datetime | None = None) -> str:
    """人性化格式化时间差（例如：刚刚、15分钟前、昨天 20:05、3天前）。"""
    if dt is None:
        return "未知时间"
    current = now or datetime.now(timezone.utc)
    if dt > current:
        return "刚刚"
    diff = current - dt
    total_seconds = int(diff.total_seconds())

    if total_seconds < 60:
        return "刚刚"
    if total_seconds < 3600:
        return f"{total_seconds // 60} 分钟前"
    if total_seconds < 86400:
        hours = total_seconds // 3600
        return f"{hours} 小时前"
    if total_seconds < 86400 * 2:
        return f"昨天 {dt.strftime('%H:%M')}"
    if total_seconds < 86400 * 7:
        days = total_seconds // 86400
        return f"{days} 天前"
    return dt.strftime("%Y-%m-%d %H:%M")


def snapshot_tier_label(entry: Path, idx: int = 0, now: datetime | None = None) -> str:
    """返回快照所在分级阶梯的直观标签。"""
    dt = parse_snapshot_time(entry)
    if dt is None:
        return "未分级"
    current = now or datetime.now(timezone.utc)
    age = current - dt
    if idx < SNAPSHOT_KEEP:
        return "近期全量"
    if age <= timedelta(hours=24):
        return "每小时"
    if age <= timedelta(days=7):
        return "每日采样"
    if age <= timedelta(days=30):
        return "每周采样"
    return "已超期"


def snapshot_diff(paths: AppPaths, snapshot: Path) -> list[str]:
    """计算快照文件与当前对应目标配置之间的统一差异行（unified diff）。"""
    import difflib

    kind = snapshot_kind(snapshot)
    target_getter = _KIND_TARGETS.get(kind)
    if not target_getter:
        return []
    target = target_getter(paths)
    if not target.is_file() or not snapshot.is_file():
        return []

    target_lines = target.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
    snapshot_lines = snapshot.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)

    diff = difflib.unified_diff(
        target_lines,
        snapshot_lines,
        fromfile=f"当前文件 ({target.name})",
        tofile=f"快照文件 ({snapshot.name})",
    )
    return list(diff)
