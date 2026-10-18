"""AI-MANUAL / AI-GEMINI 稳定度实测重排：探测全部叶子，按稳定性排序后持久化并热重载。

fallback 组按列表顺序找第一个存活节点，顺序即兜底优先级。这里把 AI-MANUAL 与
AI-GEMINI 的叶子按 `ProbeSummary.rank_key`（成功↑/失败↓/延迟↓/历史惩罚↓）重排，
写入 state 文件（`ai-manual-order.json` / `ai-gemini-order.json`），render 时消费（见 `runtime.py`）。
"""
from __future__ import annotations

import json
from dataclasses import dataclass

from ..backend.api import APIBackend
from ..backend.process import ProcessBackend
from ..backend.runtime import (
    AI_AUTO_GROUP,
    AI_GEMINI_GROUP,
    AI_MANUAL_GROUP,
    RuntimeBackend,
    _load_ai_gemini_order,
    _load_ai_manual_order,
)
from ..config import AppPaths, ai_gemini_order_file, ai_manual_order_file, runtime_file
from .probe import ProbeService, ProbeSummary
from .query import QueryService

REBALANCE_PROFILE = "codex"
REBALANCE_GEMINI_PROFILE = "gemini"
# 排序时对历史失败惩罚的加权倍数：跨多次探测的惩罚自然平滑单轮噪声，
# 削弱「一轮 5/5、下一轮 1/5」的名次翻转。
HISTORY_PENALTY_WEIGHT = 4.0
# 当现役节点历史平均失败惩罚达到该阈值（如近 20 轮有 ≥35% 轮次出现超时/丢包），
# 即便本轮碰巧 5/5 通过，也主动切到历史更稳的榜首节点。
CHRONIC_PENALTY_SWITCH_THRESHOLD_MS = 80
# 逐轮折半淘汰（active_candidates_after_round）下，尾部候选只有 1~2 轮样本，
# 几毫秒抖动就会让尾部名次每 30 分钟洗牌一次。若候选集合未变且前 TOP_K 名次
# 完全一致，视为有效兜底序未变，保留原排序以避免无意义的快照膨胀与热重载。
REBALANCE_TOP_K = 5


def _smooth_rank_key(summary: ProbeSummary):
    """`rank_key` 基础上加重历史惩罚权重。

    rank_key = (-成功数, 失败数, 最大延迟+惩罚, 平均延迟+惩罚, 惩罚, 名字)。
    这里把已叠加的惩罚还原成裸延迟，再按权重重新叠加，得到更稳的排序。
    """
    neg_success, failures, max_plus_p, avg_plus_p, penalty, name = summary.rank_key()
    weighted = penalty * HISTORY_PENALTY_WEIGHT
    return (
        neg_success,
        failures,
        max_plus_p - penalty + weighted,
        avg_plus_p - penalty + weighted,
        penalty,
        name,
    )


def _should_keep_previous_order(previous: list[str], current: list[str]) -> bool:
    if not previous or len(previous) != len(current) or set(previous) != set(current):
        return False
    return previous[:REBALANCE_TOP_K] == current[:REBALANCE_TOP_K]


@dataclass(frozen=True)
class RebalanceReport:
    ordered: tuple[str, ...]
    order_path: str
    rerendered: bool
    hot_reloaded: bool
    reason: str = ""
    switched_to: str | None = None
    gemini_ordered: tuple[str, ...] = ()
    gemini_order_path: str = ""
    gemini_switched_to: str | None = None


class RebalanceService:
    def __init__(self, paths: AppPaths):
        self.paths = paths

    def rebalance(self, dry_run: bool = False) -> RebalanceReport:
        probe_service = ProbeService(self.paths)
        report = probe_service.probe(
            group=AI_MANUAL_GROUP,
            profile=REBALANCE_PROFILE,
            record_history=True,
        )
        ordered = [summary.name for summary in sorted(report.summaries, key=_smooth_rank_key)]
        order_path = ai_manual_order_file(self.paths)

        gemini_report = None
        gemini_ordered: list[str] = []
        gemini_order_path = ai_gemini_order_file(self.paths)
        try:
            gemini_report = probe_service.probe(
                group=AI_GEMINI_GROUP,
                profile=REBALANCE_GEMINI_PROFILE,
                record_history=True,
            )
            gemini_ordered = [
                summary.name for summary in sorted(gemini_report.summaries, key=_smooth_rank_key)
            ]
        except (RuntimeError, KeyError, ValueError):
            gemini_report = None
            gemini_ordered = []

        if dry_run:
            return RebalanceReport(
                ordered=tuple(ordered),
                order_path=str(order_path),
                rerendered=False,
                hot_reloaded=False,
                reason="dry-run",
                gemini_ordered=tuple(gemini_ordered),
                gemini_order_path=str(gemini_order_path) if gemini_ordered else "",
            )

        previous_order = _load_ai_manual_order(self.paths)
        order_updated = not _should_keep_previous_order(previous_order, ordered)
        effective_order = ordered if order_updated else previous_order
        if order_updated:
            order_path.parent.mkdir(parents=True, exist_ok=True)
            order_path.write_text(json.dumps(ordered, ensure_ascii=False), encoding="utf-8")

        gemini_previous_order = _load_ai_gemini_order(self.paths)
        gemini_baseline = (
            gemini_previous_order
            if gemini_previous_order or set(gemini_ordered) != set(previous_order)
            else previous_order
        )
        gemini_order_updated = bool(
            gemini_ordered and not _should_keep_previous_order(gemini_baseline, gemini_ordered)
        )
        effective_gemini_order = gemini_ordered if gemini_order_updated else (gemini_previous_order or gemini_ordered)
        if gemini_order_updated:
            gemini_order_path.parent.mkdir(parents=True, exist_ok=True)
            gemini_order_path.write_text(json.dumps(gemini_ordered, ensure_ascii=False), encoding="utf-8")

        target_runtime = runtime_file(self.paths)
        before_bytes = target_runtime.read_bytes() if target_runtime.exists() else None
        runtime_path = RuntimeBackend(self.paths).render_runtime()
        after_bytes = runtime_path.read_bytes() if runtime_path.exists() else None
        runtime_changed = before_bytes != after_bytes

        process = ProcessBackend(self.paths)
        running = process.is_running()
        is_stale = getattr(process, "is_runtime_stale", lambda _path=None: False)(runtime_path)
        hot_reloaded = False
        if running and (order_updated or gemini_order_updated or runtime_changed or is_stale):
            try:
                APIBackend(self.paths).reload_config(str(runtime_path))
                hot_reloaded = True
            except Exception:
                # 热重载失败留给下一轮 cproxy render/rebalance 兜底，不阻断
                hot_reloaded = False
            if hot_reloaded and hasattr(process, "mark_runtime_applied"):
                process.mark_runtime_applied(runtime_path)

        switched_to: str | None = None
        if running and effective_order:
            switched_to = self._switch_if_current_unstable(report, effective_order[0], AI_MANUAL_GROUP)

        gemini_switched_to: str | None = None
        if running and gemini_report is not None and effective_gemini_order:
            gemini_switched_to = self._switch_if_current_unstable(
                gemini_report, effective_gemini_order[0], AI_GEMINI_GROUP
            )

        return RebalanceReport(
            ordered=tuple(effective_order),
            order_path=str(order_path),
            rerendered=True,
            hot_reloaded=hot_reloaded,
            switched_to=switched_to,
            gemini_ordered=tuple(effective_gemini_order),
            gemini_order_path=str(gemini_order_path) if effective_gemini_order else "",
            gemini_switched_to=gemini_switched_to,
        )

    def _switch_if_current_unstable(
        self,
        report,
        best_candidate: str,
        group_name: str = AI_MANUAL_GROUP,
    ) -> str | None:
        """当前出口在多轮实测中已不稳定（或历史高频丢包）而榜首节点稳定时，主动切到榜首。

        mihomo fallback 仅在 generate_204 超时（5000ms）时被动切换；对于「能通但丢包/
        抖动不达标」的裂化节点，热重载还会保留原选中项。此处在非自动模式下主动切到
        实测稳定的榜首叶子，闭合「探测出劣化 → 切走」回路。
        """
        current_verdict = getattr(report, "current_verdict", None)
        best_verdict = getattr(report, "verdict", None)
        if current_verdict is None or best_verdict is None:
            return None
        if not best_verdict.stable:
            return None
        current_node = getattr(report, "current", None)
        if not best_candidate or best_candidate == current_node:
            return None
        if current_verdict.stable:
            summaries = getattr(report, "summaries", ()) or ()
            cur_summary = next((s for s in summaries if getattr(s, "name", None) == current_node), None)
            best_summary = next((s for s in summaries if getattr(s, "name", None) == best_candidate), None)
            cur_penalty = getattr(cur_summary, "history_penalty_ms", 0) if cur_summary else 0
            best_penalty = getattr(best_summary, "history_penalty_ms", 0) if best_summary else 0
            if cur_penalty < CHRONIC_PENALTY_SWITCH_THRESHOLD_MS or best_penalty * 2 > cur_penalty:
                return None
        try:
            query = QueryService(self.paths)
            group_obj = query.api.get_groups().get(group_name)
            if group_obj is None:
                return None
            if group_name == AI_MANUAL_GROUP and group_obj.current == AI_AUTO_GROUP:
                return None
            query.switch_group(group_name, best_candidate)
            return best_candidate
        except Exception:
            return None
