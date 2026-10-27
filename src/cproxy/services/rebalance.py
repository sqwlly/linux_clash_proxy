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
    DEFAULT_MAX_AI_MULTIPLIER,
    RuntimeBackend,
    _deprioritize_high_multiplier,
    _load_ai_gemini_order,
    _load_ai_manual_order,
)
from ..config import AppPaths, ai_gemini_order_file, ai_manual_order_file, read_config, runtime_file
from .nodelist import match_region
from .probe import (
    GEMINI_FAST_REGIONS,
    ProbeService,
    ProbeSummary,
    clear_stream_fragile_node,
    is_healthy_non_iepl_summary,
    is_iepl_node,
    is_static_iepl_node,
    probe_paths_context,
    record_stream_fragile_node,
)
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


def _smooth_rank_key(summary: ProbeSummary, *, prefer_non_iepl: bool = False):
    """`rank_key` 基础上加重历史惩罚权重。

    rank_key = (-成功数, 失败数, 最大延迟+惩罚, 平均延迟+惩罚, 惩罚, 名字)。
    这里把已叠加的惩罚还原成裸延迟，再按权重重新叠加，得到更稳的排序。
    当 `prefer_non_iepl=True`（AI-GEMINI / AI-MANUAL）时：
    - Tier 0: 零动态断流且本轮全通健康的非专线节点（healthy_non_iepl）
    - Tier 1: 零动态断流且本轮多数成功的非专线节点（alive_non_iepl）
    - Tier 2: 曾有动态重试记录但本身非静态 IEPL/裸 vision 的存活节点（防止 1X 节点偶发重试后跌到 50s 必断的 IEPL 之后）
    - Tier 3: 静态 IEPL / 裸 vision 健康节点
    - Tier 4: 其余劣化/超时节点
    """
    neg_success, failures, max_plus_p, avg_plus_p, penalty, name = summary.rank_key()
    weighted = penalty * HISTORY_PENALTY_WEIGHT
    max_w = max_plus_p - penalty + weighted
    avg_w = avg_plus_p - penalty + weighted
    if not prefer_non_iepl:
        return (
            neg_success,
            failures,
            max_w,
            avg_w,
            penalty,
            name,
        )
    healthy_non_iepl = is_healthy_non_iepl_summary(summary)
    alive_non_iepl = (
        not is_iepl_node(summary.name)
        and summary.success_count > 0
        and summary.success_count >= failures
        and penalty < 200
        and summary.avg_delay is not None
        and summary.avg_delay <= 1350
    )
    dynamic_non_static_iepl = (
        not is_static_iepl_node(summary.name)
        and summary.success_count > 0
        and (summary.success_count >= failures or summary.success_count >= 2)
        and summary.avg_delay is not None
        and summary.avg_delay <= 1500
    )
    is_healthy = summary.success_count > 0 and failures == 0 and penalty < CHRONIC_PENALTY_SWITCH_THRESHOLD_MS
    if healthy_non_iepl:
        tier = 0
    elif alive_non_iepl:
        tier = 1
    elif dynamic_non_static_iepl:
        tier = 2
    elif is_healthy:
        tier = 3
    else:
        tier = 4
    iepl_tiebreak = 0 if not is_static_iepl_node(summary.name) else 1
    reg = match_region(summary.name)
    region_tier = (
        0
        if (
            not (healthy_non_iepl or alive_non_iepl or dynamic_non_static_iepl)
            or reg in GEMINI_FAST_REGIONS
            or reg in ("OTHER", None)
        )
        else 1
    )
    return (
        tier,
        region_tier,
        neg_success,
        failures,
        iepl_tiebreak,
        avg_w,
        max_w,
        penalty,
        name,
    )


def _should_keep_previous_order(previous: list[str], current: list[str]) -> bool:
    if not previous or len(previous) != len(current) or set(previous) != set(current):
        return False
    if previous[:REBALANCE_TOP_K] == current[:REBALANCE_TOP_K]:
        return True
    # 逐轮折半淘汰下，第 3~5 名常仅有 2~4 轮样本；若前 2 名（主用+首选热备）顺序完全未变，
    # 且前 TOP_K 候选集合一致（仅第 3~5 名内部互换），同样保留原排序，避免每 30 分钟触发无意义热重载。
    return previous[:2] == current[:2] and set(previous[:REBALANCE_TOP_K]) == set(current[:REBALANCE_TOP_K])


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

    def rebalance(self, dry_run: bool = False, *, external_only: bool = False) -> RebalanceReport:
        with probe_paths_context(self.paths):
            return self._rebalance_impl(dry_run=dry_run, external_only=external_only)

    def _rebalance_impl(self, dry_run: bool = False, *, external_only: bool = False) -> RebalanceReport:
        cfg = read_config(self.paths)
        is_external = external_only or bool(isinstance(cfg, dict) and cfg.get("verge-mode"))
        raw_max_mult = cfg.get("max-ai-multiplier") if isinstance(cfg, dict) else None
        try:
            max_mult = float(raw_max_mult) if raw_max_mult is not None else DEFAULT_MAX_AI_MULTIPLIER
        except (TypeError, ValueError):
            max_mult = DEFAULT_MAX_AI_MULTIPLIER
        manual_prefer = cfg.get("ai-manual-prefer") if isinstance(cfg, dict) else None
        gemini_prefer = cfg.get("ai-gemini-prefer") if isinstance(cfg, dict) else None

        probe_service = ProbeService(self.paths)
        report = probe_service.probe(
            group=AI_MANUAL_GROUP,
            profile=REBALANCE_PROFILE,
            record_history=True,
        )
        ordered = [
            summary.name
            for summary in sorted(
                report.summaries,
                key=lambda s: _smooth_rank_key(s, prefer_non_iepl=True),
            )
        ]
        ordered = _deprioritize_high_multiplier(
            ordered,
            max_mult=max_mult,
            prefer=manual_prefer if isinstance(manual_prefer, list) else None,
        )
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
                summary.name
                for summary in sorted(
                    gemini_report.summaries,
                    key=lambda s: _smooth_rank_key(s, prefer_non_iepl=True),
                )
            ]
            gemini_ordered = _deprioritize_high_multiplier(
                gemini_ordered,
                max_mult=max_mult,
                prefer=gemini_prefer if isinstance(gemini_prefer, list) else None,
            )
        except (RuntimeError, KeyError, ValueError):
            gemini_report = None
            gemini_ordered = []

        if ordered and not self._should_switch_current(report, ordered[0], AI_MANUAL_GROUP):
            cur_manual = getattr(report, "current", None)
            cur_manual_verdict = getattr(report, "current_verdict", None)
            cur_manual_summary = next(
                (s for s in getattr(report, "summaries", ()) or () if getattr(s, "name", None) == cur_manual),
                None,
            )
            best_manual_summary = next(
                (s for s in getattr(report, "summaries", ()) or () if getattr(s, "name", None) == ordered[0]),
                None,
            )
            cur_manual_usable = bool(
                cur_manual_verdict is not None
                and (
                    getattr(cur_manual_verdict, "stable", False)
                    or cur_manual_summary is None
                    or (
                        getattr(cur_manual_summary, "success_count", 0) > 0
                        and getattr(cur_manual_summary, "success_count", 0)
                        >= getattr(best_manual_summary, "success_count", 0)
                    )
                )
            )
            if (
                cur_manual
                and cur_manual_usable
                and cur_manual in ordered
                and not (is_static_iepl_node(cur_manual, self.paths) and not is_static_iepl_node(ordered[0], self.paths))
            ):
                ordered = [cur_manual] + [n for n in ordered if n != cur_manual]

        if (
            gemini_ordered
            and gemini_report is not None
            and not self._should_switch_current(gemini_report, gemini_ordered[0], AI_GEMINI_GROUP)
        ):
            cur_gemini = getattr(gemini_report, "current", None)
            cur_gemini_verdict = getattr(gemini_report, "current_verdict", None)
            cur_gemini_summary = next(
                (s for s in getattr(gemini_report, "summaries", ()) or () if getattr(s, "name", None) == cur_gemini),
                None,
            )
            best_gemini_summary = next(
                (
                    s
                    for s in getattr(gemini_report, "summaries", ()) or ()
                    if getattr(s, "name", None) == gemini_ordered[0]
                ),
                None,
            )
            cur_gemini_usable = bool(
                cur_gemini_verdict is not None
                and (
                    getattr(cur_gemini_verdict, "stable", False)
                    or cur_gemini_summary is None
                    or (
                        getattr(cur_gemini_summary, "success_count", 0) > 0
                        and getattr(cur_gemini_summary, "success_count", 0)
                        >= getattr(best_gemini_summary, "success_count", 0)
                    )
                )
            )
            if (
                cur_gemini
                and cur_gemini_usable
                and cur_gemini in gemini_ordered
                and not (
                    is_static_iepl_node(cur_gemini, self.paths)
                    and not is_static_iepl_node(gemini_ordered[0], self.paths)
                )
            ):
                gemini_ordered = [cur_gemini] + [n for n in gemini_ordered if n != cur_gemini]

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

        rerendered = False
        hot_reloaded = False
        if is_external:
            running = True
        else:
            target_runtime = runtime_file(self.paths)
            before_bytes = target_runtime.read_bytes() if target_runtime.exists() else None
            process = ProcessBackend(self.paths)
            running = process.is_running()
            was_stale_before_render = (
                getattr(process, "is_runtime_stale", lambda _path=None: False)(target_runtime)
                if running and target_runtime.exists()
                else False
            )
            runtime_path = RuntimeBackend(self.paths).render_runtime()
            after_bytes = runtime_path.read_bytes() if runtime_path.exists() else None
            runtime_changed = before_bytes != after_bytes
            rerendered = True

            is_stale = getattr(process, "is_runtime_stale", lambda _path=None: False)(runtime_path)
            if running and (order_updated or gemini_order_updated or runtime_changed or is_stale):
                from ..backend.runtime import is_only_ai_group_order_change
                from .ai_watch import has_any_inflight_ai_connection

                api_instance = APIBackend(self.paths)
                if not has_any_inflight_ai_connection(self.paths, api_instance):
                    try:
                        api_instance.reload_config(str(runtime_path))
                        hot_reloaded = True
                    except Exception:
                        # 热重载失败留给下一轮 cproxy render/rebalance 兜底，不阻断
                        hot_reloaded = False
                    if hot_reloaded and hasattr(process, "mark_runtime_applied"):
                        process.mark_runtime_applied(runtime_path)
                elif (
                    not was_stale_before_render
                    and before_bytes is not None
                    and after_bytes is not None
                    and hasattr(process, "mark_runtime_applied")
                    and is_only_ai_group_order_change(
                        before_bytes.decode("utf-8", errors="replace"),
                        after_bytes.decode("utf-8", errors="replace"),
                    )
                ):
                    process.mark_runtime_applied(runtime_path)

        switched_to: str | None = None
        if running and effective_order:
            switched_to = self._switch_if_current_unstable(report, effective_order[0], AI_MANUAL_GROUP)

        gemini_switched_to: str | None = None
        if running and gemini_report is not None and effective_gemini_order:
            gemini_switched_to = self._switch_if_current_unstable(
                gemini_report, effective_gemini_order[0], AI_GEMINI_GROUP
            )
            current_gemini_leaf = gemini_switched_to or getattr(gemini_report, "current", None)
            if type(probe_service) is ProbeService and is_static_iepl_node(current_gemini_leaf, self.paths):
                effective_gemini_order, gemini_switched_to = self._verify_gemini_stream_hold(
                    list(effective_gemini_order),
                    gemini_order_path,
                    gemini_switched_to,
                )

        return RebalanceReport(
            ordered=tuple(effective_order),
            order_path=str(order_path),
            rerendered=rerendered,
            hot_reloaded=hot_reloaded,
            reason="external-only" if is_external else "",
            switched_to=switched_to,
            gemini_ordered=tuple(effective_gemini_order),
            gemini_order_path=str(gemini_order_path) if effective_gemini_order else "",
            gemini_switched_to=gemini_switched_to,
        )

    def _verify_gemini_stream_hold(
        self,
        ordered: list[str],
        order_path,
        switched_to: str | None,
        max_attempts: int = 3,
    ) -> tuple[list[str], str | None]:
        """对 AI-GEMINI 当前生效节点做一次模拟长连接保持验毒（Hold Probe）。

        若当前节点在保持期间发生 15s 空闲 EOF 或 TLS bad record MAC，则将其降级到列表尾部，
        并依次切换到下一个候选复测，确保最终落地的 AI-GEMINI 出口具备长连接存活能力。
        """
        from .probe import simulate_gemini_stream_hold

        try:
            query = QueryService(self.paths)
            group_obj = query.api.get_groups().get(AI_GEMINI_GROUP)
            if group_obj is None:
                return ordered, switched_to
            active_node = switched_to or str(group_obj.current or "") or (ordered[0] if ordered else "")
            proxy_port = RuntimeBackend(self.paths).mixed_port()
        except Exception:
            return ordered, switched_to

        tried: set[str] = set()
        demoted = False
        for _ in range(max(1, max_attempts)):
            if not active_node or active_node in tried:
                break
            tried.add(active_node)
            ok, _reason = simulate_gemini_stream_hold(proxy_port=proxy_port)
            if ok:
                clear_stream_fragile_node(self.paths, active_node)
                break
            record_stream_fragile_node(self.paths, active_node)
            if active_node in ordered:
                ordered.remove(active_node)
                ordered.append(active_node)
                demoted = True
            next_candidate = next((c for c in ordered if c not in tried), None)
            if not next_candidate:
                break
            try:
                should_drop = is_static_iepl_node(active_node, self.paths)
                try:
                    query.switch_group(AI_GEMINI_GROUP, next_candidate, drop_connections=should_drop)
                except TypeError:
                    query.switch_group(AI_GEMINI_GROUP, next_candidate)
                switched_to = next_candidate
                active_node = next_candidate
            except Exception:
                break

        if demoted:
            try:
                order_path.parent.mkdir(parents=True, exist_ok=True)
                order_path.write_text(json.dumps(ordered, ensure_ascii=False), encoding="utf-8")
            except OSError:
                pass
        return ordered, switched_to

    @staticmethod
    def _should_switch_current(
        report,
        best_candidate: str,
        group_name: str = AI_MANUAL_GROUP,
    ) -> bool:
        current_verdict = getattr(report, "current_verdict", None)
        best_verdict = getattr(report, "verdict", None)
        if current_verdict is None or best_verdict is None:
            return False
        current_node = getattr(report, "current", None)
        if not best_candidate or best_candidate == current_node:
            return False
        summaries = getattr(report, "summaries", ()) or ()
        best_summary = next((s for s in summaries if getattr(s, "name", None) == best_candidate), None)
        cur_summary = next((s for s in summaries if getattr(s, "name", None) == current_node), None)
        cur_is_failing = cur_summary is not None and getattr(cur_summary, "success_count", 0) <= getattr(
            cur_summary, "failures", 0
        )
        is_managed_ai = group_name in (AI_GEMINI_GROUP, AI_MANUAL_GROUP)
        migrating_off_static_iepl_with_majority_1x = (
            is_managed_ai
            and (is_static_iepl_node(current_node) or cur_is_failing)
            and best_summary is not None
            and not is_static_iepl_node(best_candidate)
            and getattr(best_summary, "success_count", 0) >= 4
            and getattr(best_summary, "success_count", 0) > getattr(best_summary, "failures", 0)
            and (getattr(best_summary, "avg_delay", None) or 9999) <= 1350
        )
        if not best_verdict.stable and not migrating_off_static_iepl_with_majority_1x:
            return False
        if (
            best_summary is not None
            and getattr(best_summary, "failures", 0) > 0
            and not migrating_off_static_iepl_with_majority_1x
        ):
            return False
        cur_region = match_region(str(current_node or ""))
        migrating_gemini_off_iepl = (
            is_managed_ai
            and (is_static_iepl_node(current_node) or cur_is_failing)
            and (is_healthy_non_iepl_summary(best_summary) or migrating_off_static_iepl_with_majority_1x)
        )
        migrating_gemini_off_slow = (
            is_managed_ai
            and cur_summary is not None
            and best_summary is not None
            and is_healthy_non_iepl_summary(best_summary)
            and (
                not is_healthy_non_iepl_summary(cur_summary)
                or (
                    cur_region not in GEMINI_FAST_REGIONS
                    and cur_region not in ("OTHER", None)
                    and cur_summary.avg_delay is not None
                    and best_summary.avg_delay is not None
                    and cur_summary.avg_delay - best_summary.avg_delay >= 180
                )
            )
        )
        if current_verdict.stable and not migrating_gemini_off_iepl and not migrating_gemini_off_slow:
            cur_penalty = getattr(cur_summary, "history_penalty_ms", 0) if cur_summary else 0
            best_penalty = getattr(best_summary, "history_penalty_ms", 0) if best_summary else 0
            if cur_penalty < CHRONIC_PENALTY_SWITCH_THRESHOLD_MS or best_penalty * 2 > cur_penalty:
                return False
        return True

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
        if not self._should_switch_current(report, best_candidate, group_name):
            return None
        try:
            query = QueryService(self.paths)
            group_obj = query.api.get_groups().get(group_name)
            if group_obj is None:
                return None
            if group_name == AI_MANUAL_GROUP and group_obj.current == AI_AUTO_GROUP:
                return None
            current_node = getattr(report, "current", None) or getattr(group_obj, "current", None)
            from .ai_watch import has_active_ai_transfer

            if not is_static_iepl_node(current_node, self.paths) and has_active_ai_transfer(
                self.paths, getattr(query, "api", None), group_name, current_node
            ):
                return None
            should_drop = group_name not in (AI_GEMINI_GROUP, AI_MANUAL_GROUP) or is_static_iepl_node(current_node, self.paths)
            try:
                query.switch_group(group_name, best_candidate, drop_connections=should_drop)
            except TypeError:
                query.switch_group(group_name, best_candidate)
            return best_candidate
        except Exception:
            return None
