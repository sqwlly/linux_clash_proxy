"""`cproxy benchmark` —— 机场订阅健康度与质量评分卡服务。

汇总主订阅与各附加订阅（`subscriptions`）的所有真实节点，结合 Mihomo 实时健康检查与
节点标签解析引擎（:mod:`cproxy.services.node_tags`），从 6 个维度评估每家机场：
- 节点总数与地区覆盖数
- 存活率（Alive %）
- 存活节点平均延迟与最低延迟
- 专线（IEPL/IPLC）占比
- AI 就绪节点数（美/日/新/韩/英/欧等地区且延迟 < 500ms）
- 高倍率节点（>= 3.0X）占比
最终算出 0–100 综合健康分与评级（S / A / B / C）。
"""

from __future__ import annotations

from dataclasses import dataclass
from statistics import median
from typing import Any

from ..backend.api import APIBackend, APIUnavailableError
from ..backend.runtime import HEALTH_CHECK_TIMEOUT_MS
from ..config import AppPaths, config_file, load_yaml_file, runtime_file
from .node_tags import parse_node_tag
from .nodelist import is_panel_info_node

AI_CORE_REGIONS = frozenset({"US", "JP", "SG", "KR", "GB", "TW", "DE", "FR"})


@dataclass
class ProviderScorecard:
    name: str
    total_nodes: int
    alive_nodes: int
    avg_delay_ms: int | None
    median_delay_ms: int | None
    min_delay_ms: int | None
    regions_count: int
    dedicated_count: int
    ai_ready_count: int
    high_mult_count: int
    score: int
    grade: str

    @property
    def alive_rate(self) -> float:
        return (self.alive_nodes / self.total_nodes * 100.0) if self.total_nodes > 0 else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "total_nodes": self.total_nodes,
            "alive_nodes": self.alive_nodes,
            "alive_rate": round(self.alive_rate, 1),
            "avg_delay_ms": self.avg_delay_ms,
            "median_delay_ms": self.median_delay_ms,
            "min_delay_ms": self.min_delay_ms,
            "regions_count": self.regions_count,
            "dedicated_count": self.dedicated_count,
            "ai_ready_count": self.ai_ready_count,
            "high_mult_count": self.high_mult_count,
            "score": self.score,
            "grade": self.grade,
        }


def _compute_score_and_grade(
    total: int,
    alive: int,
    avg_delay: int | None,
    regions: int,
    dedicated: int,
    ai_ready: int,
) -> tuple[int, str]:
    if total == 0:
        return 0, "C"
    alive_ratio = alive / total
    # 存活率占 45 分
    score = alive_ratio * 45.0
    # 延迟表现占 25 分（<=150ms 满分，>=800ms 0分）
    if avg_delay is not None and avg_delay > 0:
        delay_factor = max(0.0, min(1.0, (800.0 - avg_delay) / 650.0))
        score += delay_factor * 25.0
    # AI 可用节点储备占 15 分（>=6 个满分）
    score += min(1.0, ai_ready / 6.0) * 15.0
    # 地区多样性占 10 分（>=5 个地区满分）
    score += min(1.0, regions / 5.0) * 10.0
    # 专线加成占 5 分
    if dedicated > 0:
        score += min(5.0, (dedicated / max(1, total)) * 10.0)

    final_score = max(0, min(100, round(score)))
    if final_score >= 85:
        grade = "S"
    elif final_score >= 70:
        grade = "A"
    elif final_score >= 50:
        grade = "B"
    else:
        grade = "C"
    return final_score, grade


def evaluate_providers(paths: AppPaths) -> list[ProviderScorecard]:
    """评估主订阅与各附加订阅的健康度评分卡。"""
    rfile = runtime_file(paths)
    cfile = config_file(paths)
    data = load_yaml_file(rfile) if rfile.exists() else load_yaml_file(cfile)
    if not isinstance(data, dict):
        data = {}

    extra_names: list[str] = []
    for item in data.get("subscriptions") or []:
        if isinstance(item, dict):
            n = str(item.get("name") or "").strip()
            if n and n not in extra_names:
                extra_names.append(n)

    raw_proxies = [
        str(p.get("name"))
        for p in (data.get("proxies") or [])
        if isinstance(p, dict) and p.get("name") and not is_panel_info_node(str(p.get("name")))
    ]

    live_groups = {}
    try:
        live_groups = APIBackend(paths).get_groups(request_timeout=1.5)
    except APIUnavailableError:
        pass

    buckets: dict[str, list[str]] = {"主订阅": []}
    for extra in extra_names:
        buckets[extra] = []

    for node_name in raw_proxies:
        matched_extra = None
        for extra in extra_names:
            if node_name == extra or node_name.startswith(f"{extra} "):
                matched_extra = extra
                break
        if matched_extra:
            buckets[matched_extra].append(node_name)
        else:
            buckets["主订阅"].append(node_name)

    cards: list[ProviderScorecard] = []
    for provider_name, nodes in buckets.items():
        if not nodes:
            continue
        alive_count = 0
        delays: list[int] = []
        regions: set[str] = set()
        dedicated_count = 0
        ai_ready_count = 0
        high_mult_count = 0

        for name in nodes:
            tag = parse_node_tag(name)
            if tag.region != "OTHER":
                regions.add(tag.region)
            if tag.is_dedicated_line:
                dedicated_count += 1
            if tag.is_high_multiplier:
                high_mult_count += 1

            entry = live_groups.get(name)
            delay = entry.delay if entry is not None else None
            alive = entry.alive if entry is not None else None
            is_ok = (
                delay is not None
                and 0 < delay < HEALTH_CHECK_TIMEOUT_MS
                and alive is not False
            )
            if is_ok and delay is not None:
                alive_count += 1
                delays.append(delay)
                if tag.region in AI_CORE_REGIONS and delay < 500 and not tag.is_high_multiplier:
                    ai_ready_count += 1
            elif not live_groups and alive is not False:
                # API 未运行时按静态节点数估算存活
                alive_count += 1

        avg_d = round(sum(delays) / len(delays)) if delays else None
        med_d = round(median(delays)) if delays else None
        min_d = min(delays) if delays else None
        score, grade = _compute_score_and_grade(
            len(nodes),
            alive_count,
            avg_d,
            len(regions),
            dedicated_count,
            ai_ready_count,
        )
        cards.append(
            ProviderScorecard(
                name=provider_name,
                total_nodes=len(nodes),
                alive_nodes=alive_count,
                avg_delay_ms=avg_d,
                median_delay_ms=med_d,
                min_delay_ms=min_d,
                regions_count=len(regions),
                dedicated_count=dedicated_count,
                ai_ready_count=ai_ready_count,
                high_mult_count=high_mult_count,
                score=score,
                grade=grade,
            )
        )
    return cards
