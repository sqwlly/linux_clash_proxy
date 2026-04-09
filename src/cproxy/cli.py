from __future__ import annotations

import os
import sys
from argparse import Namespace
from pathlib import Path

from . import __version__
from .api import APIUnavailableError
from .backend.models import AIProbeReport, ProxyGroup
from .config import default_paths
from .diagnostics import ConnectivityReport, GroupCheckReport, run_ai_probe, run_connectivity_test, test_group
from .geodata import check_country_mmdb
from .install import auto_migrate_from_default_legacy, init_user_layout, is_placeholder_config, migrate_from_legacy
from .logs import follow_lines, read_recent_lines
from .proxyenv import proxy_env_lines, run_proxy_shell, run_with_proxy
from .process import ProcessOwnershipError, get_status, restart_process, start_process, stop_process
from .config import log_file
from .runtime import render_runtime
from .output import build_root_parser, normalize_name
from .services.query import QueryService

ANSI_RESET = "\033[0m"
ANSI_BOLD = "\033[1m"
ANSI_BLUE = "\033[34m"
ANSI_GREEN = "\033[32m"
ANSI_YELLOW = "\033[33m"
ANSI_RED = "\033[31m"
ANSI_CYAN = "\033[36m"


def _get_group(groups: dict, name: str):
    group = groups.get(name)
    if not group:
        raise SystemExit(f"错误: 未找到代理组: {name}")
    return group


def _group_value(group, key: str, default=None):
    if isinstance(group, ProxyGroup):
        mapping = {
            "type": group.type,
            "now": group.current,
            "all": group.candidates,
            "alive": group.alive,
            "delay": group.delay,
        }
        return mapping.get(key, default)
    return group.get(key, default)


def _format_delay_label(delay) -> str:
    return f"{delay}ms" if isinstance(delay, int) else "-"


def _color_enabled() -> bool:
    return sys.stdout.isatty() or os.environ.get("FORCE_COLOR") == "1"


def _style(text: object, *codes: str) -> str:
    content = str(text)
    if not _color_enabled() or not codes:
        return content
    return f"{''.join(codes)}{content}{ANSI_RESET}"


def _section_title(title: str) -> str:
    return _style(title, ANSI_BOLD, ANSI_BLUE)


def _accent(text: object) -> str:
    return _style(text, ANSI_CYAN)


def _status_color(text: str) -> str:
    if text in {"正常", "运行中", "可访问", "已就绪"}:
        return _style(text, ANSI_GREEN)
    if text in {"部分异常", "待刷新", "未知"}:
        return _style(text, ANSI_YELLOW)
    if text in {"失败", "异常", "未运行", "不可访问"}:
        return _style(text, ANSI_RED)
    return text


def _print_section(title: str) -> None:
    print(_section_title(title))


def _resolve_ai_route(groups: dict) -> dict[str, object]:
    manual_target = _group_value(_get_group(groups, "AI-MANUAL"), "now", "-")
    auto_target = _group_value(_get_group(groups, "AI-AUTO"), "now", "-")
    auto_mode = manual_target == "AI-AUTO"
    active_group = auto_target if auto_mode else manual_target
    active = _get_group(groups, active_group)
    active_node = _group_value(active, "now", "-")
    active_delay = _group_value(active, "delay", "-")
    active_alive = _group_value(active, "alive")
    standby_group = "AI-SG" if active_group == "AI-US" else "AI-US"
    standby = _get_group(groups, standby_group)
    standby_node = _group_value(standby, "now", "-")
    standby_delay = _group_value(standby, "delay", "-")
    standby_alive = _group_value(standby, "alive")

    return {
        "auto_mode": auto_mode,
        "mode_label": "自动切换" if auto_mode else f"固定 {manual_target}",
        "active_group": active_group,
        "active_node": active_node,
        "active_delay": active_delay,
        "active_alive": active_alive,
        "standby_group": standby_group,
        "standby_node": standby_node,
        "standby_delay": standby_delay,
        "standby_alive": standby_alive,
    }


def _render_current(groups: dict, group_name: str, raw: bool) -> int:
    current = _group_value(_get_group(groups, group_name), "now")
    if not current:
        raise SystemExit(f"错误: 代理组 [{group_name}] 当前无可读的 now 状态")
    if raw:
        print(current)
    else:
        _print_section("摘要")
        print(f"当前选择: {_accent(normalize_name(current))}")
    return 0


def _render_list_groups(groups, raw: bool) -> int:
    items = []
    iterable = groups.values() if isinstance(groups, dict) else groups
    for group in iterable:
        name = group.name if isinstance(group, ProxyGroup) else str(group.get("name", ""))
        group_type = str(_group_value(group, "type", "")).lower()
        if group_type in {"selector", "select", "fallback", "url-test", "load-balance"}:
            normalized_type = "select" if group_type == "selector" else group_type
            items.append((name, normalized_type, normalize_name(_group_value(group, "now", "-"))))

    if raw:
        for name, group_type, _ in items:
            print(f"{name}\t{group_type}")
        return 0

    _print_section("摘要")
    print(f"总组数: {len(items)}")
    print(f"可切换组数: {len(items)}")
    print()
    _print_section("列表")
    print(f"{'组名':<20} {'类型':<12} 当前选择")
    for name, group_type, current in items:
        print(f"{name:<20} {group_type:<12} {current}")
    return 0


def _render_list_nodes(groups: dict, group_name: str, raw: bool) -> int:
    group = _get_group(groups, group_name)
    current = _group_value(group, "now", "")
    items = _group_value(group, "all", [])

    if raw:
        for item in items:
            prefix = "* " if item == current else "  "
            print(f"{prefix}{item}")
        return 0

    _print_section("摘要")
    print(f"目标组: {group_name}")
    print(f"当前选择: {_accent(normalize_name(current))}")
    print(f"候选数: {len(items)}")
    print()
    _print_section("列表")
    for item in items:
        label = "当前" if item == current else "候选"
        print(f"{label}  {normalize_name(item)}")
    return 0


def _render_ai_status(groups: dict, raw: bool) -> int:
    names = (
        "AI-MANUAL",
        "AI-AUTO",
        "AI-US",
        "AI-SG",
        "🇺🇸 United States",
        "🇸🇬 Singapore",
    )
    probe_report = run_ai_probe(default_paths())
    if raw:
        for name in names:
            group = groups.get(name)
            if not group:
                print(f"{name}: 缺失")
                continue
            delay = _group_value(group, "delay", "-")
            print(
                f"{name}: type={_group_value(group, 'type', '-')} now={_group_value(group, 'now', '-')} "
                f"alive={_group_value(group, 'alive', '-')} last_delay={delay}"
            )
        print(f"AI-PROBE: {_probe_summary_status(probe_report)}")
        for item in probe_report.results:
            print(f"AI-PROBE-ITEM: name={item.name} ok={item.ok} detail={item.detail} url={item.url}")
        return 0

    route = _resolve_ai_route(groups)
    active_group = str(route["active_group"])
    active_node = route["active_node"]
    active_delay = route["active_delay"]
    active_alive = route["active_alive"]
    standby_group = str(route["standby_group"])
    standby_node = route["standby_node"]
    standby_delay = route["standby_delay"]
    standby_alive = route["standby_alive"]
    standby_status = "正常" if standby_alive is True else "异常" if standby_alive is False else "未知"
    active_status = "正常" if active_alive is True else "异常" if active_alive is False else "未知"

    _print_section("摘要")
    print(
        f"AI 路由: {route['mode_label']}  当前出口={normalize_name(active_node)}  "
        f"区域={active_group}  延迟={_format_delay_label(active_delay)}  状态={active_status}"
    )
    print(f"AI 探测: {_status_color(_probe_summary_status(probe_report))}")
    print()
    _print_section("连通性")
    for item in probe_report.results:
        label = _status_color("正常" if item.ok else "失败")
        print(f"{label}  {item.name}  {item.url}")
    print()
    _print_section("链路")
    print("AI-MANUAL")
    if bool(route["auto_mode"]):
        print("└─ AI-AUTO")
        print(f"   └─ {active_group}")
        print(f"      └─ {normalize_name(active_node)} ({_format_delay_label(active_delay)})")
    else:
        print(f"└─ {active_group}")
        print(f"   └─ {normalize_name(active_node)} ({_format_delay_label(active_delay)})")
    print()
    _print_section("备用")
    print(f"{standby_group} -> {normalize_name(standby_node)} ({_format_delay_label(standby_delay)}, {_status_color(standby_status)})")
    print()
    _print_section("分组")
    for name in ("AI-MANUAL", "AI-AUTO", "AI-US", "AI-SG"):
        group = _get_group(groups, name)
        print(f"{name:<10} {_group_value(group, 'type', '-'):<8} 当前: {normalize_name(_group_value(group, 'now', '-'))}")
    return 0


def _probe_summary_status(report: AIProbeReport) -> str:
    ok_count = sum(1 for item in report.results if item.ok)
    if ok_count == len(report.results):
        return "正常"
    if ok_count == 0:
        return "失败"
    return "部分异常"


def _render_status(raw: bool) -> int:
    snapshot = get_status(default_paths())
    config_state = "已就绪" if snapshot.runtime_ready else "待刷新"
    status_text = "运行中" if snapshot.running else "未运行"
    api_text = "不可访问"
    ai_mode = "-"
    ai_summary = "-"

    try:
        route = _resolve_ai_route(QueryService(default_paths()).get_ai_status_groups())
        api_text = "可访问"
        ai_mode = str(route["mode_label"])
        ai_summary = f"{route['active_group']} -> {normalize_name(route['active_node'])}"
        if isinstance(route["active_delay"], int):
            ai_summary = f"{ai_summary} ({route['active_delay']}ms)"
    except APIUnavailableError:
        pass

    if raw:
        print("版本: 0.1.0")
        print(f"原始配置: {snapshot.source_config}")
        print(f"运行配置: {snapshot.runtime_config}")
        print(f"控制接口: {snapshot.controller}")
        print(f"代理端口: {snapshot.port}")
        print(f"运行配置状态: {config_state}")
        print(f"状态: {status_text}")
        if snapshot.pid:
            print(f"PID: {snapshot.pid}")
        return 0

    _print_section("摘要")
    print(f"状态: {_status_color(status_text)}")
    print(f"API: {_status_color(api_text)}")
    print(f"AI 路由模式: {ai_mode}")
    print(f"AI 当前出口: {_accent(ai_summary)}")
    print(f"运行配置状态: {_status_color(config_state)}")
    print()
    _print_section("资源")
    print(f"代理端口: {snapshot.port}")
    print(f"控制接口: {snapshot.controller}")
    print()
    _print_section("路径")
    print(f"原始配置: {snapshot.source_config}")
    print(f"运行配置: {snapshot.runtime_config}")
    if snapshot.pid:
        print(f"PID: {snapshot.pid}")
    return 0


def _render_group_check(report: GroupCheckReport, raw: bool) -> int:
    if raw:
        for item in report.results:
            print(f"{item.name}: {item.delay}ms" if item.ok and item.delay is not None else f"{item.name}: 失败")
        return 0 if all(item.ok for item in report.results) else 1

    ok_items = [item for item in report.results if item.ok and item.delay is not None]
    best = min(ok_items, key=lambda item: item.delay) if ok_items else None
    worst = max(ok_items, key=lambda item: item.delay) if ok_items else None

    _print_section("摘要")
    print(f"目标组: {report.group_name}")
    print(f"可用: {len(ok_items)}/{len(report.results)}")
    print(f"最佳: {best.name} ({best.delay}ms)" if best else "最佳: -")
    print(f"最慢: {worst.name} ({worst.delay}ms)" if worst else "最慢: -")
    print()
    _print_section("结果")
    for item in report.results:
        if item.ok and item.delay is not None:
            print(f"{_status_color('正常')}  {item.name}  {item.delay}ms")
        else:
            print(f"{_status_color('失败')}  {item.name}  -")
    return 0 if len(ok_items) == len(report.results) else 1


def _render_connectivity_report(report: ConnectivityReport) -> int:
    passed = sum(1 for item in report.results if item.ok)
    _print_section("摘要")
    print("目标: 代理连通性")
    print(f"可用: {passed}/{len(report.results)}")
    print(f"出口 IP: {report.exit_ip or '-'}")
    print()
    _print_section("结果")
    for item in report.results:
        if item.ok:
            print(f"{_status_color('正常')}  {item.name}  {item.detail}")
        else:
            print(f"{_status_color('失败')}  {item.name}  {item.detail}")
    return 0 if passed == len(report.results) else 1


def _render_logs(lines: int, follow: bool) -> int:
    path = log_file(default_paths())
    if not path.exists():
        raise SystemExit(f"错误: 日志文件不存在: {path}")

    _print_section("日志")
    print(f"日志文件: {path}")
    print()
    for line in read_recent_lines(path, lines):
        print(line)

    if not follow:
        return 0

    try:
        for line in follow_lines(path):
            print(line)
    except KeyboardInterrupt:
        print()
        print("日志查看已停止")
    return 0


def _run_bootstrap() -> int:
    paths = default_paths()
    config_path = init_user_layout(paths)
    migrated_from: Path | None = None

    if is_placeholder_config(paths):
        migrated_path = auto_migrate_from_default_legacy(paths)
        if migrated_path is None:
            legacy_root = Path(os.environ.get("CPROXY_LEGACY_ROOT", "/root/clash_proxy"))
            legacy_config = legacy_root / "config.yaml"
            raise RuntimeError(f"错误: 当前配置为空，且未找到可迁移配置: {legacy_config}")
        migrated_from = Path(os.environ.get("CPROXY_LEGACY_ROOT", "/root/clash_proxy")) / "config.yaml"
        config_path = migrated_path

    runtime_path = render_runtime(paths)
    pid = start_process(paths)
    snapshot = get_status(paths)
    if not snapshot.running:
        raise RuntimeError("错误: 代理启动后状态异常，请执行 cproxy logs --lines 100 排查")

    geodata_check = check_country_mmdb(paths)

    print("一键部署完成")
    print(f"配置文件: {config_path}")
    if migrated_from is not None:
        print(f"已自动迁移旧配置: {migrated_from}")
    print(f"运行配置: {runtime_path}")
    print(f"代理进程: 运行中 (PID: {pid})")
    if geodata_check.ok:
        print(f"GeoIP: {geodata_check.detail}")
    else:
        print(f"GeoIP: {geodata_check.detail}")
    return 0


def run(argv: list[str] | None = None) -> int:
    parser = build_root_parser()
    args: Namespace = parser.parse_args(argv)
    try:
        if args.version:
            print(__version__)
            return 0
        if args.command == "init":
            config_file = init_user_layout(default_paths())
            print(f"已初始化配置: {config_file}")
            return 0
        if args.command == "bootstrap":
            return _run_bootstrap()
        if args.command == "migrate-from-legacy":
            config_file = migrate_from_legacy(default_paths(), Path(args.legacy_root))
            print(f"已迁移配置: {config_file}")
            return 0
        if args.command == "render":
            runtime_path = render_runtime(default_paths())
            print(f"已生成运行配置: {runtime_path}")
            return 0
        if args.command == "start":
            pid = start_process(default_paths())
            print(f"代理已启动 (PID: {pid})")
            return 0
        if args.command == "stop":
            stopped = stop_process(default_paths())
            if stopped:
                print("代理已停止")
            else:
                print("代理未运行")
            return 0
        if args.command == "restart":
            pid = restart_process(default_paths())
            print(f"代理已启动 (PID: {pid})")
            return 0
        if args.command == "logs":
            return _render_logs(args.lines, args.follow)
        if args.command == "status":
            return _render_status(args.raw)
        if args.command == "test":
            return _render_connectivity_report(run_connectivity_test(default_paths()))
        if args.command == "test-group":
            return _render_group_check(test_group(default_paths(), args.group), args.raw)
        if args.command == "proxy-env":
            for line in proxy_env_lines(default_paths()):
                print(line)
            return 0
        if args.command == "with-proxy":
            return run_with_proxy(default_paths(), args.command_args)
        if args.command == "proxy-shell":
            print(_section_title("进入临时代理 shell，退出后代理环境失效"))
            return run_proxy_shell(default_paths(), args.shell_args)
        if args.command in {"current", "list-groups", "list-nodes", "ai-status"}:
            service = QueryService(default_paths())
            if args.command == "current":
                return _render_current({args.group: service.get_group(args.group)}, args.group, args.raw)
            if args.command == "list-groups":
                return _render_list_groups(service.list_groups(), args.raw)
            if args.command == "list-nodes":
                return _render_list_nodes({args.group: service.get_group(args.group)}, args.group, args.raw)
            return _render_ai_status(service.get_ai_status_groups(), args.raw)
        if args.command == "switch":
            service = QueryService(default_paths())
            group = service.switch_group(args.group, args.target)
            print(_section_title("结果"))
            print(f"代理组: {args.group}")
            print(f"当前选择: {_accent(normalize_name(group.current))}")
            return 0
        return 0
    except (APIUnavailableError, ProcessOwnershipError, FileNotFoundError, RuntimeError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


def main() -> None:
    raise SystemExit(run(sys.argv[1:]))


if __name__ == "__main__":
    main()
