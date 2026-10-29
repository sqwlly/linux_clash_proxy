from __future__ import annotations

import os
import subprocess
import sys
import time
from collections import Counter, defaultdict
from dataclasses import dataclass

from ..backend.api import APIBackend
from ..backend.process import SYSTEMD_SERVICE_NAME
from ..backend.runtime import AI_STATUS_GROUPS, ai_exit_metrics
from ..config import AppPaths
from ..proxyenv import proxy_http_url
from .probe import ProbeService, default_group_for_profile
from .probe_history import load_history_rows, probe_history_file

AI_HOST_KEYWORDS = (
    "chatgpt", "openai", "claude", "anthropic", "github",
    "gemini", "google", "antigravity", "googleapis",
    "moonshot", "minimaxi", "minimax", "deepseek", "dashscope",
    "grok", "x.ai",
)
AI_KNOWN_PROCESSES = (
    "agy", "claude", "codex", "gemini", "cursor", "aider", "opencode", "kimi",
)


@dataclass(frozen=True)
class AIConnection:
    host: str
    count: int
    route: str


@dataclass(frozen=True)
class AIProcessRoute:
    process: str
    host: str
    count: int
    route: str


@dataclass(frozen=True)
class IncidentSection:
    title: str
    lines: list[str]


def _process_basename(metadata: dict) -> str:
    raw = str(metadata.get("process") or metadata.get("processPath") or "").strip()
    if not raw:
        return "-"
    base = raw.replace("\\", "/").rsplit("/", 1)[-1].removesuffix(" (deleted)")
    if base.lower().startswith("agy.") and base.lower().endswith(".old"):
        return "agy"
    return base or "-"


def _is_known_ai_process(proc_name: str) -> bool:
    lower = proc_name.lower()
    return any(lower == item or lower.startswith(f"{item}.") or lower.startswith(f"{item}-") for item in AI_KNOWN_PROCESSES)


def _format_merged_hosts(host_counts: Counter[str]) -> str:
    ordered = [host for host, _ in host_counts.most_common()]
    if not ordered:
        return "-"
    if len(ordered) == 1:
        return ordered[0]
    if len(ordered) == 2:
        return f"{ordered[0]}, {ordered[1]}"
    return f"{ordered[0]}, {ordered[1]} 等 {len(ordered)} 个"


def get_active_ai_processes(paths: AppPaths) -> list[AIProcessRoute]:
    """查询当前活跃的 AI 进程，按（进程, 顶层策略组）合并目标域名与连接数。"""
    api = APIBackend(paths)
    payload = api.get_connections()
    connections = payload.get("connections") if isinstance(payload, dict) else []
    if not isinstance(connections, list):
        return []

    counts: Counter[tuple[str, str]] = Counter()
    host_buckets: defaultdict[tuple[str, str], Counter[str]] = defaultdict(Counter)
    routes: defaultdict[tuple[str, str], Counter[str]] = defaultdict(Counter)
    for item in connections:
        if not isinstance(item, dict):
            continue
        metadata = item.get("metadata") or {}
        if not isinstance(metadata, dict):
            continue
        host = str(
            metadata.get("host")
            or metadata.get("sniffHost")
            or metadata.get("remoteDestination")
            or "-"
        ).lower()
        proc = _process_basename(metadata)
        if proc == "-":
            continue
        host_hit = any(keyword in host for keyword in AI_HOST_KEYWORDS)
        proc_hit = _is_known_ai_process(proc)
        if not host_hit and not proc_hit:
            continue
        chains = item.get("chains") or []
        policy_group = str(chains[-1]) if chains else "-"
        route = " -> ".join(str(part) for part in reversed(chains[:3])) if chains else "-"
        key = (proc, policy_group)
        counts[key] += 1
        host_buckets[key][host] += 1
        routes[key][route] += 1

    return [
        AIProcessRoute(
            process=proc,
            host=_format_merged_hosts(host_buckets[(proc, policy_group)]),
            count=count,
            route=routes[(proc, policy_group)].most_common(1)[0][0],
        )
        for (proc, policy_group), count in counts.most_common(15)
    ]


def get_ai_connections(paths: AppPaths) -> list[AIConnection]:
    api = APIBackend(paths)
    payload = api.get_connections()
    connections = payload.get("connections") if isinstance(payload, dict) else []
    if not isinstance(connections, list):
        raise RuntimeError("API 返回缺少 connections 字段")

    counts: Counter[str] = Counter()
    routes: defaultdict[str, Counter[str]] = defaultdict(Counter)
    for item in connections:
        if not isinstance(item, dict):
            continue
        metadata = item.get("metadata") or {}
        host = str(
            metadata.get("host")
            or metadata.get("sniffHost")
            or metadata.get("remoteDestination")
            or "-"
        ).lower()
        if not any(keyword in host for keyword in AI_HOST_KEYWORDS):
            continue
        chains = item.get("chains") or []
        route = " -> ".join(str(part) for part in chains[:3]) if chains else "-"
        counts[host] += 1
        routes[host][route] += 1

    return [
        AIConnection(host=host, count=count, route=routes[host].most_common(1)[0][0])
        for host, count in counts.most_common(20)
    ]


def guard(
    paths: AppPaths,
    profile: str = "codex",
    command: list[str] | None = None,
) -> int:
    report = ProbeService(paths).probe(
        group=default_group_for_profile(profile),
        profile=profile,
        switch=True,
        show_progress=True,
    )
    if not report.switched and report.skip_reason and report.skip_reason != "当前已是推荐稳定节点":
        if not report.current_verdict.stable:
            print(f"警告: AI 出口不稳定 ({report.skip_reason})", file=sys.stderr)

    if not command:
        return 0

    http_url = proxy_http_url(paths)
    env_patch = {
        "HTTP_PROXY": http_url,
        "HTTPS_PROXY": http_url,
        "ALL_PROXY": http_url.replace("http://", "socks5h://"),
        "http_proxy": http_url,
        "https_proxy": http_url,
        "all_proxy": http_url.replace("http://", "socks5h://"),
        "NO_PROXY": "127.0.0.1,localhost",
        "no_proxy": "127.0.0.1,localhost",
    }
    env = {**os.environ, **env_patch}
    result = subprocess.run(command, env=env)
    return result.returncode


def build_incident(paths: AppPaths, profile: str = "codex") -> list[IncidentSection]:
    sections: list[IncidentSection] = []

    header_lines = [
        f"time={time.strftime('%Y-%m-%dT%H:%M:%S%z')}",
        f"profile={profile}",
    ]
    sections.append(IncidentSection("事件报告", header_lines))

    service_lines: list[str] = []
    try:
        result = subprocess.run(
            ["systemctl", "--user", "show", SYSTEMD_SERVICE_NAME,
             "--property=ActiveState,SubState,MainPID,NRestarts", "--no-pager"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            service_lines.extend(result.stdout.strip().splitlines())
    except (FileNotFoundError, subprocess.TimeoutExpired):
        service_lines.append("systemctl 不可用")

    try:
        result = subprocess.run(
            ["pgrep", "-a", "mihomo"],
            capture_output=True, text=True, timeout=5,
        )
        if result.stdout.strip():
            service_lines.extend(result.stdout.strip().splitlines())
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    if not service_lines:
        service_lines.append("无 mihomo 进程")
    try:
        from .verge import collect_scheduler_telemetry

        sched = collect_scheduler_telemetry(paths)
        if sched.get("configured"):
            service_lines.append(
                f"SchedulerMode={sched.get('scheduler_mode')} "
                f"SchedulerOK={sched.get('daemon_ok')} "
                f"HeartbeatAgeSec={sched.get('heartbeat_age_sec')}"
            )
    except Exception:
        pass
    sections.append(IncidentSection("service", service_lines))

    ai_lines: list[str] = []
    try:
        api = APIBackend(paths)
        groups = api.get_groups()
        for name in AI_STATUS_GROUPS:
            group = groups.get(name)
            if group is None:
                ai_lines.append(f"{name}: 缺失")
                continue
            # 延迟取出口节点的真实值：fixed fallback 组（AI-MANUAL/AI-GEMINI）自身
            # history 恒空，直接读 group.delay 会打成 None
            _node, delay, _alive = ai_exit_metrics(groups, name)
            ai_lines.append(
                f"{name}: type={group.type} now={group.current} alive={group.alive} "
                f"delay={'-' if delay is None else delay}"
            )
    except Exception as exc:
        ai_lines.append(f"API 不可访问: {exc}")
    sections.append(IncidentSection("ai-status", ai_lines))

    probe_lines: list[str] = []
    try:
        report = ProbeService(paths).probe(
            group=default_group_for_profile(profile),
            profile=profile,
        )
        probe_lines.append(f"STABLE\t{'true' if report.verdict.stable else 'false'}\t{report.verdict.reason}")
        if report.best:
            probe_lines.append(
                f"BEST\t{report.best.name}\tsuccess={report.best.success_count}/{report.rounds}"
                f"\tfailures={report.best.failures}\tavg={report.best.avg_delay}ms\tmax={report.best.max_delay}ms"
            )
        for summary in sorted(report.summaries, key=lambda s: s.rank_key()):
            probe_lines.append(
                f"NODE\t{summary.name}\tsuccess={summary.success_count}/{report.rounds}"
                f"\tfailures={summary.failures}\tavg={summary.avg_delay}ms\tmax={summary.max_delay}ms"
            )
    except Exception as exc:
        probe_lines.append(f"探测失败: {exc}")
    sections.append(IncidentSection("probe", probe_lines))

    conn_lines: list[str] = []
    try:
        connections = get_ai_connections(paths)
        if connections:
            for conn in connections:
                conn_lines.append(f"AI_CONNECTION\t{conn.host}\tcount={conn.count}\troute={conn.route}")
        else:
            conn_lines.append("无 AI 相关活动连接")
    except Exception as exc:
        conn_lines.append(f"连接查询失败: {exc}")
    sections.append(IncidentSection("connections", conn_lines))

    history_lines: list[str] = []
    try:
        rows = load_history_rows(probe_history_file(paths), 5)
        if rows:
            for item in rows:
                history_lines.append(
                    f"PROBE_HISTORY\tts={item.get('ts', '-')}\tprofile={item.get('profile', '-')}"
                    f"\tstrategy={item.get('strategy', '-')}\tcurrent={item.get('current', '-')}"
                    f"\tbest={item.get('best', '-')}\tstable={item.get('stable', '-')}"
                    f"\tswitched={item.get('switched', '-')}\treason={item.get('skip_reason') or item.get('reason', '-')}"
                )
        else:
            history_lines.append("无探测历史")
    except Exception as exc:
        history_lines.append(f"历史读取失败: {exc}")
    sections.append(IncidentSection("recent-probe-history", history_lines))

    return sections
