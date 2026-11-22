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

from ..audit import write_audit_event
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
from .query import QueryService

AI_MANAGED_GROUPS = (AI_MANUAL_GROUP, AI_GEMINI_GROUP, AI_US_GROUP, AI_SG_GROUP)
_GROUP_TYPES = frozenset({"selector", "select", "fallback", "url-test", "urltest", "load-balance", "loadbalance"})
# 这些名称既是常用 CLI 进程名又出现在 GROUP_ALIASES 中，flush 场景下优先按进程过滤，避免误杀整组其他客户端
_PROCESS_PRIORITY_TARGETS = frozenset({"claude", "codex"})


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

    actual_group_names = [
        name
        for name, grp in groups.items()
        if grp.candidates or str(grp.type).lower() in _GROUP_TYPES
    ]
    exact_group_lower = {name.lower(): name for name in actual_group_names}
    active_procs: set[str] = set()
    for item in raw_conns:
        if not isinstance(item, dict):
            continue
        meta = item.get("metadata")
        if isinstance(meta, dict):
            p = _process_basename(meta).lower()
            if p and p != "-":
                active_procs.add(p)
                if p.endswith(".exe"):
                    active_procs.add(p[:-4])

    raw_target = str(target or "").strip()
    target_lower = raw_target.lower()
    explicit_keepalive = target_lower == "h2-keepalive"
    if not raw_target:
        stale_only = True
        mode = "stale"
        effective_target = "AI-* (stale)"
    elif target_lower == "all":
        mode = "all"
        effective_target = "all"
    elif explicit_keepalive:
        mode = "group"
        effective_target = "H2-KEEPALIVE"
    elif target_lower in exact_group_lower:
        mode = "group"
        effective_target = exact_group_lower[target_lower]
    elif target_lower in _PROCESS_PRIORITY_TARGETS or (
        target_lower in active_procs and target_lower not in {"ai", "gemini", "default", "global", "auto", "us", "sg"}
    ):
        mode = "filter"
        effective_target = target_lower
    else:
        match_group = QueryService(paths).match_rule_group()
        aliased = resolve_group_alias(raw_target, actual_group_names, match_group=match_group)
        if aliased:
            mode = "group"
            effective_target = aliased
        else:
            mode = "filter"
            effective_target = target_lower

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
        raw_chains = item.get("chains")
        chains = [str(c) for c in raw_chains if c] if isinstance(raw_chains, list) else []
        if "H2-KEEPALIVE" in chains and not explicit_keepalive:
            continue
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
            if stale_only:
                should_close = bool(expected_leaf and expected_leaf != "-" and conn_leaf != expected_leaf)
            else:
                should_close = True
        elif mode == "stale":
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
            proc_lower = proc.lower()
            host_hit = host != "-" and effective_target in host
            proc_hit = proc_lower != "-" and (
                proc_lower == effective_target or proc_lower == f"{effective_target}.exe"
            )
            if host_hit or proc_hit:
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

    write_audit_event(
        paths,
        action="flush_connections",
        target=effective_target,
        result="ok",
        detail={
            "mode": mode,
            "stale_only": stale_only,
            "scanned": report.scanned,
            "closed_count": len(report.closed),
        },
    )
    return report
