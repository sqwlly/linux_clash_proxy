from __future__ import annotations

import socket
import ssl
import time
from dataclasses import dataclass
from math import ceil

from ..backend.api import APIBackend, APIUnavailableError
from ..backend.models import ProxyGroup
from ..config import AppPaths
from ..feedback import ProgressLine
from ..names import normalize_name
from .probe_history import load_history_penalties, probe_history_file, record_probe_history

DEFAULT_GROUP = "AI-MANUAL"
DEFAULT_TIMEOUT_MS = 8000
GEMINI_HOLD_HOST = "daily-cloudcode-pa.googleapis.com"
GEMINI_HOLD_PORT = 443
GEMINI_HOLD_PATH = "/v1internal:streamGenerateContent?alt=sse"
GEMINI_HOLD_SECONDS = 15.5


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
    "gemini": {"url": "https://generativelanguage.googleapis.com/v1beta/models", "strategy": "conservative"},
}


def default_group_for_profile(profile: str) -> str:
    return "AI-GEMINI" if profile == "gemini" else DEFAULT_GROUP


@dataclass(frozen=True)
class ProbeSummary:
    name: str
    delays: tuple[int, ...]
    failures: int
    history_penalty_ms: int = 0

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

    def rank_key(self) -> tuple[int, int, int, int, int, str]:
        _penalty = 1_000_000
        max_d = self.max_delay if self.max_delay is not None else _penalty
        avg_d = self.avg_delay if self.avg_delay is not None else _penalty
        penalty = max(0, self.history_penalty_ms)
        return (-self.success_count, self.failures, max_d + penalty, avg_d + penalty, penalty, self.name)


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


from contextlib import contextmanager
from typing import Iterator

_IEPL_KEYWORDS = ("IEPL", "IPLC", "内网专线")
STREAM_FRAGILE_TTL_SECONDS = 1800.0
_ACTIVE_PATHS: AppPaths | None = None
_CONFIG_FRAGILE_CACHE: tuple[str, int, frozenset[str]] = ("", -1, frozenset())
_DYNAMIC_FRAGILE_CACHE: tuple[str, int, dict[str, float]] = ("", -1, {})


@contextmanager
def probe_paths_context(paths: AppPaths | None) -> Iterator[None]:
    global _ACTIVE_PATHS
    prev = _ACTIVE_PATHS
    _ACTIVE_PATHS = paths
    try:
        yield
    finally:
        _ACTIVE_PATHS = prev


def _load_config_fragile_nodes(paths: AppPaths) -> frozenset[str]:
    global _CONFIG_FRAGILE_CACHE
    try:
        from ..backend.runtime import is_stream_fragile_proxy
        from ..config import config_file, read_config

        cfg_path = config_file(paths)
        if not cfg_path.exists():
            return frozenset()
        mtime_ns = cfg_path.stat().st_mtime_ns
        key = str(cfg_path)
        if _CONFIG_FRAGILE_CACHE[0] == key and _CONFIG_FRAGILE_CACHE[1] == mtime_ns:
            return _CONFIG_FRAGILE_CACHE[2]
        cfg = read_config(paths)
        fragile = frozenset(
            str(p.get("name") or "")
            for p in (cfg.get("proxies") or [])
            if isinstance(p, dict) and p.get("name") and is_stream_fragile_proxy(p, str(p.get("name") or ""))
        )
        _CONFIG_FRAGILE_CACHE = (key, mtime_ns, fragile)
        return fragile
    except Exception:
        return frozenset()


def _load_dynamic_fragile_map(paths: AppPaths) -> dict[str, float]:
    global _DYNAMIC_FRAGILE_CACHE
    try:
        import json
        from ..config import ai_watch_state_file

        state_path = ai_watch_state_file(paths)
        if not state_path.exists():
            return {}
        mtime_ns = state_path.stat().st_mtime_ns
        key = str(state_path)
        if _DYNAMIC_FRAGILE_CACHE[0] == key and _DYNAMIC_FRAGILE_CACHE[1] == mtime_ns:
            return _DYNAMIC_FRAGILE_CACHE[2]
        data = json.loads(state_path.read_text(encoding="utf-8"))
        raw = data.get("stream_fragile") if isinstance(data, dict) else None
        parsed = (
            {str(k): float(v) for k, v in raw.items() if k and isinstance(v, (int, float))}
            if isinstance(raw, dict)
            else {}
        )
        _DYNAMIC_FRAGILE_CACHE = (key, mtime_ns, parsed)
        return parsed
    except Exception:
        return {}


def is_dynamic_stream_fragile(name: str | None, paths: AppPaths | None = None, *, now: float | None = None) -> bool:
    if not name:
        return False
    eff_paths = paths or _ACTIVE_PATHS
    if eff_paths is None:
        return False
    fragile_map = _load_dynamic_fragile_map(eff_paths)
    ts = fragile_map.get(str(name))
    if ts is None:
        return False
    ts_now = time.time() if now is None else now
    return (ts_now - float(ts)) < STREAM_FRAGILE_TTL_SECONDS


def record_stream_fragile_node(paths: AppPaths, name: str | None, *, now: float | None = None) -> None:
    global _DYNAMIC_FRAGILE_CACHE
    if not name:
        return
    try:
        import json
        from ..config import ai_watch_state_file

        state_path = ai_watch_state_file(paths)
        state_path.parent.mkdir(parents=True, exist_ok=True)
        data: dict = {}
        if state_path.exists():
            try:
                loaded = json.loads(state_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    data = loaded
            except Exception:
                data = {}
        ts_now = time.time() if now is None else now
        sf = data.get("stream_fragile")
        if not isinstance(sf, dict):
            sf = {}
        sf = {
            str(k): float(v)
            for k, v in sf.items()
            if k and isinstance(v, (int, float)) and (ts_now - float(v)) < STREAM_FRAGILE_TTL_SECONDS
        }
        sf[str(name)] = ts_now
        data["stream_fragile"] = sf
        tmp = state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        tmp.replace(state_path)
        _DYNAMIC_FRAGILE_CACHE = ("", -1, {})
    except Exception:
        pass


def clear_stream_fragile_node(paths: AppPaths, name: str | None) -> None:
    global _DYNAMIC_FRAGILE_CACHE
    if not name:
        return
    try:
        import json
        from ..config import ai_watch_state_file

        state_path = ai_watch_state_file(paths)
        if not state_path.exists():
            return
        loaded = json.loads(state_path.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict):
            return
        sf = loaded.get("stream_fragile")
        if not isinstance(sf, dict) or str(name) not in sf:
            return
        sf.pop(str(name), None)
        loaded["stream_fragile"] = sf
        tmp = state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(loaded, ensure_ascii=False), encoding="utf-8")
        tmp.replace(state_path)
        _DYNAMIC_FRAGILE_CACHE = ("", -1, {})
    except Exception:
        pass


def is_static_iepl_node(name: str | None, paths: AppPaths | None = None) -> bool:
    """判定节点是否为静态专线/裸 vision 易断流节点（名称含 IEPL/IPLC/内网专线，或底层协议为裸 TCP xtls-rprx-vision）。"""
    if not name:
        return False
    upper = str(name).upper()
    if any(kw in upper for kw in _IEPL_KEYWORDS):
        return True
    eff_paths = paths or _ACTIVE_PATHS
    if eff_paths is not None and str(name) in _load_config_fragile_nodes(eff_paths):
        return True
    return False


def is_iepl_node(name: str | None, paths: AppPaths | None = None) -> bool:
    """判定节点是否为长连接易断流节点（兼容任意机场命名与无 IEPL 节点场景）：
    1. 名称含 IEPL / IPLC / 内网专线；
    2. 协议指纹为裸 TCP + xtls-rprx-vision（如不带 IEPL 标签的 `TE` / `推荐` / 专线节点）；
    3. 动态实测断流记忆（近期在 Hold Probe 或真实 AI 长流中发生过断流 EOF / bad record MAC 的任意节点）。
    """
    if not name:
        return False
    if is_static_iepl_node(name, paths):
        return True
    eff_paths = paths or _ACTIVE_PATHS
    if eff_paths is not None and is_dynamic_stream_fragile(name, eff_paths):
        return True
    return False


is_stream_fragile_node = is_iepl_node


def simulate_gemini_stream_hold(
    proxy_host: str = "127.0.0.1",
    proxy_port: int = 7890,
    hold_seconds: float = GEMINI_HOLD_SECONDS,
    timeout: float = 10.0,
) -> tuple[bool, str]:
    """模拟 agy 的 Gemini 真实长连接：HTTP CONNECT → TLS 握手 → 保持连接越过 15s 空闲掐流阈值 →
    发送 streamGenerateContent POST 并校验 HTTP 响应状态行。

    用于捕获「短包 delay_test 看似正常，但 15s 空闲 / 50s 长思考必被中转机 EOF 掐断
    或触发 xtls-rprx-vision `tls: bad record MAC`」的节点。
    """
    raw_sock: socket.socket | None = None
    tls_sock: ssl.SSLSocket | None = None
    try:
        raw_sock = socket.create_connection((proxy_host, proxy_port), timeout=timeout)
        connect_req = (
            f"CONNECT {GEMINI_HOLD_HOST}:{GEMINI_HOLD_PORT} HTTP/1.1\r\n"
            f"Host: {GEMINI_HOLD_HOST}:{GEMINI_HOLD_PORT}\r\n\r\n"
        ).encode("ascii")
        raw_sock.sendall(connect_req)
        banner = raw_sock.recv(4096)
        first_line = banner.splitlines()[0].decode("latin-1", "replace") if banner else ""
        if " 200 " not in first_line:
            return False, f"CONNECT 失败: {first_line or 'EOF'}"

        ctx = ssl.create_default_context()
        tls_sock = ctx.wrap_socket(raw_sock, server_hostname=GEMINI_HOLD_HOST)
        tls_sock.settimeout(timeout)
        if hold_seconds > 0:
            time.sleep(hold_seconds)
        post_req = (
            f"POST {GEMINI_HOLD_PATH} HTTP/1.1\r\n"
            f"Host: {GEMINI_HOLD_HOST}\r\n"
            "Content-Type: application/json\r\n"
            "Content-Length: 2\r\n\r\n{}"
        ).encode("ascii")
        tls_sock.sendall(post_req)
        resp = tls_sock.recv(1024)
        if not resp:
            return False, "保持连接后读取 EOF"
        status_line = resp.splitlines()[0].decode("latin-1", "replace").strip()
        if not status_line.startswith("HTTP/1."):
            return False, f"非预期响应: {status_line}"
        return True, status_line
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"
    finally:
        if tls_sock is not None:
            try:
                tls_sock.close()
            except OSError:
                pass
        elif raw_sock is not None:
            try:
                raw_sock.close()
            except OSError:
                pass


NON_IEPL_HEALTHY_AVG_MS = 1350
NON_IEPL_HEALTHY_MAX_MS = 1800
GEMINI_FAST_REGIONS = frozenset({"US", "KR", "TW", "JP", "SG", "GB"})


def is_healthy_non_iepl_summary(
    summary: ProbeSummary | None, *, ignore_dynamic_fragile: bool = False
) -> bool:
    if summary is None:
        return False
    if ignore_dynamic_fragile:
        if is_static_iepl_node(summary.name):
            return False
    elif is_iepl_node(summary.name):
        return False
    if summary.success_count <= 0 or summary.failures > 0:
        return False
    if summary.history_penalty_ms >= 200:
        return False
    if summary.avg_delay is None or summary.max_delay is None:
        return False
    return summary.avg_delay <= NON_IEPL_HEALTHY_AVG_MS and summary.max_delay <= NON_IEPL_HEALTHY_MAX_MS


def probe_rank_key(
    summary: ProbeSummary, *, prefer_non_iepl: bool = False
) -> tuple[int, int, int, int, int, int, int, str]:
    neg_success, failures, max_plus_p, avg_plus_p, penalty, name = summary.rank_key()
    healthy_non_iepl = is_healthy_non_iepl_summary(summary)
    if prefer_non_iepl:
        if healthy_non_iepl:
            iepl_tier = 0
        elif is_healthy_non_iepl_summary(summary, ignore_dynamic_fragile=True):
            iepl_tier = 1
        else:
            iepl_tier = 2
    else:
        iepl_tier = 0
    if prefer_non_iepl and iepl_tier < 2:
        from .nodelist import match_region

        reg = match_region(summary.name)
        region_tier = 0 if (reg in GEMINI_FAST_REGIONS or reg in ("OTHER", None)) else 1
    else:
        region_tier = 0
    return (neg_success, failures, iepl_tier, region_tier, max_plus_p, avg_plus_p, penalty, name)


def active_candidates_after_round(
    results: dict[str, dict],
    current: str | None,
    history_penalties: dict[str, int] | None = None,
    *,
    prefer_non_iepl: bool = False,
) -> set[str]:
    penalties = history_penalties or {}
    summaries = [
        ProbeSummary(
            name=n,
            delays=tuple(d["delays"]),
            failures=d["failures"],
            history_penalty_ms=penalties.get(n, 0),
        )
        for n, d in results.items()
    ]
    keep = max(1, ceil(len(summaries) / 2))
    if prefer_non_iepl:
        healthy_non_iepl_count = sum(1 for s in summaries if is_healthy_non_iepl_summary(s))
        keep = max(keep, healthy_non_iepl_count)
    active = {
        s.name for s in sorted(summaries, key=lambda s: probe_rank_key(s, prefer_non_iepl=prefer_non_iepl))[:keep]
    }
    if prefer_non_iepl:
        for s in summaries:
            if s.success_count > 0 and not is_static_iepl_node(s.name):
                active.add(s.name)
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
    score -= min(10, max(0, summary.history_penalty_ms) / 30)
    return max(0, min(100, round(score)))


def switch_skip_reason(
    best: ProbeSummary | None,
    current: str | None,
    current_verdict: StabilityVerdict,
    current_summary: ProbeSummary | None,
    strategy: ProbeStrategy,
    *,
    prefer_non_iepl: bool = False,
) -> str:
    if best is None:
        return "没有可切换的成功节点"
    if current == best.name:
        return "当前已是推荐稳定节点"
    if prefer_non_iepl and is_iepl_node(current) and is_healthy_non_iepl_summary(best):
        return ""
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
    *,
    prefer_non_iepl: bool = False,
) -> str:
    if not verdict.stable:
        if not current_verdict.stable:
            return switch_skip_reason(
                best, current, current_verdict, current_summary, strategy, prefer_non_iepl=prefer_non_iepl
            )
        return verdict.reason
    return switch_skip_reason(
        best, current, current_verdict, current_summary, strategy, prefer_non_iepl=prefer_non_iepl
    )


def _summary_payload(summary: ProbeSummary, rounds: int, strategy: ProbeStrategy) -> dict:
    return {
        "name": summary.name,
        "success": summary.success_count,
        "total": summary.success_count + summary.failures,
        "failures": summary.failures,
        "avg_ms": summary.avg_delay,
        "max_ms": summary.max_delay,
        "min_ms": summary.min_delay,
        "history_penalty_ms": summary.history_penalty_ms,
        "score": stable_score(summary, rounds, strategy),
    }


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
        record_history: bool = False,
        show_progress: bool = False,
    ) -> ProbeReport:
        with probe_paths_context(self.paths):
            return self._probe_impl(
                group=group,
                profile=profile,
                strategy_name=strategy_name,
                url=url,
                rounds=rounds,
                timeout=timeout,
                switch=switch,
                record_history=record_history,
                show_progress=show_progress,
            )

    def _probe_impl(
        self,
        group: str = DEFAULT_GROUP,
        profile: str = "codex",
        strategy_name: str | None = None,
        url: str | None = None,
        rounds: int | None = None,
        timeout: int = DEFAULT_TIMEOUT_MS,
        switch: bool = False,
        record_history: bool = False,
        show_progress: bool = False,
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

        history_path = probe_history_file(self.paths)
        penalties = load_history_penalties(history_path, group, profile, probe_url, strategy.default_rounds)

        groups = self.api.get_groups()
        if group not in groups:
            from ..names import resolve_group_alias

            aliased = resolve_group_alias(group, groups.keys())
            if aliased:
                group = aliased
        if group not in groups:
            from .switch_tree import suggest_groups_from_names

            hint = suggest_groups_from_names(group, list(groups.keys()))
            raise RuntimeError(f"错误: 未找到代理组或节点: {group}\n{hint}")

        from .ban import filter_unbanned_nodes, load_active_bans

        candidates, switch_paths = collect_leaf_candidates(groups, group)
        candidates = filter_unbanned_nodes(candidates, load_active_bans(self.paths), keep_fallback=True)
        current = resolve_current_leaf(groups, group)

        results: dict[str, dict] = {n: {"delays": [], "failures": 0} for n in candidates}
        active = set(candidates)

        progress = ProgressLine(enabled=show_progress)
        if show_progress:
            progress.commit(f"探测 | {group} | {len(candidates)} 节点 | {probe_rounds} 轮")

        for rnd in range(probe_rounds):
            round_candidates = [c for c in candidates if c in active]
            if show_progress:
                if progress.live:
                    progress.update(f"轮 {rnd + 1}/{probe_rounds} | {len(round_candidates)} 节点…")
                else:
                    progress.commit(f"轮 {rnd + 1}/{probe_rounds} | {len(round_candidates)} 节点...")
            for index, name in enumerate(round_candidates, start=1):
                try:
                    payload = self.api.delay_test(name, probe_url, timeout, request_timeout=req_timeout)
                    delay = int(payload["delay"])
                except (APIUnavailableError, KeyError, TypeError, ValueError):
                    delay = None
                if delay is None:
                    results[name]["failures"] += 1
                else:
                    results[name]["delays"].append(delay)
                if show_progress:
                    outcome = f"{delay}ms" if delay is not None else "超时"
                    progress.update(
                        f"轮 {rnd + 1}/{probe_rounds} | 节点 {index}/{len(round_candidates)}"
                        f" · {normalize_name(name)} {outcome}"
                    )

            if rnd < probe_rounds - 1 and len(active) > 1:
                before = len(active)
                active = active_candidates_after_round(
                    {n: results[n] for n in active},
                    current,
                    penalties,
                    prefer_non_iepl=(group in ("AI-GEMINI", "AI-MANUAL") or profile in ("gemini", "codex")),
                )
                if show_progress:
                    progress.commit(f"筛选 | 保留 {len(active)} | 淘汰 {before - len(active)}")

        if show_progress:
            progress.commit("完成")

        prefer_non_iepl = group in ("AI-GEMINI", "AI-MANUAL") or profile in ("gemini", "codex")
        summaries = tuple(
            ProbeSummary(
                name=n,
                delays=tuple(d["delays"]),
                failures=d["failures"],
                history_penalty_ms=penalties.get(n, 0),
            )
            for n, d in results.items()
        )
        successful = [s for s in summaries if s.success_count > 0]
        best = min(successful, key=lambda s: probe_rank_key(s, prefer_non_iepl=prefer_non_iepl)) if successful else None
        verdict = stability_verdict(best, probe_rounds, strategy)
        current_summary = next((s for s in summaries if s.name == current), None)
        current_verdict = stability_verdict(current_summary, probe_rounds, strategy)
        preview = _preview_reason(
            best, verdict, current, current_verdict, current_summary, strategy, prefer_non_iepl=prefer_non_iepl
        )

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
                try:
                    from .query import QueryService

                    query = QueryService(self.paths)
                    query.api = self.api
                    for grp, _target in reversed(path):
                        if grp != "AI-GEMINI" or is_iepl_node(current):
                            query._drop_stale_ai_connections(grp, keep_leaf=best.name)
                except Exception:
                    pass
                switched = True

        if record_history or switch:
            try:
                record_probe_history(
                    history_path,
                    profile=profile,
                    strategy_name=strat_name,
                    group=group,
                    url=probe_url,
                    rounds=probe_rounds,
                    timeout_ms=timeout,
                    current=current,
                    current_stable=current_verdict.stable,
                    current_reason=current_verdict.reason,
                    best=best.name if best else None,
                    stable=verdict.stable,
                    reason=verdict.reason,
                    switch_requested=switch,
                    switched=switched,
                    skip_reason=skip_reason,
                    nodes=[_summary_payload(s, probe_rounds, strategy) for s in summaries],
                )
            except OSError:
                pass

        return ProbeReport(
            group=group, profile=profile, strategy_name=strat_name, strategy=strategy,
            rounds=probe_rounds, url=probe_url, current=current,
            current_verdict=current_verdict, best=best, verdict=verdict,
            summaries=summaries, switch_requested=switch, switched=switched,
            skip_reason=skip_reason, preview_reason=preview,
        )
