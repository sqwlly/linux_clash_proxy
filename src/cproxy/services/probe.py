from __future__ import annotations

from dataclasses import dataclass
from math import ceil

from ..backend.api import APIBackend, APIUnavailableError
from ..backend.models import ProxyGroup
from ..config import AppPaths

DEFAULT_GROUP = "AI-MANUAL"
DEFAULT_TIMEOUT_MS = 8000


@dataclass(frozen=True)
class ProbeStrategy:
    min_rounds: int
    default_rounds: int
    max_delay_ms: int
    max_avg_ms: int
    min_improvement_ms: int
    min_improvement_ratio: float


STRATEGIES = {
    "conservative": ProbeStrategy(5, 5, 3000, 1500, 100, 0.20),
    "balanced": ProbeStrategy(3, 3, 3500, 1800, 75, 0.15),
    "aggressive": ProbeStrategy(3, 3, 4500, 2200, 50, 0.10),
}

PROFILES = {
    "codex": {"url": "https://chatgpt.com/backend-api/codex/responses/compact", "strategy": "conservative"},
    "chatgpt": {"url": "https://chatgpt.com", "strategy": "balanced"},
    "github": {"url": "https://github.com", "strategy": "balanced"},
    "claude": {"url": "https://claude.ai", "strategy": "conservative"},
}


@dataclass(frozen=True)
class ProbeSummary:
    name: str
    delays: tuple[int, ...]
    failures: int

    @property
    def success_count(self) -> int:
        return len(self.delays)

    @property
    def avg_delay(self) -> int | None:
        return round(sum(self.delays) / len(self.delays)) if self.delays else None

    @property
    def max_delay(self) -> int | None:
        return max(self.delays) if self.delays else None

    @property
    def min_delay(self) -> int | None:
        return min(self.delays) if self.delays else None

    def rank_key(self) -> tuple[int, int, int, int, str]:
        _penalty = 1_000_000
        max_d = self.max_delay if self.max_delay is not None else _penalty
        avg_d = self.avg_delay if self.avg_delay is not None else _penalty
        return (-self.success_count, self.failures, max_d, avg_d, self.name)


@dataclass(frozen=True)
class StabilityVerdict:
    stable: bool
    reason: str


@dataclass(frozen=True)
class ProbeReport:
    group: str
    profile: str
    strategy_name: str
    strategy: ProbeStrategy
    rounds: int
    url: str
    current: str | None
    current_verdict: StabilityVerdict
    best: ProbeSummary | None
    verdict: StabilityVerdict
    summaries: tuple[ProbeSummary, ...]
    switch_requested: bool
    switched: bool
    skip_reason: str
    preview_reason: str


def format_delay(value: int | None) -> str:
    return f"{value}ms" if value is not None else "-"


def _children(groups: dict[str, ProxyGroup], name: str) -> list[str]:
    group = groups.get(name)
    return list(group.candidates) if group else []


def collect_leaf_candidates(
    groups: dict[str, ProxyGroup], group_name: str
) -> tuple[list[str], dict[str, list[tuple[str, str]]]]:
    candidates: list[str] = []
    switch_paths: dict[str, list[tuple[str, str]]] = {}

    def visit(name: str, path: list[tuple[str, str]], seen: set[str]) -> None:
        children = _children(groups, name)
        if not children:
            if name not in switch_paths:
                candidates.append(name)
                switch_paths[name] = path
            return
        if name in seen:
            return
        for child in children:
            visit(child, [*path, (name, child)], seen | {name})

    visit(group_name, [], set())
    if not candidates:
        return [group_name], {group_name: []}
    return candidates, switch_paths


def resolve_current_leaf(groups: dict[str, ProxyGroup], group_name: str) -> str | None:
    name = group_name
    seen: set[str] = set()
    while True:
        if name in seen:
            return name
        seen.add(name)
        group = groups.get(name)
        if group is None:
            return name if name != group_name else None
        current = group.current
        if not current or current == "-":
            return None
        if _children(groups, current):
            name = current
            continue
        return current


def active_candidates_after_round(results: dict[str, dict], current: str | None) -> set[str]:
    summaries = [
        ProbeSummary(name=n, delays=tuple(d["delays"]), failures=d["failures"])
        for n, d in results.items()
    ]
    keep = max(1, ceil(len(summaries) / 2))
    active = {s.name for s in sorted(summaries, key=lambda s: s.rank_key())[:keep]}
    if current and current in results:
        active.add(current)
    return active


def stability_verdict(summary: ProbeSummary | None, rounds: int, strategy: ProbeStrategy) -> StabilityVerdict:
    if summary is None:
        return StabilityVerdict(False, "没有成功节点")
    if rounds < strategy.min_rounds:
        return StabilityVerdict(False, f"切换要求至少 {strategy.min_rounds} 轮探测，当前 {rounds} 轮")
    if summary.success_count != rounds:
        return StabilityVerdict(False, f"成功 {summary.success_count}/{rounds}，要求全成功")
    if summary.failures:
        return StabilityVerdict(False, f"失败 {summary.failures} 次，要求 0 失败")
    if summary.max_delay is None or summary.avg_delay is None:
        return StabilityVerdict(False, "缺少有效延迟数据")
    if summary.max_delay > strategy.max_delay_ms:
        return StabilityVerdict(False, f"最大延迟 {summary.max_delay}ms 超过 {strategy.max_delay_ms}ms")
    if summary.avg_delay > strategy.max_avg_ms:
        return StabilityVerdict(False, f"平均延迟 {summary.avg_delay}ms 超过 {strategy.max_avg_ms}ms")
    return StabilityVerdict(True, "满足稳定门槛")


def stable_score(summary: ProbeSummary, rounds: int, strategy: ProbeStrategy) -> int:
    if rounds < 1:
        return 0
    score = min(summary.success_count, rounds) / rounds * 100
    score -= summary.failures * 20
    if summary.avg_delay is not None:
        score -= min(25, (summary.avg_delay / strategy.max_avg_ms) * 20)
    if summary.max_delay is not None:
        score -= min(20, (summary.max_delay / strategy.max_delay_ms) * 15)
    if summary.max_delay is not None and summary.min_delay is not None:
        score -= min(15, ((summary.max_delay - summary.min_delay) / strategy.max_delay_ms) * 30)
    return max(0, min(100, round(score)))


def switch_skip_reason(
    best: ProbeSummary | None,
    current: str | None,
    current_verdict: StabilityVerdict,
    current_summary: ProbeSummary | None,
    strategy: ProbeStrategy,
) -> str:
    if best is None:
        return "没有可切换的成功节点"
    if current == best.name:
        return "当前已是推荐稳定节点"
    if not current_verdict.stable or current_summary is None:
        return ""
    if best.avg_delay is None or current_summary.avg_delay is None:
        return ""
    improvement = current_summary.avg_delay - best.avg_delay
    required = max(strategy.min_improvement_ms, round(current_summary.avg_delay * strategy.min_improvement_ratio))
    if improvement < required:
        return f"当前节点也稳定，平均延迟改善 {improvement}ms 未达到 {required}ms 防抖门槛"
    return ""


def _preview_reason(
    best: ProbeSummary | None,
    verdict: StabilityVerdict,
    current: str | None,
    current_verdict: StabilityVerdict,
    current_summary: ProbeSummary | None,
    strategy: ProbeStrategy,
) -> str:
    if not verdict.stable:
        return verdict.reason
    return switch_skip_reason(best, current, current_verdict, current_summary, strategy)


class ProbeService:
    def __init__(self, paths: AppPaths):
        self.paths = paths
        self.api = APIBackend(paths)

    def probe(
        self,
        group: str = DEFAULT_GROUP,
        profile: str = "codex",
        strategy_name: str | None = None,
        url: str | None = None,
        rounds: int | None = None,
        timeout: int = DEFAULT_TIMEOUT_MS,
        switch: bool = False,
    ) -> ProbeReport:
        prof = PROFILES[profile]
        strat_name = strategy_name or str(prof["strategy"])
        strategy = STRATEGIES[strat_name]
        probe_url = url or str(prof["url"])
        probe_rounds = rounds if rounds is not None else strategy.default_rounds
        if probe_rounds < 1:
            raise ValueError(f"错误: rounds 必须大于 0，当前值 {probe_rounds}")
        if timeout < 1:
            raise ValueError(f"错误: timeout 必须大于 0，当前值 {timeout}")
        req_timeout = max(10, timeout // 1000 + 2)

        groups = self.api.get_groups()
        if group not in groups:
            raise RuntimeError(f"错误: 未找到代理组或节点: {group}")

        candidates, switch_paths = collect_leaf_candidates(groups, group)
        current = resolve_current_leaf(groups, group)

        results: dict[str, dict] = {n: {"delays": [], "failures": 0} for n in candidates}
        active = set(candidates)

        for rnd in range(probe_rounds):
            for name in [c for c in candidates if c in active]:
                try:
                    payload = self.api.delay_test(name, probe_url, timeout, request_timeout=req_timeout)
                    delay = int(payload["delay"])
                except (APIUnavailableError, KeyError, TypeError, ValueError):
                    delay = None
                if delay is None:
                    results[name]["failures"] += 1
                else:
                    results[name]["delays"].append(delay)

            if rnd < probe_rounds - 1 and len(active) > 1:
                active = active_candidates_after_round({n: results[n] for n in active}, current)

        summaries = tuple(
            ProbeSummary(name=n, delays=tuple(d["delays"]), failures=d["failures"])
            for n, d in results.items()
        )
        successful = [s for s in summaries if s.success_count > 0]
        best = min(successful, key=lambda s: s.rank_key()) if successful else None
        verdict = stability_verdict(best, probe_rounds, strategy)
        current_summary = next((s for s in summaries if s.name == current), None)
        current_verdict = stability_verdict(current_summary, probe_rounds, strategy)
        preview = _preview_reason(best, verdict, current, current_verdict, current_summary, strategy)

        switched = False
        skip_reason = ""
        if switch:
            skip_reason = preview
            if not skip_reason:
                if best is None:
                    raise RuntimeError("错误: 没有可切换的成功节点")
                path = switch_paths.get(best.name)
                if path is None:
                    raise RuntimeError(f"错误: 未找到推荐节点的切换路径: {best.name}")
                for grp, target in reversed(path):
                    self.api.switch_group(grp, target)
                switched = True

        return ProbeReport(
            group=group, profile=profile, strategy_name=strat_name, strategy=strategy,
            rounds=probe_rounds, url=probe_url, current=current,
            current_verdict=current_verdict, best=best, verdict=verdict,
            summaries=summaries, switch_requested=switch, switched=switched,
            skip_reason=skip_reason, preview_reason=preview,
        )
