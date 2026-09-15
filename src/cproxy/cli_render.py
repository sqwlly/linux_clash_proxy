from __future__ import annotations

import os
import re
import sys
import unicodedata
from collections.abc import Callable
from datetime import date, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any

from . import __version__
from .api import APIUnavailableError
from .backend.api import APIBackend
from .backend.models import AIProbeReport, ProxyGroup
from .backend.runtime_metrics import collect_runtime_metrics
from .config import default_paths, log_file, read_config
from .diagnostics import ConnectivityReport, GroupCheckReport, run_ai_probe
from .geodata import check_country_mmdb
from .install import auto_migrate_from_default_legacy, init_user_layout, is_placeholder_config
from .logs import follow_lines, read_recent_lines
from .output import STATUS_PROCESS_TOP_DEFAULT, normalize_name
from .process import get_status, restart_process, start_process
from .runtime import render_runtime
from .security import validate_controller_security
from .services.ipcheck import IpCheckService
from .services.ops import build_incident, get_ai_connections
from .services.probe_history import load_history_rows, probe_history_file
from .services.query import QueryService
from .services.subscription_info import SubscriptionUsage, display_entries
from .services.refresh import RefreshReport
from .services.traffic import ProcessTrafficReport, TrafficService, format_bytes
from .snapshots import list_snapshots, restore_snapshot, snapshot_kind, snapshots_dir

ANSI_RESET = "\033[0m"
ANSI_BOLD = "\033[1m"
ANSI_BLUE = "\033[34m"
ANSI_GREEN = "\033[32m"
ANSI_YELLOW = "\033[33m"
ANSI_RED = "\033[31m"
ANSI_CYAN = "\033[36m"
ANSI_MAGENTA = "\033[35m"

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


def _color_enabled(stream: object | None = None) -> bool:
    """是否上色。`stream=None` 时按 stdout 判定（历史行为）；传 stderr 则按 stderr 判。

    门控规则（CPROXY_COLOR / NO_COLOR / FORCE_COLOR / config 的 output-color）
    仍由 `_color_mode()` 单点决定，这里只负责挑 TTY。
    """
    mode = _color_mode()
    if mode == "always":
        return True
    if mode == "never":
        return False
    target = sys.stdout if stream is None else stream
    isatty = getattr(target, "isatty", None)
    return bool(isatty()) if callable(isatty) else False


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


def _style(text: object, *codes: str, stream: object | None = None) -> str:
    content = str(text)
    if not _color_enabled(stream) or not codes:
        return content
    return f"{''.join(codes)}{content}{ANSI_RESET}"


def print_error(message: object) -> None:
    """统一的 stderr 错误出口：整条着红，非 TTY / NO_COLOR 下逐字降级。

    整条包裹而非只染「错误: 」前缀是刻意的——测试断言
    `"错误: Mihomo API 不可访问" in stderr`，只染前缀会把 ANSI 复位码插进
    这个子串中间，反而打爆断言。
    """
    print(_style(message, ANSI_RED, stream=sys.stderr), file=sys.stderr)


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


def _section_heading(title: str, *, icon: str = "▸") -> str:
    """区块标题文本；图标随 `_icons_enabled()` 开关（与 proxy.sh 的 section_label 对齐）。

    与 :func:`_section_title` 的分工：`_section_title` 只是“加粗蓝字”原语，也被
    通知类文本复用；本函数专用于**区块标题**，因此承载图标门控。
    """
    return _section_title(f"{icon} {title}" if _icons_enabled() else title)


# 区块标题下划线宽度：定长横线给面板以“标题带”的分隔感，宽度不随标题
# 变化（CJK 标题长短不一，随标题定宽会显得零碎）
_SECTION_RULE_WIDTH = 36


def _section_rule() -> str:
    """标题下的细分隔线；配色与标题一致，NO_COLOR 下为普通横线。"""
    return _style("─" * _SECTION_RULE_WIDTH, ANSI_BLUE)


def _print_section(title: str) -> None:
    print(_section_heading(title))
    print(_section_rule())


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
    print(
        f"{standby_group} -> {normalize_name(standby_node)}"
        f" ({_format_delay_label(standby_delay)}, {_status_label(standby_status)})"
    )
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


# status 面板显示参数（进程行数的默认值属于 CLI 契约，见 output.STATUS_PROCESS_TOP_DEFAULT）
_STATUS_PROCESS_LABEL_WIDTH = 46
_STATUS_PATH_WIDTH = 48


def _today_traffic_summary(paths) -> dict | None:
    """今日流量汇总（代理/直连），数据库缺失或异常时返回 None，绝不阻塞 status。"""
    try:
        audit = TrafficService(paths).audit(days=1, top=1)
    except Exception:
        return None
    if not any(
        (
            audit["proxy_download"],
            audit["proxy_upload"],
            audit["direct_download"],
            audit["direct_upload"],
        )
    ):
        return None
    return audit


def _today_process_traffic(paths, top: int) -> ProcessTrafficReport | None:
    """今日按进程流量（全量 + 代理/直连拆分）；top<=0 或采集失败时返回 None。"""
    if top <= 0:
        return None
    try:
        return TrafficService(paths).process_breakdown(days=1, top=top)
    except Exception:
        return None


def _connection_count(paths) -> int | None:
    """Mihomo 当前连接数；API 不可达时返回 None。"""
    try:
        connections = APIBackend(paths).get_connections().get("connections")
    except Exception:
        return None
    return len(connections) if isinstance(connections, list) else None


def _render_traffic_totals(traffic: dict) -> None:
    """今日总量 / 代理 / 直连三行；代理与直连带占比与流量条。

    链路着色：代理 ↓绿/↑青，直连 ↓黄/↑洋红（与四列进程表的列色一致），
    一眼区分谁走代理、谁在直连。
    """
    total_down = traffic["proxy_download"] + traffic["direct_download"]
    total_up = traffic["proxy_upload"] + traffic["direct_upload"]
    proxy_all = traffic["proxy_download"] + traffic["proxy_upload"]
    direct_all = traffic["direct_download"] + traffic["direct_upload"]
    all_traffic = proxy_all + direct_all
    # (标签, ↓, ↑, 占比分子|None, ↓色, ↑色, 条形色)
    # 条形色与行链路一致：代理行绿条（长度=代理占比）、直连行黄条（长度=直连占比），
    # 与按进程表的双色条（同一条内绿黄分段）语义不同，不可混用 _chain_traffic_bar
    rows = [
        ("今日总量", total_down, total_up, None, ANSI_GREEN, ANSI_CYAN, ANSI_GREEN),
        ("代理", traffic["proxy_download"], traffic["proxy_upload"], proxy_all, ANSI_GREEN, ANSI_CYAN, ANSI_GREEN),
        ("直连", traffic["direct_download"], traffic["direct_upload"], direct_all, ANSI_YELLOW, ANSI_MAGENTA, ANSI_YELLOW),
    ]
    label_width = max(_display_width(row[0]) for row in rows) + _KV_GUTTER
    down_w = max(_display_width("↓" + format_bytes(row[1])) for row in rows)
    up_w = max(_display_width("↑" + format_bytes(row[2])) for row in rows)
    ratio_w = max(
        (_display_width(f"{row[3] / all_traffic * 100 if all_traffic else 0.0:.1f}%") for row in rows if row[3] is not None),
        default=0,
    )
    for label, down, up, part, down_color, up_color, bar_color in rows:
        line = (
            f"{_pad_right(label, label_width)}"
            f"{_style(_pad_left('↓' + format_bytes(down), down_w), down_color)}"
            f"  {_style(_pad_left('↑' + format_bytes(up), up_w), up_color)}"
        )
        if part is not None:
            ratio = part / all_traffic * 100 if all_traffic else 0.0
            # 流量条位于行尾，不做定宽填充，避免行尾空白
            line += f"   {_pad_left(f'{ratio:.1f}%', ratio_w)}  {_traffic_bar(part, all_traffic, pad=False, color=bar_color)}"
        print(line)


def _render_process_traffic(report: ProcessTrafficReport | None) -> None:
    """按进程流量子表：占比 / 代理↓ 代理↑ 直连↓ 直连↑ / 流量条 / 进程。"""
    if report is None or not report.rows:
        return
    print()
    print(f"  进程 Top {len(report.rows)}")
    _render_traffic_table(
        report.rows,
        lambda row: _process_display_label(row.label, width=_STATUS_PROCESS_LABEL_WIDTH),
        header="进程",
        columns=_chain_split_columns(),
        total=report.total,
        bar_of=_process_row_bar,
    )


# 订阅用量着色/提示阈值：余量不足 20% 转黄、耗尽转红；到期 7 天内转黄、
# 已过期转红；记录超 48h（订阅每日 04:00 刷新）标注数据截至时间
_SUBSCRIPTION_LOW_RATIO = 0.2
_SUBSCRIPTION_EXPIRE_SOON_DAYS = 7
_SUBSCRIPTION_EXPIRE_NOTICE_DAYS = 30
_SUBSCRIPTION_STALE_HOURS = 48


def _format_subscription_usage(usage: SubscriptionUsage) -> str:
    """单条订阅用量一行：剩余/总量（含已用占比）与到期日。

    按余量与到期紧迫度着色（正常绿 / 偏低黄 / 耗尽或过期红）；
    total 缺失时退化为展示已用上下行。
    """
    parts: list[str] = []
    if usage.total:
        remaining = usage.remaining or 0
        pct_used = min(100.0, usage.used / usage.total * 100)
        text = f"剩余 {format_bytes(remaining)} / {format_bytes(usage.total)}（已用 {pct_used:.1f}%）"
        if remaining <= 0:
            color = ANSI_RED
        elif remaining < usage.total * _SUBSCRIPTION_LOW_RATIO:
            color = ANSI_YELLOW
        else:
            color = ANSI_GREEN
        parts.append(_style(text, color))
    else:
        parts.append(f"已用 ↓{format_bytes(usage.download or 0)} ↑{format_bytes(usage.upload or 0)}")
    if usage.expire:
        expire_day = _safe_timestamp_date(usage.expire)
        if expire_day is not None:
            days = (expire_day - date.today()).days
            text = f"到期 {expire_day:%Y-%m-%d}"
            if days < 0:
                parts.append(_style(f"{text}（已过期 {-days} 天）", ANSI_RED))
            elif days <= _SUBSCRIPTION_EXPIRE_SOON_DAYS:
                parts.append(_style(f"{text}（剩 {days} 天）", ANSI_YELLOW))
            elif days <= _SUBSCRIPTION_EXPIRE_NOTICE_DAYS:
                parts.append(f"{text}（剩 {days} 天）")
            else:
                parts.append(text)
    stale = _subscription_stale_suffix(usage)
    if stale:
        parts.append(stale)
    return " · ".join(parts)


def _safe_timestamp_date(timestamp: int) -> date | None:
    """Unix 秒 → 本地日期；越界/畸形值返回 None（跳过到期行，不崩面板）。"""
    try:
        return datetime.fromtimestamp(timestamp).date()
    except (ValueError, OSError, OverflowError):
        return None


def _subscription_stale_suffix(usage: SubscriptionUsage) -> str:
    """记录超 48h 未更新时标注数据截至时间，提示订阅刷新可能已失败。"""
    try:
        updated = datetime.fromisoformat(usage.updated_at)
        age = datetime.now() - updated
    except (ValueError, TypeError):
        # 畸形时间串，或带时区的 aware 时间与本地 naive 相减
        return ""
    if age < timedelta(hours=_SUBSCRIPTION_STALE_HOURS):
        return ""
    return f"数据截至 {updated:%m-%d %H:%M}"


def _render_subscription_info(entries: list[tuple[str, SubscriptionUsage]]) -> None:
    """「订阅」区块：主订阅在前、附加机场按配置顺序，各自一行用量。"""
    if not entries:
        return
    print()
    _print_section("订阅")
    _render_kv([(label, _format_subscription_usage(usage)) for label, usage in entries])


def _render_status(raw: bool, process_top: int = STATUS_PROCESS_TOP_DEFAULT) -> int:
    paths = default_paths()
    snapshot = get_status(paths)
    config_state = "已就绪" if snapshot.runtime_ready else "待刷新"
    status_text = "运行中" if snapshot.running else "未运行"
    api_text = "不可访问"
    ai_mode = "-"
    ai_summary = "-"

    try:
        route = _resolve_ai_route(QueryService(paths).get_ai_status_groups())
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
        traffic = _today_traffic_summary(paths)
        if traffic is not None:
            total_down = traffic["proxy_download"] + traffic["direct_download"]
            total_up = traffic["proxy_upload"] + traffic["direct_upload"]
            print(f"今日流量: down={total_down} up={total_up}")
            print(f"今日代理: down={traffic['proxy_download']} up={traffic['proxy_upload']}")
            print(f"今日直连: down={traffic['direct_download']} up={traffic['direct_upload']}")
        for label, usage in display_entries(paths):
            print(
                f"订阅 {label}: upload={usage.upload} download={usage.download}"
                f" total={usage.total} expire={usage.expire} updated_at={usage.updated_at}"
            )
        return 0

    title = "◆ cproxy" if _icons_enabled() else "cproxy"
    print(_style(title, ANSI_BOLD, ANSI_BLUE))
    print(_section_rule())

    _print_section("摘要")
    _render_kv(
        [
            ("状态", _status_label(status_text)),
            ("API", _status_label(api_text)),
            ("运行配置", _status_label(config_state)),
            ("AI 路由", ai_mode),
            ("当前出口", _accent(ai_summary)),
        ]
    )

    print()
    _print_section("资源")
    metrics = collect_runtime_metrics(paths, snapshot.pid)
    # API 已知不可达时不再试一次——否则白白多等一个 api-timeout（默认 2s）
    connections = _connection_count(paths) if api_text == "可访问" else None
    _render_kv(
        [
            ("代理端口", snapshot.port),
            ("控制接口", snapshot.controller),
            ("PID", str(snapshot.pid) if snapshot.pid else ""),
            ("连接数", "" if connections is None else str(connections)),
            ("运行时间", "" if metrics.uptime_seconds is None else _format_uptime(metrics.uptime_seconds)),
            ("内存", "" if metrics.memory_bytes is None else format_bytes(metrics.memory_bytes)),
            ("日志", "" if metrics.log_bytes is None else format_bytes(metrics.log_bytes)),
        ]
    )

    traffic = _today_traffic_summary(paths)
    if traffic is not None:
        print()
        _print_section("流量 (今日)")
        _render_traffic_totals(traffic)
        _render_process_traffic(_today_process_traffic(paths, process_top))

    _render_subscription_info(display_entries(paths))

    print()
    _print_section("路径")
    path_rows = [
        ("原始配置", _shorten_path(snapshot.source_config, _STATUS_PATH_WIDTH)),
        ("运行配置", _shorten_path(snapshot.runtime_config, _STATUS_PATH_WIDTH)),
    ]
    # 仅当运行实例加载的 runtime 内容已落后于磁盘当前版本时才展示：
    # runtime 路径恒定，所以信号是**内容时效**而非路径差异
    if snapshot.runtime_stale:
        path_rows.append(("配置时效", _status_label("待刷新") + "  运行实例未跟随最近一次 render"))
    _render_kv(path_rows)

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
    best = min(ok_items, key=lambda item: item.delay or 0) if ok_items else None
    worst = max(ok_items, key=lambda item: item.delay or 0) if ok_items else None

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
    _print_section("结果")
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
        for extra in report.extra_subscriptions:
            print(f"extra:{extra.name}={extra.status} detail={extra.detail}")
        print(f"runtime={report.runtime_path}")
        print(f"restarted={report.restarted} hot_reloaded={report.hot_reloaded}")
        for item in report.groups:
            print(f"{item.group}: {item.action} current={item.current or '-'} target={item.target or '-'} {item.detail}")
        return 0

    _print_section("摘要")
    subscription_label = report.subscription
    if report.subscription_detail:
        subscription_label = f"{subscription_label}  {report.subscription_detail}"
    print(f"订阅更新: {subscription_label}")
    for extra in report.extra_subscriptions:
        extra_label = extra.status
        if extra.detail:
            extra_label = f"{extra_label}  {extra.detail}"
        print(f"附加订阅 {extra.name}: {extra_label}")
    print(f"运行配置: {report.runtime_path}")
    if report.restarted:
        print("代理: 已重启应用新配置（连接已中断）")
    elif report.hot_reloaded:
        print("代理: 已热重载应用新配置（连接不中断）")
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


_DIMENSION_TITLES = {
    "node": "按出口链路",
    "rule": "按命中规则",
    "host": "按目标主机",
    "process": "按进程",
}

_DIMENSION_LABEL_TITLES = {
    "node": "链路",
    "rule": "规则",
    "host": "主机",
    "process": "进程",
}

_TRAFFIC_BAR_WIDTH = 20
_TRAFFIC_BAR_PARTIALS = "▏▎▍▌▋▊▉"


def _display_width(text: str) -> int:
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in text)


def _pad_left(text: str, width: int) -> str:
    return " " * max(0, width - _display_width(text)) + text


def _pad_right(text: str, width: int) -> str:
    return text + " " * max(0, width - _display_width(text))


def _traffic_bar(value: int, max_value: int, *, pad: bool = True, color: str = ANSI_GREEN) -> str:
    """纯文本 ASCII 流量条（先 pad 后上色，保证宽度计算不受 ANSI 转义干扰）。

    ``pad=False`` 用于流量条位于行尾的场景，避免留下行尾空白；
    表格内需要靠它对齐后续列，保持默认的定宽填充。
    ``color`` 供链路着色：代理流量条绿色、直连流量条黄色。
    """
    if max_value <= 0 or value <= 0:
        # 表格内需占位：返回空串会让该行标签左移一整个条形宽度、整表错位
        return _pad_right("", _TRAFFIC_BAR_WIDTH) if pad else ""
    ratio = min(1.0, value / max_value)
    scaled = _TRAFFIC_BAR_WIDTH * ratio
    full = int(scaled)
    partial = ""
    if full < _TRAFFIC_BAR_WIDTH:
        frac_idx = min(len(_TRAFFIC_BAR_PARTIALS) - 1, int((scaled - full) * len(_TRAFFIC_BAR_PARTIALS)))
        partial = _TRAFFIC_BAR_PARTIALS[frac_idx]
    bar = "█" * full + partial
    if pad:
        bar = _pad_right(bar, _TRAFFIC_BAR_WIDTH)
    return _style(bar, color)


def _chain_traffic_bar(proxy: int, direct: int, max_value: int, *, pad: bool = True) -> str:
    """双色流量条：绿段=代理、黄段=直连，条宽仍按总量占比。

    与 `_traffic_bar` 相同的拼条/pad/0 字节占位契约；先拼纯文本条再按
    代理比例切分上色，宽度计算不受 ANSI 转义干扰。
    """
    total = proxy + direct
    if max_value <= 0 or total <= 0:
        return _pad_right("", _TRAFFIC_BAR_WIDTH) if pad else ""
    scaled = _TRAFFIC_BAR_WIDTH * min(1.0, total / max_value)
    full = int(scaled)
    partial = ""
    if full < _TRAFFIC_BAR_WIDTH:
        frac_idx = min(len(_TRAFFIC_BAR_PARTIALS) - 1, int((scaled - full) * len(_TRAFFIC_BAR_PARTIALS)))
        partial = _TRAFFIC_BAR_PARTIALS[frac_idx]
    bar = "█" * full + partial
    split = int(_display_width(bar) * proxy / total)
    segments = ((bar[:split], ANSI_GREEN), (bar[split:], ANSI_YELLOW))
    colored = "".join(_style(seg, color) for seg, color in segments if seg)
    if not pad:
        return colored
    return colored + " " * max(0, _TRAFFIC_BAR_WIDTH - _display_width(bar))


def _process_row_bar(row, dim_max: int) -> str:
    """按进程行流量条：绿段=代理、黄段=直连（条宽按该进程总量）。"""
    proxy = row.proxy_download + row.proxy_upload
    total = row.download + row.upload
    return _chain_traffic_bar(proxy, total - proxy, dim_max)


def _traffic_column_widths(rows: list, extra_headers: list[str]) -> tuple[int, int]:
    down_texts = [format_bytes(row.download) for row in rows] + [extra_headers[0]]
    up_texts = [format_bytes(row.upload) for row in rows] + [extra_headers[1]]
    return (
        max(_display_width(text) for text in down_texts),
        max(_display_width(text) for text in up_texts),
    )


def _process_display_label(label: str, *, width: int | None = None) -> str:
    """人读进程标签：保留完整路径便于定位，仅剥离 /proc 的 " (deleted)" 标记。

    传入 ``width`` 时改为紧凑显示（见 :func:`_shorten_path`），供 status 面板使用；
    不传时维持“完整路径”语义，供 traffic 报表使用。
    """
    display = label.removesuffix(" (deleted)")
    if width is None:
        return display
    return _shorten_path(display, width)


# 压缩路径时跳过的“噪声”目录段：这些段对辨识进程几乎没有贡献，却极占宽度。
_PATH_NOISE_SEGMENTS = frozenset(
    {
        "bin",
        "build",
        "dist",
        "etc",
        "lib",
        "lib64",
        "libexec",
        "local",
        # nvm / pipx 风格的 "versions/node/vX.Y.Z" 安装层级，对辨识进程无贡献
        "node",
        "node_modules",
        "opt",
        "sbin",
        "share",
        "site-packages",
        "usr",
        "vendor",
        "versions",
    }
)
# 版本号段与目标平台三元组段，例如 v22.23.1 / x86_64-unknown-linux-musl。
_PATH_NOISE_RE = re.compile(r"^(?:v\d+(?:\.\d+)*|(?:x86_64|aarch64|arm64|i686|amd64)[\w.-]*)$")


def _home_relative(text: str) -> str:
    home = str(Path.home())
    if not home or home == "/":
        return text
    if text == home:
        return "~"
    if text.startswith(home + "/"):
        return "~" + text[len(home):]
    return text


def _is_noise_segment(segment: str) -> bool:
    if not segment:
        return True
    return segment in _PATH_NOISE_SEGMENTS or bool(_PATH_NOISE_RE.match(segment))


def _shorten_path(text: str, width: int) -> str:
    """把超长路径压进 ``width`` 显示宽度，尽量保留有辨识度的尾段。

    先做 ``$HOME`` → ``~``；仍超宽时从尾部回升段落，跳过噪声段
    （``node_modules`` / ``bin`` / 版本号 / 平台三元组等），前缀 ``…/``。
    """
    if not text or text == "-":
        return text

    display = _home_relative(text)
    if _display_width(display) <= width:
        return display

    parts = display.split("/")
    if len(parts) <= 1:
        return display

    tail = [parts[-1]]
    # parts[0] 是根标记（绝对路径的空串，或 `~`），与末段一起不进候选循环
    for segment in reversed(parts[1:-1]):
        if _is_noise_segment(segment):
            continue
        candidate = "…/" + "/".join([segment, *tail])
        if _display_width(candidate) > width:
            break
        tail.insert(0, segment)
    result = "…/" + "/".join(tail)
    if _display_width(result) <= width:
        return result

    # 末段自身就超宽（如极长可执行名）：上述循环只能丢中间段，压不进 width。
    # 保头部截断（路径头部比尾部更易辨识）并补省略号，确保真的满足列宽契约。
    budget = max(0, width - _display_width("…"))
    head = ""
    for ch in result:
        if _display_width(head + ch) > budget:
            break
        head += ch
    return head + "…"


def _format_uptime(seconds: int) -> str:
    hours, remainder = divmod(max(0, int(seconds)), 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours}h {minutes:02d}m {secs:02d}s"


# “标签  值”两列布局的间距（区块内标签列宽 = 最长标签 + 该间距）。
_KV_GUTTER = 4


def _render_kv(rows: list[tuple[str, str]]) -> None:
    """按东亚宽度对齐输出「标签  值」两列；空值行自动跳过。"""
    visible = [(label, value) for label, value in rows if value]
    if not visible:
        return
    width = max(_display_width(label) for label, _ in visible) + _KV_GUTTER
    for label, value in visible:
        print(f"{_pad_right(label, width)}{value}")


def _total_size_columns() -> list[tuple[str, Callable[[Any], str], str]]:
    """「↓下载 / ↑上传」两列：按上下行合计口径（方向色：↓绿/↑青）。"""
    return [
        ("↓下载", lambda row: format_bytes(row.download), ANSI_GREEN),
        ("↑上传", lambda row: format_bytes(row.upload), ANSI_CYAN),
    ]


def _chain_split_columns() -> list[tuple[str, Callable[[Any], str], str]]:
    """「代理↓ / 代理↑ / 直连↓ / 直连↑」四列：按链路拆分并着色。

    比单个代理占比更直白——低代理占比的进程（如 0.1%）在百分比下几乎看不出量，
    拆成字节后 0 B / 324 MB 的对比一目了然。链路用色相区分（代理绿/青，
    直连黄/洋红），方向靠箭头：表头即图例。
    """
    return [
        ("代理↓", lambda row: format_bytes(row.proxy_download), ANSI_GREEN),
        ("代理↑", lambda row: format_bytes(row.proxy_upload), ANSI_CYAN),
        ("直连↓", lambda row: format_bytes(row.download - row.proxy_download), ANSI_YELLOW),
        ("直连↑", lambda row: format_bytes(row.upload - row.proxy_upload), ANSI_MAGENTA),
    ]


def _render_traffic_table(
    rows: list,
    label_of,
    *,
    header: str,
    columns: list[tuple[str, Callable[[Any], str], str]],
    total: int,
    bar_of: Callable[[Any, int], str] | None = None,
) -> None:
    """渲染「占比 | <数值列…> | 流量条 | 标签」对齐表格。

    ``columns`` 是按顺序渲染的数值列 ``(表头, 取值函数, 颜色)``，取值函数返回
    **未着色**的格式化字符串——上色在 pad 之后进行，宽度计算不受 ANSI 转义
    干扰；表头单元格带同色（加粗），兼作列色图例。因此同一套排版既能渲染
    「↓下载 / ↑上传」，也能渲染「代理↓ / 代理↑ / 直连↓ / 直连↑」这类按链路
    拆分的列。``total`` 是占比列分母。``bar_of(row, dim_max)`` 自定义流量条
    （如按进程行的绿/黄双色条），缺省为总量的绿色单色条。
    """
    widths = [
        max([_display_width(title), *(_display_width(value_of(row)) for row in rows)])
        for title, value_of, _ in columns
    ]
    # 逐段独立加粗而非整行嵌套包一层：内层列色的 RESET 会截断外层加粗
    head = _style(f"  {_pad_left('占比', 6)}", ANSI_BOLD) + "".join(
        f"  {_style(_pad_left(title, width), ANSI_BOLD, color)}"
        for (title, _, color), width in zip(columns, widths)
    )
    head += _style(f"  {_pad_right('流量', _TRAFFIC_BAR_WIDTH)}  {header}", ANSI_BOLD)
    print(head)

    dim_max = max((row.download + row.upload for row in rows), default=0)
    for row in rows:
        size = row.download + row.upload
        pct = size / total * 100 if total else 0.0
        line = f"  {_pad_left(f'{pct:.1f}%', 6)}" + "".join(
            f"  {_style(_pad_left(value_of(row), width), color)}"
            for (_, value_of, color), width in zip(columns, widths)
        )
        bar = bar_of(row, dim_max) if bar_of is not None else _traffic_bar(size, dim_max)
        print(f"{line}  {bar}  {label_of(row)}")


def _render_traffic(paths, *, action: str, days: int, by: str | None, top: int, raw: bool) -> int:
    service = TrafficService(paths)
    if action == "collect":
        result = service.collect()
        if raw:
            print(
                f"TRAFFIC_COLLECT\tconnections={result.connections}"
                f"\tdown={result.download_delta}\tup={result.upload_delta}"
            )
            return 0
        _print_section("流量采集完成")
        print(f"连接数: {result.connections}")
        print(f"本周期增量: ↓{format_bytes(result.download_delta)} ↑{format_bytes(result.upload_delta)}")
        return 0

    if action == "audit":
        audit = service.audit(days=days, top=top)
        if raw:
            # 进程查询只在确实要输出时才做：下文的“暂无流量记录”早退不应白跑一次查询
            processes = service.process_breakdown(days=days, top=top)
            print(
                f"TRAFFIC_AUDIT\tsince={audit['since']}\tuntil={audit['until']}"
                f"\tproxy_down={audit['proxy_download']}\tproxy_up={audit['proxy_upload']}"
                f"\tdirect_down={audit['direct_download']}\tdirect_up={audit['direct_upload']}"
            )
            for row in audit["rows"]:
                print(f"TRAFFIC_AUDIT_HOST\t{row.label}\tdown={row.download}\tup={row.upload}")
            # 注意 down/up 为全量口径；proxy_down/proxy_up 是其中走代理的部分
            # 字段名用 total_* 而非沿用 down/up：该行口径已从“仅代理”改为“全量”，
            # 复用旧字段名会让外部脚本静默拿到含 ~92% 直连的数字。改名后旧解析会在
            # 取值时立刻暴露，而不是悄悄算错。
            for row in processes.rows:
                print(
                    f"TRAFFIC_AUDIT_PROCESS\t{row.label}\ttotal_down={row.download}\ttotal_up={row.upload}"
                    f"\tproxy_down={row.proxy_download}\tproxy_up={row.proxy_upload}"
                )
            return 0
        window = (
            f"{audit['since']}"
            if audit["since"] == audit["until"]
            else f"{audit['since']} ~ {audit['until']}"
        )
        _print_section(f"代理流量审计 ({window})")
        proxy_all = audit["proxy_download"] + audit["proxy_upload"]
        direct_all = audit["direct_download"] + audit["direct_upload"]
        if proxy_all == 0 and direct_all == 0:
            print("暂无流量记录 (collector 尚未采集或数据库为空)")
            return 0
        processes = service.process_breakdown(days=days, top=top)
        proxy_ratio = proxy_all / (proxy_all + direct_all) * 100 if (proxy_all + direct_all) else 0.0
        print(
            f"代理: {_style('↓' + format_bytes(audit['proxy_download']), ANSI_GREEN)}"
            f" {_style('↑' + format_bytes(audit['proxy_upload']), ANSI_CYAN)}"
            f"  ({proxy_ratio:.1f}%)"
            f"    直连: {_style('↓' + format_bytes(audit['direct_download']), ANSI_YELLOW)}"
            f" {_style('↑' + format_bytes(audit['direct_upload']), ANSI_MAGENTA)}"
        )
        rows = audit["rows"]
        if rows:
            print()
            print("仅代理流量, 按目标主机:")
            _render_traffic_table(
                rows,
                lambda row: row.label,
                header="主机",
                columns=_total_size_columns(),
                total=proxy_all,
            )
        if processes.rows:
            print()
            print("按进程 (全量, 代理/直连拆分):")
            _render_traffic_table(
                processes.rows,
                lambda row: _process_display_label(row.label),
                header="进程",
                columns=_chain_split_columns(),
                total=processes.total,
                bar_of=_process_row_bar,
            )
        if not rows and not processes.rows:
            print("窗口内没有走代理的流量")
        return 0

    report = service.report(days=days, dimension=by, top=top)
    if raw:
        print(
            f"TRAFFIC_REPORT\tsince={report['since']}\tuntil={report['until']}"
            f"\tdown={report['total_download']}\tup={report['total_upload']}"
        )
        for dimension, rows in report["dimensions"].items():
            for row in rows:
                print(f"TRAFFIC_{dimension.upper()}\t{row.label}\tdown={row.download}\tup={row.upload}")
        for row in report["daily"]:
            print(f"TRAFFIC_DAY\t{row.label}\tdown={row.download}\tup={row.upload}")
        return 0

    window = (
        f"{report['since']}"
        if report["since"] == report["until"]
        else f"{report['since']} ~ {report['until']}"
    )
    _print_section(f"流量统计 ({window})")
    if report["total_download"] == 0 and report["total_upload"] == 0:
        print("暂无流量记录 (collector 尚未采集或数据库为空)")
        return 0
    total_download = report["total_download"]
    total_upload = report["total_upload"]
    total_all = total_download + total_upload
    print(
        f"总计: {_style('↓' + format_bytes(total_download), ANSI_GREEN)}"
        f"  {_style('↑' + format_bytes(total_upload), ANSI_CYAN)}"
    )

    daily_rows = report["daily"]
    if len(daily_rows) > 1:
        print()
        print("按日期:")
        down_w, up_w = _traffic_column_widths(daily_rows, ["↓下载", "↑上传"])
        print(
            _style(
                f"  {_pad_right('日期', 12)}"
                f"  {_pad_left('↓下载', down_w)}"
                f"  {_pad_left('↑上传', up_w)}"
                "  流量",
                ANSI_BOLD,
            )
        )
        day_max = max(row.download + row.upload for row in daily_rows)
        for row in daily_rows:
            print(
                f"  {_pad_right(row.label, 12)}"
                f"  {_pad_left(format_bytes(row.download), down_w)}"
                f"  {_pad_left(format_bytes(row.upload), up_w)}"
                f"  {_traffic_bar(row.download + row.upload, day_max)}"
            )

    for dimension, rows in report["dimensions"].items():
        print()
        print(_DIMENSION_TITLES.get(dimension, dimension) + ":")
        if not rows:
            print("  -")
            continue
        _render_traffic_table(
            rows,
            (lambda row: _process_display_label(row.label)) if dimension == "process" else (lambda row: row.label),
            header=_DIMENSION_LABEL_TITLES.get(dimension, dimension),
            columns=_total_size_columns(),
            total=total_all,
        )
    return 0


def _render_ipcheck(paths, *, ip: str | None, group: str | None, node: str | None, timeout: int, raw: bool) -> int:
    service = IpCheckService(paths)
    report = service.check(ip=ip, group=group, node=node, timeout=timeout)

    if raw:
        print(
            f"IP_CHECK\tip={report.ip}\tcountry={report.country}\tcity={report.city}"
            f"\tisp={report.isp}\tasn={report.asn}\tip_type={report.ip_type}"
            f"\tnative={report.native_type}\trisk={report.risk if report.risk is not None else '-'}"
            f"\tverdict={report.verdict}\tsignals={','.join(report.signals) or '-'}"
            f"\tblocklist={','.join(report.blocklist_listed) or 'clean'}"
            f"\tdatacenter={report.datacenter or '-'}"
        )
        for item in report.services:
            print(f"IP_SERVICE\t{item.key}\t{item.status}")
        for item in report.sources:
            print(f"IP_SOURCE\t{item.source}\trisk={item.risk}\tweight={item.weight}")
        return 0

    target = f"指定 IP {report.ip}" if ip else "当前代理出口"
    _print_section(f"IP 纯净度检测 ({target})")
    print(f"出口 IP: {report.ip}  ({report.country} {report.city})")
    print(f"ISP/ASN: {report.isp}  {report.asn}")
    if report.datacenter:
        print(f"数据中心: {report.datacenter}")
    print(f"IP 类型: {report.ip_type} / {report.usage_type}  原生性: {report.native_type}")
    print(
        f"风险评分: {report.risk if report.risk is not None else '-'} / 100  "
        f"判定: {report.verdict}"
    )
    if report.signals:
        print(f"风险信号: {', '.join(report.signals)}")
    if report.blocklist_checked:
        listed = ", ".join(report.blocklist_listed) if report.blocklist_listed else "无"
        print(f"黑名单: {listed} ({len(report.blocklist_listed)}/{report.blocklist_checked} 命中)")
    if report.shared_users:
        print(f"共享用户: {report.shared_users} (质量: {report.shared_quality})")

    if report.ai_services:
        print()
        print("AI 服务快照:")
        for item in report.ai_services:
            print(f"  {item.key:<10} {item.label}")
    if report.services and not report.ai_services:
        print()
        print("服务快照:")
        for item in report.services:
            print(f"  {item.key:<10} {item.label}")

    if report.sources:
        print()
        print("多源评分:")
        for item in report.sources:
            print(f"  {item.source:<14} risk={item.risk:<3} weight={item.weight}")

    if report.switched:
        print()
        print(
            f"检测期间临时切换 {report.switch_group} -> {normalize_name(node or '')}"
            f"，已恢复为 {normalize_name(report.previous_selection)}"
        )
    return 0
