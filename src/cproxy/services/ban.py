"""`cproxy ban` / `cproxy unban` —— 节点拉黑与临时屏蔽服务。

将特定故障节点（如出口 IP 被 Claude/Google 403 封禁或晚高峰持续丢包）加入屏蔽列表，
在生成运行配置（`render_runtime`）及稳定度探测/重排时自动从 AI 候选池剔除；
同时内置安全兜底（若某分组全部节点均被命中，保留原列表以防 Mihomo 配置因空组报错）。
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from ..config import AppPaths, node_bans_file
from ..names import match_candidates_fuzzy, normalize_name


@dataclass(frozen=True)
class BanEntry:
    pattern: str
    reason: str = ""
    created_at: str = ""
    expires_at: float | None = None

    def is_expired(self, now: float | None = None) -> bool:
        if self.expires_at is None:
            return False
        current = time.time() if now is None else now
        return current >= self.expires_at

    def remaining_seconds(self, now: float | None = None) -> float | None:
        if self.expires_at is None:
            return None
        current = time.time() if now is None else now
        return max(0.0, self.expires_at - current)

    def to_dict(self) -> dict[str, Any]:
        return {
            "pattern": self.pattern,
            "reason": self.reason,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
        }


def load_active_bans(paths: AppPaths, *, now: float | None = None) -> list[BanEntry]:
    """读取当前未过期的节点屏蔽规则；文件不存在或损坏时返回空列表。"""
    path = node_bans_file(paths)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(raw, list):
        return []

    current = time.time() if now is None else now
    entries: list[BanEntry] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        pattern = str(item.get("pattern") or "").strip()
        if not pattern:
            continue
        exp_raw = item.get("expires_at")
        expires_at: float | None = None
        if isinstance(exp_raw, (int, float)):
            expires_at = float(exp_raw)
        entry = BanEntry(
            pattern=pattern,
            reason=str(item.get("reason") or "").strip(),
            created_at=str(item.get("created_at") or "").strip(),
            expires_at=expires_at,
        )
        if not entry.is_expired(current):
            entries.append(entry)
    return entries


def _save_bans(path: Path, entries: list[BanEntry]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    payload = [entry.to_dict() for entry in entries]
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def matches_ban_pattern(node_name: str, pattern: str) -> bool:
    """判断单个节点名是否命中屏蔽模式（支持精确子串、规范化子串与模糊简写如 kr01）。"""
    needle = pattern.strip()
    if not needle:
        return False
    if needle.lower() in node_name.lower():
        return True
    if normalize_name(needle).lower() in normalize_name(node_name).lower():
        return True
    return bool(match_candidates_fuzzy(needle, [node_name]))


def is_node_banned(node_name: str, bans: list[BanEntry]) -> bool:
    return any(matches_ban_pattern(node_name, entry.pattern) for entry in bans)


def filter_unbanned_nodes(
    nodes: list[str],
    bans: list[BanEntry],
    *,
    keep_fallback: bool = True,
) -> list[str]:
    """剔除命中屏蔽规则的节点；若 `keep_fallback=True` 且剔除后为空则保留原列表防崩。"""
    if not bans or not nodes:
        return list(nodes)
    filtered = [node for node in nodes if not is_node_banned(node, bans)]
    if not filtered and keep_fallback:
        return list(nodes)
    return filtered


def add_ban(
    paths: AppPaths,
    pattern: str,
    *,
    hours: float | None = None,
    reason: str = "",
    now: float | None = None,
) -> BanEntry:
    """添加或更新一条节点屏蔽规则。"""
    cleaned = str(pattern or "").strip()
    if not cleaned:
        raise ValueError("错误: 屏蔽模式不能为空")
    current = time.time() if now is None else now
    expires_at = (current + float(hours) * 3600.0) if (hours is not None and hours > 0) else None
    new_entry = BanEntry(
        pattern=cleaned,
        reason=str(reason or "").strip(),
        created_at=datetime.fromtimestamp(current).isoformat(timespec="seconds"),
        expires_at=expires_at,
    )
    existing = [
        item
        for item in load_active_bans(paths, now=current)
        if item.pattern.lower() != cleaned.lower()
    ]
    existing.append(new_entry)
    _save_bans(node_bans_file(paths), existing)
    return new_entry


def remove_ban(
    paths: AppPaths,
    pattern: str | None = None,
    *,
    clear_all: bool = False,
    now: float | None = None,
) -> list[BanEntry]:
    """移除匹配的屏蔽规则（或 `--all` 全部清空），返回被移除的条目列表。"""
    current = time.time() if now is None else now
    active = load_active_bans(paths, now=current)
    if clear_all:
        _save_bans(node_bans_file(paths), [])
        return active
    needle = str(pattern or "").strip()
    if not needle:
        return []
    removed: list[BanEntry] = []
    kept: list[BanEntry] = []
    for entry in active:
        if (
            entry.pattern.lower() == needle.lower()
            or normalize_name(entry.pattern).lower() == normalize_name(needle).lower()
            or matches_ban_pattern(entry.pattern, needle)
        ):
            removed.append(entry)
        else:
            kept.append(entry)
    if removed:
        _save_bans(node_bans_file(paths), kept)
    return removed
