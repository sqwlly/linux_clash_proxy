from __future__ import annotations

import os
import subprocess
import sys
import time
from collections import Counter, defaultdict
from dataclasses import dataclass

from ..backend.api import APIBackend
from ..backend.process import ProcessBackend
from ..config import AppPaths, read_config
from ..proxyenv import proxy_http_url
from .probe import ProbeService
from .probe_history import load_history_rows, probe_history_file

AI_HOST_KEYWORDS = ("chatgpt", "openai", "claude", "anthropic", "github")


@dataclass(frozen=True)
class AIConnection:
    host: str
    count: int
    route: str


@dataclass(frozen=True)
class IncidentSection:
    title: str
    lines: list[str]


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
    report = ProbeService(paths).probe(profile=profile, switch=True)
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
    config = read_config(paths)
    process = ProcessBackend(paths)
    status = process.status()

    header_lines = [
        f"time={time.strftime('%Y-%m-%dT%H:%M:%S%z')}",
        f"profile={profile}",
    ]
    sections.append(IncidentSection("事件报告", header_lines))

    service_lines: list[str] = []
    try:
        result = subprocess.run(
            ["systemctl", "show", "clash-proxy.service",
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
    sections.append(IncidentSection("service", service_lines))

    ai_lines: list[str] = []
    try:
        api = APIBackend(paths)
        groups = api.get_groups()
        for name in ("AI-MANUAL", "AI-AUTO", "AI-US", "AI-SG"):
            group = groups.get(name)
            if group:
                ai_lines.append(f"{name}: type={group.type} now={group.current} alive={group.alive} delay={group.delay}")
            else:
                ai_lines.append(f"{name}: 缺失")
    except Exception as exc:
        ai_lines.append(f"API 不可访问: {exc}")
    sections.append(IncidentSection("ai-status", ai_lines))

    probe_lines: list[str] = []
    try:
        report = ProbeService(paths).probe(profile=profile)
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
