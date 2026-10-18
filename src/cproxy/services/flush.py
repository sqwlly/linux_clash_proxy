"""`cproxy flush` —— 精准清理分裂出口（Split-Brain）与指定目标长连接。

当策略组（如 `AI-GEMINI` / `AI-MANUAL`）发生被动或主动切换后，客户端已建立的
HTTP/2 或长连接可能仍挂在旧节点上造成「同一进程双出口分裂」。本模块提供：
1. `--stale`（默认无参行为）：只精准掐断滞留在非当前出口节点上的陈旧 AI 连接；
2. 指定策略组（`ai` / `gemini`）、域名（如 `googleapis.com`）、进程（如 `agy`）或 `all`：
   主动断开全部匹配连接，迫使客户端立即通过最新出口重连。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..backend.api import APIBackend
from ..backend.runtime import (
    AI_GEMINI_GROUP,
    AI_MANUAL_GROUP,
    AI_SG_GROUP,
    AI_US_GROUP,
    resolve_ai_leaf,
)
from ..config import AppPaths
from ..names import resolve_group_alias
from .ops import _process_basename

AI_MANAGED_GROUPS = (AI_MANUAL_GROUP, AI_GEMINI_GROUP, AI_US_GROUP, AI_SG_GROUP)


@dataclass
class FlushedConnection:
    conn_id: str
    process: str
    host: str
    group: str
    old_leaf: str
    active_leaf: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.conn_id,
            "process": self.process,
            "host": self.host,
            "group": self.group,
            "old_leaf": self.old_leaf,
            "active_leaf": self.active_leaf,
        }


@dataclass
class FlushReport:
    mode: str
    target: str
    scanned: int = 0
    closed: list[FlushedConnection] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "target": self.target,
            "scanned": self.scanned,
            "closed_count": len(self.closed),
            "closed": [item.to_dict() for item in self.closed],
        }


def flush_connections(
    paths: AppPaths,
    target: str | None = None,
    *,
    stale_only: bool = False,
) -> FlushReport:
    """扫描并断开匹配的活跃连接。未传 `target` 时默认执行 `stale_only=True`。"""
    api = APIBackend(paths)
    groups = api.get_groups()
    payload = api.get_connections()
    raw_conns = payload.get("connections") if isinstance(payload, dict) else []
    if not isinstance(raw_conns, list):
        raw_conns = []

    raw_target = str(target or "").strip()
    if not raw_target:
        stale_only = True
        mode = "stale"
        effective_target = "AI-* (stale)"
    elif raw_target.lower() == "all":
        mode = "all"
        effective_target = "all"
    else:
        aliased = resolve_group_alias(raw_target, groups.keys())
        if aliased:
            mode = "group"
            effective_target = aliased
        else:
            mode = "filter"
            effective_target = raw_target.lower()

    active_leaves: dict[str, str] = {
        grp: resolve_ai_leaf(groups, grp)
        for grp in groups
    }

    report = FlushReport(mode=mode, target=effective_target, scanned=len(raw_conns))
    for item in raw_conns:
        if not isinstance(item, dict):
            continue
        conn_id = str(item.get("id") or "").strip()
        if not conn_id:
            continue
        chains = [str(c) for c in (item.get("chains") or []) if c]
        conn_leaf = chains[0] if chains else "-"
        conn_top_group = chains[-1] if chains else "-"
        meta = item.get("metadata") or {}
        if not isinstance(meta, dict):
            meta = {}
        host = str(
            meta.get("host")
            or meta.get("sniffHost")
            or meta.get("destinationIP")
            or meta.get("remoteDestination")
            or "-"
        ).lower()
        proc = _process_basename(meta)

        matched_group = ""
        expected_leaf = "-"
        should_close = False

        if mode == "all":
            matched_group = conn_top_group
            expected_leaf = active_leaves.get(conn_top_group, "-")
            should_close = True
        elif mode == " stale" or (mode == "stale"):
            for ai_grp in AI_MANAGED_GROUPS:
                if ai_grp in chains:
                    cur_leaf = active_leaves.get(ai_grp, "-")
                    if cur_leaf and cur_leaf != "-" and conn_leaf != cur_leaf:
                        matched_group = ai_grp
                        expected_leaf = cur_leaf
                        should_close = True
                        break
        elif mode == "group":
            if effective_target in chains:
                matched_group = effective_target
                expected_leaf = active_leaves.get(effective_target, "-")
                if stale_only:
                    should_close = bool(expected_leaf and expected_leaf != "-" and conn_leaf != expected_leaf)
                else:
                    should_close = True
        else:
            if effective_target in host or proc.lower() == effective_target:
                matched_group = conn_top_group
                expected_leaf = active_leaves.get(conn_top_group, "-")
                if stale_only:
                    should_close = bool(expected_leaf and expected_leaf != "-" and conn_leaf != expected_leaf)
                else:
                    should_close = True

        if should_close:
            try:
                api.close_connection(conn_id)
                report.closed.append(
                    FlushedConnection(
                        conn_id=conn_id,
                        process=proc,
                        host=host,
                        group=matched_group,
                        old_leaf=conn_leaf,
                        active_leaf=expected_leaf,
                    )
                )
            except Exception:
                pass

    return report
