from __future__ import annotations

from argparse import ArgumentParser, REMAINDER
from collections.abc import Callable

from .services.probe import ProbeReport, format_delay, stable_score


def normalize_name(value: object) -> str:
    if value in ("-", None):
        return "-"

    text = str(value).strip()
    parts = text.split(maxsplit=1)
    if len(parts) == 2 and parts[0] and all(not ch.isalnum() for ch in parts[0]):
        text = parts[1].strip()

    text = text.replace("丨", " ")
    text = text.replace("|", " ")
    return " ".join(text.split())


def build_probe_output(
    report: ProbeReport,
    raw: bool,
    section_title: Callable[[str], str] = str,
) -> tuple[list[str], int]:
    if raw:
        lines = [
            f"GROUP\t{report.group}",
            f"PROFILE\t{report.profile}",
            f"STRATEGY\t{report.strategy_name}",
            f"ROUNDS\t{report.rounds}",
            f"URL\t{report.url}",
            f"CURRENT\t{report.current or '-'}",
            f"CURRENT_STABLE\t{'true' if report.current_verdict.stable else 'false'}\t{report.current_verdict.reason}",
            f"STABLE\t{'true' if report.verdict.stable else 'false'}\t{report.verdict.reason}",
        ]
        if report.best:
            score = stable_score(report.best, report.rounds, report.strategy)
            lines.append(
                f"BEST\t{report.best.name}\tsuccess={report.best.success_count}/{report.rounds}"
                f"\tfailures={report.best.failures}\tavg={format_delay(report.best.avg_delay)}"
                f"\tmax={format_delay(report.best.max_delay)}\tscore={score}"
            )
        if report.switched:
            lines.append(f"SWITCH\t{report.group}\t{report.best.name if report.best else '-'}")
        if report.switch_requested and not report.switched:
            lines.append(f"SKIP_SWITCH\t{report.group}\t{report.skip_reason}")
        for summary in sorted(report.summaries, key=lambda item: item.rank_key()):
            score = stable_score(summary, report.rounds, report.strategy)
            lines.append(
                f"NODE\t{summary.name}\tsuccess={summary.success_count}/{report.rounds}"
                f"\tfailures={summary.failures}\tavg={format_delay(summary.avg_delay)}"
                f"\tmax={format_delay(summary.max_delay)}\tmin={format_delay(summary.min_delay)}\tscore={score}"
            )
    else:
        current_label = normalize_name(report.current) if report.current else "-"
        current_stable = "稳定" if report.current_verdict.stable else "不稳定"
        best_label = normalize_name(report.best.name) if report.best else "-"
        best_stable = "稳定" if report.verdict.stable else "不稳定"
        lines = [
            section_title("摘要"),
            f"目标组: {report.group}",
            f"配置: {report.profile} / {report.strategy_name}",
            f"探测: {report.rounds} 轮, {report.url}",
            f"当前: {current_label} ({current_stable})",
            f"推荐: {best_label} ({best_stable})",
        ]
        if report.best and report.switched:
            lines.append(f"切换: 已切换 {report.group} -> {best_label}")
        elif report.switch_requested:
            lines.append(f"切换: 未切换 ({report.skip_reason})")
        elif report.preview_reason:
            lines.append(f"切换预览: 不会切换 ({report.preview_reason})")
        elif report.best:
            lines.append(f"切换预览: 会切换到 {best_label}")
        lines.extend(["", section_title("结果"), *_probe_table_lines(report)])

    if report.switch_requested:
        request_satisfied = (
            report.switched
            or report.skip_reason == "当前已是推荐稳定节点"
            or (report.current_verdict.stable and report.skip_reason.startswith("当前节点也稳定"))
        )
        if not request_satisfied:
            return lines, 1
    return lines, 0 if report.best else 1


def _probe_table_lines(report: ProbeReport) -> list[str]:
    header = f"{'节点':<16} {'成功':>4}  {'失败':>4}  {'平均':>6}  {'最大':>6}  {'最小':>6}  {'score':>5}"
    lines = [header]
    for summary in sorted(report.summaries, key=lambda item: item.rank_key()):
        score = stable_score(summary, report.rounds, report.strategy)
        lines.append(
            f"{normalize_name(summary.name):<16} {summary.success_count}/{report.rounds:>3}  "
            f"{summary.failures:>4}  {format_delay(summary.avg_delay):>6}  "
            f"{format_delay(summary.max_delay):>6}  {format_delay(summary.min_delay):>6}  {score:>5}"
        )
    return lines


def build_root_parser() -> ArgumentParser:
    parser = ArgumentParser(prog="cproxy", description="User-level Mihomo proxy CLI")
    parser.add_argument("--version", action="store_true", help="Show version and exit")
    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser("init", help="Initialize user config directories")
    subparsers.add_parser("bootstrap", help="One-click bootstrap without arguments")

    current_parser = subparsers.add_parser("current", help="Show current proxy selection")
    current_parser.add_argument("group")
    current_parser.add_argument("--raw", action="store_true")

    groups_parser = subparsers.add_parser("list-groups", help="List switchable proxy groups")
    groups_parser.add_argument("--raw", action="store_true")

    nodes_parser = subparsers.add_parser("list-nodes", help="List group candidates")
    nodes_parser.add_argument("group")
    nodes_parser.add_argument("--raw", action="store_true")

    switch_parser = subparsers.add_parser("switch", help="Switch selector group target")
    switch_parser.add_argument("group")
    switch_parser.add_argument("target")

    ai_status_parser = subparsers.add_parser("ai-status", help="Show AI routing status")
    ai_status_parser.add_argument("--raw", action="store_true")

    migrate_parser = subparsers.add_parser("migrate-from-legacy", help="Import config from legacy repo")
    migrate_parser.add_argument("legacy_root")

    subparsers.add_parser("render", help="Render runtime config")
    snapshots_parser = subparsers.add_parser("snapshots", help="List runtime/config snapshots")
    snapshots_parser.add_argument("--raw", action="store_true")

    rollback_parser = subparsers.add_parser("rollback", help="Restore a previous runtime/config snapshot")
    rollback_parser.add_argument("name", nargs="?", help="Snapshot filename (default: latest runtime snapshot)")

    refresh_parser = subparsers.add_parser("refresh", help="Update subscription, render, restart and probe groups")
    refresh_parser.add_argument("--subscription-url", help="Subscription URL (overrides config subscription-url)")
    refresh_parser.add_argument("--group", action="append", default=[], help="Group to probe and auto-switch (repeatable)")
    refresh_parser.add_argument("--raw", action="store_true")

    status_parser = subparsers.add_parser("status", help="Show current status")
    status_parser.add_argument("--raw", action="store_true")
    subparsers.add_parser("start", help="Start proxy process")
    subparsers.add_parser("stop", help="Stop proxy process")
    subparsers.add_parser("restart", help="Restart proxy process")
    logs_parser = subparsers.add_parser("logs", help="Show cproxy log output")
    logs_parser.add_argument("--lines", type=int, default=50)
    logs_parser.add_argument("--follow", action="store_true")

    subparsers.add_parser("test", help="Test proxy connectivity")

    security_parser = subparsers.add_parser("security-check", help="Validate local GA security configuration")
    security_parser.add_argument("--strict", action="store_true", help="Treat warnings as failures")

    support_parser = subparsers.add_parser("support-bundle", help="Write a redacted support bundle")
    support_parser.add_argument("--output", help="Output tar.gz path")

    test_group_parser = subparsers.add_parser("test-group", help="Test group or node health")
    test_group_parser.add_argument("group")
    test_group_parser.add_argument("--raw", action="store_true")

    subparsers.add_parser("proxy-env", help="Print proxy environment variables")

    with_proxy_parser = subparsers.add_parser("with-proxy", help="Run one command with proxy env")
    with_proxy_parser.add_argument("command_args", nargs=REMAINDER)

    proxy_shell_parser = subparsers.add_parser("proxy-shell", help="Open a temporary proxy shell")
    proxy_shell_parser.add_argument("shell_args", nargs=REMAINDER)

    probe_parser = subparsers.add_parser("probe-stable-node", help="Multi-round delay probe to find the most stable node")
    probe_parser.add_argument("group", nargs="?", default="AI-MANUAL")
    probe_parser.add_argument("--profile", choices=["codex", "chatgpt", "github", "claude"], default="codex")
    probe_parser.add_argument("--strategy", choices=["conservative", "balanced", "aggressive"])
    probe_parser.add_argument("--url")
    probe_parser.add_argument("--rounds", type=int)
    probe_parser.add_argument("--timeout", type=int, default=8000)
    probe_parser.add_argument("--switch", action="store_true")
    probe_parser.add_argument("--raw", action="store_true")

    subparsers.add_parser("tui", help="Launch terminal UI dashboard")
    return parser
