from __future__ import annotations

import os
import sys
from argparse import Namespace
from functools import lru_cache
from pathlib import Path

from . import __version__
from .api import APIUnavailableError
from .backend.models import AIProbeReport, ProxyGroup
from .config import default_paths, log_file, read_config
from .diagnostics import ConnectivityReport, GroupCheckReport, run_ai_probe, run_connectivity_test, test_group
from .geodata import check_country_mmdb
from .install import auto_migrate_from_default_legacy, init_user_layout, is_placeholder_config, migrate_from_legacy
from .logs import follow_lines, read_recent_lines
from .proxyenv import proxy_env_lines, run_proxy_shell, run_with_proxy
from .process import ProcessOwnershipError, get_status, restart_process, start_process, stop_process
from .runtime import render_runtime
from .security import validate_controller_security
from .snapshots import list_snapshots, restore_snapshot, snapshot_kind, snapshots_dir
from .support import build_support_bundle
from .output import build_probe_output, build_root_parser, normalize_name
from .services.query import QueryService
from .services.refresh import RefreshReport, RefreshService
from .services.probe import ProbeService
from .services.probe_history import load_history_rows, probe_history_file
from .services.ops import AIConnection, build_incident, get_ai_connections, guard

ANSI_RESET = "\033[0m"
ANSI_BOLD = "\033[1m"
ANSI_BLUE = "\033[34m"
ANSI_GREEN = "\033[32m"
ANSI_YELLOW = "\033[33m"
ANSI_RED = "\033[31m"
ANSI_CYAN = "\033[36m"

POSITIVE_STATUSES = {"正常", "运行中", "可访问", "已就绪"}
WARNING_STATUSES = {"部分异常", "待刷新", "未知"}
NEGATIVE_STATUSES = {"失败", "异常", "未运行", "不可访问"}
STATUS_ICONS = {
    "正常": "✓",
    "运行中": "●",
    "可访问": "✓",
    "已就绪": "✓",
    "部分异常": "!",
    "待刷新": "!",
    "未知": "?",
    "失败": "✗",
    "异常": "✗",
    "未运行": "○",
    "不可访问": "✗",
}


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


@lru_cache(maxsize=1)
def _output_config() -> dict:
    try:
        return read_config(default_paths())
    except Exception:
        return {}


def _truthy(value: object) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _falsey(value: object) -> bool:
    return str(value).strip().lower() in {"0", "false", "no", "off"}


def _color_mode() -> str:
    env_mode = os.environ.get("CPROXY_COLOR")
    if env_mode:
        normalized = env_mode.strip().lower()
        if normalized in {"auto", "always", "never"}:
            return normalized

    if os.environ.get("FORCE_COLOR") == "1":
        return "always"
    if os.environ.get("NO_COLOR"):
        return "never"

    config = _output_config()
    if "output-color" in config:
        config_mode = str(config.get("output-color")).strip().lower()
        if config_mode in {"auto", "always", "never"}:
            return config_mode

    # 如果配置中未显式声明 output-color，在 CLI 侧保持更保守行为：
    # 无图标输出时按 auto（TTY 自动），有图标时默认恢复为 always，兼容之前的开箱即用体验。
    if _icons_enabled():
        return "always"

    return "auto"


def _color_enabled() -> bool:
    mode = _color_mode()
    if mode == "always":
        return True
    if mode == "never":
        return False
    return sys.stdout.isatty()


def _icons_enabled() -> bool:
    env_icons = os.environ.get("CPROXY_ICONS")
    if env_icons:
        if _truthy(env_icons):
            return True
        if _falsey(env_icons):
            return False

    config = _output_config()
    if "output-icons" not in config and os.environ.get("NO_COLOR"):
        return False

    config_icons = config.get("output-icons", False)
    if isinstance(config_icons, bool):
        return config_icons
    if _truthy(config_icons):
        return True
    if _falsey(config_icons):
        return False
    return False


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
    if text in POSITIVE_STATUSES:
        return _style(text, ANSI_GREEN)
    if text in WARNING_STATUSES:
        return _style(text, ANSI_YELLOW)
    if text in NEGATIVE_STATUSES:
        return _style(text, ANSI_RED)
    return text


def _status_label(text: str) -> str:
    label = _status_color(text)
    if not _icons_enabled():
        return label

    icon = STATUS_ICONS.get(text)
    if not icon:
        return label
    return f"{icon} {label}"


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
        f"区域={active_group}  延迟={_format_delay_label(active_delay)}  状态={_status_label(active_status)}"
    )
    print(f"AI 探测: {_status_label(_probe_summary_status(probe_report))}")
    print()
    _print_section("连通性")
    for item in probe_report.results:
        label = _status_label("正常" if item.ok else "失败")
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
    print(f"{standby_group} -> {normalize_name(standby_node)} ({_format_delay_label(standby_delay)}, {_status_label(standby_status)})")
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
        print(f"版本: {__version__}")
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
    print(f"状态: {_status_label(status_text)}")
    print(f"API: {_status_label(api_text)}")
    print(f"AI 路由模式: {ai_mode}")
    print(f"AI 当前出口: {_accent(ai_summary)}")
    print(f"运行配置状态: {_status_label(config_state)}")
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
    if not snapshot.running and api_text == "可访问":
        print()
        _print_section("提示")
        print("当前用户级 cproxy 未运行，API 可能来自其它 Mihomo 实例。")
        print("生产入口状态请优先查看 clash-proxy status 或在仓库根目录运行 ./proxy.sh status。")
    if not snapshot.runtime_ready:
        print("如需使用用户级 cproxy，请先运行 cproxy render 生成运行配置。")
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
            print(f"{_status_label('正常')}  {item.name}  {item.delay}ms")
        else:
            print(f"{_status_label('失败')}  {item.name}  -")
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
            print(f"{_status_label('正常')}  {item.name}  {item.detail}")
        else:
            print(f"{_status_label('失败')}  {item.name}  {item.detail}")
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


def _render_security_check(strict: bool) -> int:
    report = validate_controller_security(default_paths())
    if report.issues:
        for issue in report.issues:
            print(f"{issue.severity.upper()}: {issue.code}: {issue.detail}")
    else:
        print("OK: security configuration passed")
    if any(issue.severity == "error" or (strict and issue.severity == "warning") for issue in report.issues):
        return 1
    return 0


def _render_snapshots(paths, raw: bool) -> int:
    entries = list_snapshots(paths)
    if raw:
        for entry in entries:
            print(entry.name)
        return 0

    _print_section("摘要")
    print(f"快照数: {len(entries)}")
    print(f"快照目录: {snapshots_dir(paths)}")
    if entries:
        print()
        _print_section("列表")
        for entry in entries:
            size = entry.stat().st_size
            print(f"{snapshot_kind(entry):<8} {entry.name}  {size}B")
    return 0


def _run_rollback(paths, name: str | None) -> int:
    if name:
        candidate = snapshots_dir(paths) / Path(name).name
        if not candidate.is_file():
            raise RuntimeError(f"错误: 快照不存在: {name}")
        snapshot = candidate
    else:
        runtime_snapshots = list_snapshots(paths, "runtime")
        if not runtime_snapshots:
            raise RuntimeError("错误: 没有可用的运行配置快照")
        snapshot = runtime_snapshots[0]

    target = restore_snapshot(paths, snapshot)
    kind = snapshot_kind(snapshot)
    print(_section_title("结果"))
    print(f"已恢复快照: {snapshot.name}")
    print(f"目标文件: {target}")
    if kind == "runtime":
        if get_status(paths).running:
            restart_process(paths)
            print("代理已重启以应用回滚")
        else:
            print("提示: 代理未运行，配置将在下次启动时生效")
    else:
        print("提示: 原始配置已恢复，执行 cproxy render 使其生效")
    return 0


def _render_refresh(report: RefreshReport, raw: bool) -> int:
    if raw:
        print(f"subscription={report.subscription} detail={report.subscription_detail}")
        print(f"runtime={report.runtime_path}")
        print(f"restarted={report.restarted}")
        for item in report.groups:
            print(f"{item.group}: {item.action} current={item.current or '-'} target={item.target or '-'} {item.detail}")
        return 0

    _print_section("摘要")
    subscription_label = report.subscription
    if report.subscription_detail:
        subscription_label = f"{subscription_label}  {report.subscription_detail}"
    print(f"订阅更新: {subscription_label}")
    print(f"运行配置: {report.runtime_path}")
    if report.restarted:
        print("代理: 已重启应用新配置")
    elif report.was_running:
        print("代理: 运行中")
    else:
        print("代理: 未运行（跳过重启与探测）")
    if report.groups:
        print()
        _print_section("分组探测")
        for item in report.groups:
            line = f"{item.group}: {item.action}"
            if item.target:
                line += f" -> {normalize_name(item.target)}"
            if item.detail:
                line += f"  {item.detail}"
            print(line)
    return 0


def _render_shadow_history(paths, limit: int, raw: bool) -> int:
    rows = load_history_rows(probe_history_file(paths), limit)
    if raw:
        for item in rows:
            print(
                "PROBE_HISTORY\t"
                f"ts={item.get('ts', '-')}\tprofile={item.get('profile', '-')}"
                f"\tstrategy={item.get('strategy', '-')}\tcurrent={item.get('current', '-')}"
                f"\tbest={item.get('best', '-')}\tstable={item.get('stable', '-')}"
                f"\tswitched={item.get('switched', '-')}\treason={item.get('skip_reason') or item.get('reason', '-')}"
            )
        return 0

    _print_section("稳定探测历史")
    if not rows:
        print("-")
        return 0
    for item in rows:
        print(
            f"{item.get('ts', '-')}  "
            f"{item.get('profile', '-')}  "
            f"{item.get('strategy', '-')}  "
            f"current={item.get('current', '-')}  "
            f"best={item.get('best', '-')}  "
            f"stable={item.get('stable', '-')}  "
            f"switched={item.get('switched', '-')}  "
            f"reason={item.get('skip_reason') or item.get('reason', '-')}"
        )
    return 0


def _render_ai_connections(paths, raw: bool) -> int:
    connections = get_ai_connections(paths)
    if raw:
        for conn in connections:
            print(f"AI_CONNECTION\t{conn.host}\tcount={conn.count}\troute={conn.route}")
        return 0

    _print_section("AI 连接")
    if not connections:
        print("未发现 ChatGPT/OpenAI/Claude/GitHub 相关活动连接")
        return 0
    for conn in connections:
        print(f"{conn.host}  {conn.count} active  {conn.route}")
    return 0


def _render_incident(paths, profile: str) -> int:
    sections = build_incident(paths, profile)
    for section in sections:
        _print_section(section.title)
        for line in section.lines:
            print(line)
        print()
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
        if args.command == "snapshots":
            return _render_snapshots(default_paths(), args.raw)
        if args.command == "rollback":
            return _run_rollback(default_paths(), args.name)
        if args.command == "refresh":
            report = RefreshService(default_paths()).refresh(
                subscription_url=args.subscription_url,
                groups=args.group,
            )
            return _render_refresh(report, args.raw)
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
        if args.command == "security-check":
            return _render_security_check(args.strict)
        if args.command == "support-bundle":
            output_path = Path(args.output) if args.output else None
            bundle_path = build_support_bundle(default_paths(), output_path)
            print(f"已生成支持包: {bundle_path}")
            return 0
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
            print(f"当前选择: {normalize_name(group.current)}")
            return 0
        if args.command == "probe-stable-node":
            report = ProbeService(default_paths()).probe(
                group=args.group,
                profile=args.profile,
                strategy_name=args.strategy,
                url=args.url,
                rounds=args.rounds,
                timeout=args.timeout,
                switch=args.switch,
                record_history=args.record_history,
                show_progress=not args.raw,
            )
            output_lines, exit_code = build_probe_output(report, args.raw, _section_title)
            print("\n".join(output_lines))
            return exit_code
        if args.command == "shadow-probe":
            report = ProbeService(default_paths()).probe(
                group=args.group,
                profile=args.profile,
                strategy_name=args.strategy,
                url=args.url,
                rounds=args.rounds,
                timeout=args.timeout,
                switch=False,
                record_history=True,
                show_progress=not args.raw,
            )
            output_lines, exit_code = build_probe_output(report, args.raw, _section_title)
            print("\n".join(output_lines))
            return exit_code
        if args.command == "shadow-history":
            return _render_shadow_history(default_paths(), args.limit, args.raw)
        if args.command == "guard":
            command = args.command_args
            if command and command[0] == "--":
                command = command[1:]
            return guard(default_paths(), profile=args.profile, command=command or None)
        if args.command == "ai-connections":
            return _render_ai_connections(default_paths(), args.raw)
        if args.command == "incident":
            return _render_incident(default_paths(), args.profile)
        if args.command == "ai-use":
            report = ProbeService(default_paths()).probe(
                group=args.group,
                profile=args.profile,
                switch=True,
                show_progress=not args.raw,
            )
            output_lines, exit_code = build_probe_output(report, args.raw, _section_title)
            print("\n".join(output_lines))
            return exit_code
        if args.command == "tui":
            from .tui.app import run_tui
            run_tui(default_paths())
            return 0
        return 0
    except (APIUnavailableError, ProcessOwnershipError, FileNotFoundError, RuntimeError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


def main() -> None:
    raise SystemExit(run(sys.argv[1:]))


if __name__ == "__main__":
    main()
