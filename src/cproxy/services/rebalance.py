"""AI-MANUAL 稳定度实测重排：探测全部叶子，按稳定性排序后持久化并热重载。

fallback 组按列表顺序找第一个存活节点，顺序即兜底优先级。这里把 AI-MANUAL
的叶子按 `ProbeSummary.rank_key`（成功↑/失败↓/延迟↓/历史惩罚↓）重排，写入
state 文件（`ai-manual-order.json`），render 时消费（见 `runtime.py`）。
"""
from __future__ import annotations

import json
from dataclasses import dataclass

from ..backend.api import APIBackend
from ..backend.process import ProcessBackend
from ..backend.runtime import AI_MANUAL_GROUP, RuntimeBackend
from ..config import AppPaths, ai_manual_order_file
from .probe import ProbeService

REBALANCE_PROFILE = "codex"


@dataclass(frozen=True)
class RebalanceReport:
    ordered: tuple[str, ...]
    order_path: str
    rerendered: bool
    hot_reloaded: bool
    reason: str = ""


class RebalanceService:
    def __init__(self, paths: AppPaths):
        self.paths = paths

    def rebalance(self, dry_run: bool = False) -> RebalanceReport:
        report = ProbeService(self.paths).probe(
            group=AI_MANUAL_GROUP,
            profile=REBALANCE_PROFILE,
            record_history=True,
        )
        ordered = [summary.name for summary in sorted(report.summaries, key=lambda s: s.rank_key())]
        order_path = ai_manual_order_file(self.paths)

        if dry_run:
            return RebalanceReport(
                ordered=tuple(ordered),
                order_path=str(order_path),
                rerendered=False,
                hot_reloaded=False,
                reason="dry-run",
            )

        order_path.parent.mkdir(parents=True, exist_ok=True)
        order_path.write_text(json.dumps(ordered, ensure_ascii=False), encoding="utf-8")

        runtime_path = RuntimeBackend(self.paths).render_runtime()
        hot_reloaded = False
        if ProcessBackend(self.paths).is_running():
            try:
                APIBackend(self.paths).reload_config(str(runtime_path))
                hot_reloaded = True
            except Exception:
                # 热重载失败留给下一轮 cproxy render/rebalance 兜底，不阻断
                hot_reloaded = False
        return RebalanceReport(
            ordered=tuple(ordered),
            order_path=str(order_path),
            rerendered=True,
            hot_reloaded=hot_reloaded,
        )
