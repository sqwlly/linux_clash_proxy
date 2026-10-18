from __future__ import annotations

import os
import re
import sys
from collections.abc import Callable, Sequence
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any

from . import __version__
from .api import APIUnavailableError
from .backend.api import APIBackend
from .backend.models import AIProbeReport, ProxyGroup
from .backend.runtime import (
    AI_AUTO_GROUP,
    AI_GEMINI_GROUP,
    AI_SG_GROUP,
    AI_STATUS_GROUPS,
    AI_US_GROUP,
    ai_exit_metrics,
    delay_state,
    resolve_ai_leaf,
    resolve_ai_route,
)
from .backend.runtime_metrics import collect_runtime_metrics
from .config import default_paths, log_file, read_config
from .diagnostics import ConnectivityReport, GroupCheckReport, run_ai_probe
from .doctor import run_doctor
from .feedback import Spinner
from .geodata import check_country_mmdb
from .install import auto_migrate_from_default_legacy, init_user_layout, is_placeholder_config
from .logs import follow_lines, read_recent_lines
from .output import STATUS_PROCESS_TOP_DEFAULT, normalize_name
from .process import ProcessBackend, get_status, restart_process, start_process
from .runtime import render_runtime
from .security import validate_controller_security
from .services.ipcheck import IpCheckService
from .services.nodelist import is_panel_info_node
from .services.ops import build_incident, get_ai_connections
from .services.probe_history import load_history_rows, probe_history_file
from .services.query import QueryService
from .services.refresh import RefreshReport, update_source_from_subscription
from .services.subscription_info import SubscriptionUsage, display_entries
from .services.switch_tree import lookup_choice, resolve_group_query
from .services.traffic import ProcessTrafficReport, TrafficService, format_bytes
from .snapshots import (
    compact_snapshots,
    format_relative_time,
    list_snapshots,
    parse_snapshot_time,
    restore_snapshot,
    snapshot_diff,
    snapshot_kind,
    snapshot_tier_label,
    snapshots_dir,
)
from .structured_output import emit_json
from .textwidth import display_width as _display_width
from .textwidth import pad_left as _pad_left
from .textwidth import pad_right as _pad_right

ANSI_RESET = "\033[0m"
ANSI_BOLD = "\033[1m"
ANSI_DIM = "\033[2m"
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


def _status_cell(text: str) -> tuple[str, tuple[str, ...]]:
    """状态文案 → 「带图标的文本 + 上色码」。

    文本与色码**分开**返回：表格要先把文本 pad 到列宽、再上色（见 `_render_table`），
    否则 ANSI 转义会掺进宽度计算。图标受 `_icons_enabled()` 门控（`NO_COLOR` 下
    同时关掉颜色与图标）。

    注：图标现在与文本同色（此前图标是无色、文本有色），属刻意的外观统一。
    """
    if text in POSITIVE_STATUSES:
        codes: tuple[str, ...] = (ANSI_GREEN,)
    elif text in WARNING_STATUSES:
        codes = (ANSI_YELLOW,)
    elif text in NEGATIVE_STATUSES:
        codes = (ANSI_RED,)
    else:
        codes = ()
    icon = STATUS_ICONS.get(text) if _icons_enabled() else None
    return (f"{icon} {text}" if icon else text), codes


def _status_label(text: str) -> str:
    label, codes = _status_cell(text)
    return _style(label, *codes)


def _section_heading(title: str, *, icon: str = "▸") -> str:
    """区块标题文本；图标随 `_icons_enabled()` 开关（沿用历史 legacy `proxy.sh` 的
    `section_label`，该脚本已于 2026-10-17 删除）。

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


def _print_section(title: str, *, rule: bool = True) -> None:
    print(_section_heading(title))
    if rule:
        print(_section_rule())


#: 表格列间距
_TABLE_GUTTER = "  "

#: 表格单元格：`(未着色文本, 上色码)`
TableCell = tuple[str, tuple[str, ...]]


def _table_widths(headers: list[str], rows: list[list[TableCell]]) -> list[int]:
    """逐列宽度（取表头与所有单元格文本的最大显示宽度）。"""
    return [
        max(_display_width(headers[i]), *(_display_width(row[i][0]) for row in rows))
        for i in range(len(headers))
    ]


def _render_table(
    headers: list[str],
    rows: list[list[TableCell]],
    *,
    aligns: list[str] | None = None,
    widths: list[int] | None = None,
    grid: bool = False,
) -> None:
    """渲染列对齐的表格。

    单元格传**未着色文本 + 上色码**，pad 到列宽之后再上色——与
    `_render_traffic_table` 同一心法，ANSI 转义不会掺进宽度计算（CJK 走
    `display_width`）。`aligns` 逐列给 `"l"`/`"r"`，缺省全左；表头加粗。

    `grid=True` 时画出完整的行列分隔线（`│` 竖线 + 表头下与各行间的 `─` 横线，
    浅色），表宽相应变宽；缺省只用列间距，留给不需要视觉重量表的调用方。

    列宽完全由内容决定，不感知终端宽度：过窄的终端会折行，但不会丢信息。
    """
    if not rows:
        return
    aligns = aligns or ["l"] * len(headers)
    widths = widths if widths is not None else _table_widths(headers, rows)

    def cells_of(row: list[TableCell]) -> list[str]:
        out = []
        for (text, codes), width, align in zip(row, widths, aligns):
            padded = _pad_left(text, width) if align == "r" else _pad_right(text, width)
            out.append(_style(padded, *codes))
        return out

    if grid:
        # 每列左右各留一空格，边框用浅色以免压过内容
        def grid_line(cells: list[str]) -> str:
            return _style("│ ", ANSI_DIM) + _style(" │ ", ANSI_DIM).join(cells) + _style(" │", ANSI_DIM)

        def node(left: str, mid: str, right: str) -> str:
            return _style(left + mid.join("─" * (width + 2) for width in widths) + right, ANSI_DIM)

        print(node("┌", "┬", "┐"))
        print(grid_line(cells_of([(header, (ANSI_BOLD,)) for header in headers])))
        # 逐行之间也画横线：只隔列不隔行时，相邻行的值容易连读成同一格
        for row in rows:
            print(node("├", "┼", "┤"))
            print(grid_line(cells_of(row)))
        print(node("└", "┴", "┘"))
        return

    def plain_line(cells: list[str]) -> str:
        return _TABLE_GUTTER.join(cells).rstrip()

    print(plain_line(cells_of([(header, (ANSI_BOLD,)) for header in headers])))
    for row in rows:
        print(plain_line(cells_of(row)))


def _exit_summary(group_name: object, node: object, delay: object) -> str:
    summary = f"{group_name} -> {normalize_name(node)}"
    # `None`（无 history）与 `0`（本轮未测得）同属 `unknown`，都不附括号后缀，
    # 避免两边行为分裂；仅在有实测值（`NNNms`）或明确超时（`超时`）时标注。
    if isinstance(delay, int) and delay_state(delay) != "unknown":
        summary = f"{summary} ({_exit_delay_label(delay)})"
    return summary


def _exit_delay_label(delay: object) -> str:
    """AI 出口延迟的三态文案。

    `history` 里的 5000 是探测**超时**哨兵、0 是**未测得**——都不是真实延迟。
    照毫秒展示会把「超时」说成「很快」，所以这里如实标注。
    """
    if not isinstance(delay, int):
        return "未测得"
    return {"ok": f"{delay}ms", "timeout": "超时", "unknown": "未测得"}[delay_state(delay)]


def _exit_delay_cell(delay: object) -> TableCell:
    """延迟单元格：有测量值时无色（数字本身不是告警），`超时`/`未测得` 用色标出。"""
    label = _exit_delay_label(delay)
    if isinstance(delay, int) and delay_state(delay) == "ok":
        return label, ()
    codes: tuple[str, ...] = (ANSI_YELLOW,) if label == "超时" else (ANSI_DIM,)
    return label, codes


def _exit_state_cell(delay: object, alive: object) -> TableCell:
    """状态单元格；延迟不是 ok 时**留空**。

    `超时` 已在延迟列说清，再并排一个 mihomo 的健康标记会读成「超时 · 正常」这种
    自相矛盾的组合——mihomo 的 alive 在单次探测超时后未必翻转。
    """
    if isinstance(delay, int) and delay_state(delay) == "ok":
        return _status_cell(_alive_text(alive))
    return "", ()


def _alive_text(alive: object) -> str:
    """三态健康值 → 文案；`None` 是「mihomo 未上报」，不等于「异常」。"""
    if alive is True:
        return "正常"
    if alive is False:
        return "异常"
    return "未知"


def _delay_cell(delay: object) -> object:
    """CLI 契约里「无延迟」是字符串 `"-"`（`--raw`/`--json` 的下游按字符串解析）。"""
    return "-" if delay is None else delay


def _ai_exit(groups: dict, name: str) -> tuple[str, object, object]:
    """`:func:`ai_exit_metrics` 的 CLI 侧包装：套上 `_delay_cell` 的缺省值约定。

    拓扑解析本身在 `backend.runtime`——那里是 AI 组常量的所在地，services 层
    （`incident` 面板）也要用，放在这里会让 services 反向依赖 CLI 契约层。
    """
    node, delay, alive = ai_exit_metrics(groups, name)
    return node, _delay_cell(delay), alive


def _resolve_ai_route(groups: dict) -> dict[str, object]:
    """`:func:`backend.runtime.resolve_ai_route` 的 CLI 侧适配：展平成 17 键字典。

    键名与含义是 `ai-status --json` 与 `cproxy status` 的既有契约（`schema_version` 1），
    改键名会破坏下游解析，所以这里只做展平——出口/备用/Gemini 的解析口径由运行时侧
    的单一实现负责，CLI 与 TUI 不会各算一套。
    """
    route = resolve_ai_route(groups)
    active, standby, gemini = route.active, route.standby, route.gemini
    return {
        "auto_mode": route.auto_mode,
        # 非自动池这一支的旧值先后是「兜底 <节点名>」（把节点当组名）与「兜底 · 手动」
        # （暗示用户钉住了节点）。两者都不准：出口由 mihomo 在 AI-MANUAL 的**直连候选池**
        # 里按列表顺序取第一个存活节点，池内顺序才是用户意图（`ai-rebalance` 维护），
        # 节点本身并未被钉死——实测出现过韩国节点挂掉后自动落到新加坡。
        "mode_label": "自动切换" if route.auto_mode else "兜底 · 直连池",
        "active_group": active.group,
        "active_node": active.node,
        "active_delay": _delay_cell(active.delay),
        "active_alive": active.alive,
        "active_summary": _exit_summary(active.group, active.node, active.delay),
        "standby_group": standby.group,
        "standby_node": standby.node,
        "standby_delay": _delay_cell(standby.delay),
        "standby_alive": standby.alive,
        "gemini_present": gemini is not None,
        "gemini_group": AI_GEMINI_GROUP,
        "gemini_node": gemini.node if gemini is not None else "-",
        "gemini_delay": _delay_cell(gemini.delay) if gemini is not None else "-",
        "gemini_alive": gemini.alive if gemini is not None else None,
        "gemini_summary": (
            _exit_summary(AI_GEMINI_GROUP, gemini.node, gemini.delay) if gemini is not None else "-"
        ),
    }


def _render_current(groups: dict, group_name: str, raw: bool, json_output: bool = False) -> int:
    current = _group_value(_get_group(groups, group_name), "now")
    if not current:
        raise SystemExit(f"错误: 代理组 [{group_name}] 当前无可读的 now 状态")
    if json_output:
        emit_json("current", {"group": group_name, "current": current, "display_name": normalize_name(current)})
    elif raw:
        print(current)
    else:
        group = _get_group(groups, group_name)
        group_type = _group_value(group, "type")
        candidates = _group_value(group, "all", [])
        delay = _group_value(group, "delay")
        alive = _group_value(group, "alive")

        _print_section("摘要")
        print(f"当前选择: {_accent(normalize_name(current))}")
        print(f"代理组  : {group_name}")
        if group_type:
            print(f"组类型  : {group_type}")
        if isinstance(delay, int):
            print(f"延迟    : {_exit_delay_label(delay)}")
        if alive is not None and alive != "-" and (not isinstance(delay, int) or delay_state(delay) == "ok"):
            status = "正常" if alive is True else "异常" if alive is False else "未知"
            print(f"状态    : {_status_label(status)}")
        if candidates:
            print(f"候选数  : {len(candidates)}")
    return 0


def _render_list_groups(groups, raw: bool, json_output: bool = False, tree=None) -> int:
    items = []
    iterable = groups.values() if isinstance(groups, dict) else groups
    for group in iterable:
        name = group.name if isinstance(group, ProxyGroup) else str(group.get("name", ""))
        group_type = str(_group_value(group, "type", "")).lower()
        if group_type in {"selector", "select", "fallback", "url-test", "load-balance"}:
            normalized_type = "select" if group_type == "selector" else group_type
            items.append((name, normalized_type, normalize_name(_group_value(group, "now", "-"))))

    if json_output:
        emit_json(
            "list-groups",
            {
                "count": len(items),
                "groups": [
                    {"name": name, "type": group_type, "current": current}
                    for name, group_type, current in items
                ],
            },
        )
        return 0
    if raw:
        for name, group_type, _ in items:
            print(f"{name}\t{group_type}")
        return 0

    if tree:
        return _render_switch_entries(tree, groups, heading="用途入口")

    _print_section("摘要")
    print(f"总组数: {len(items)}")
    print(f"可切换组数: {len(items)}")
    print()
    _print_section("列表")
    name_w = max((_display_width(name) for name, _, _ in items), default=4)
    type_w = max((_display_width(group_type) for _, group_type, _ in items), default=4)
    name_w = max(name_w, _display_width("组名"))
    type_w = max(type_w, _display_width("类型"))
    print(f"{_pad_right('组名', name_w)}  {_pad_right('类型', type_w)}  当前选择")
    for name, group_type, current in items:
        print(f"{_pad_right(name, name_w)}  {_pad_right(group_type, type_w)}  {current}")
    return 0


def _render_switch_entries(tree, groups, *, heading: str) -> int:
    groups_by_name = groups if isinstance(groups, dict) else {group.name: group for group in groups}
    _print_section("摘要")
    print(f"{heading}: {len(tree)}")
    print()
    _print_section("列表")
    name_w = max((_display_width(entry.display) for entry in tree), default=4)
    key_w = max((_display_width(entry.key) for entry in tree), default=4)
    name_w = max(name_w, _display_width("名称"))
    key_w = max(key_w, _display_width("内部组"))
    print(f"{_pad_right('名称', name_w)}  {_pad_right('内部组', key_w)}  当前选择")
    for entry in tree:
        group = groups_by_name.get(entry.key)
        current = normalize_name(_group_value(group, "now", "-")) if group else "-"
        print(f"{_pad_right(entry.display, name_w)}  {_pad_right(entry.key, key_w)}  {_accent(current)}")
    return 0


def _render_list_nodes(groups: dict, group_name: str | None, raw: bool, json_output: bool = False, tree=None) -> int:
    if not group_name:
        if raw or json_output:
            raise SystemExit("错误: 缺少必需参数: group")
        if tree:
            _render_switch_entries(tree, groups, heading="用途入口")
            print()
            print("指定入口或组名查看候选项，例如: cproxy list-nodes 'AI 出口'")
            return 0
        raise SystemExit("错误: 缺少必需参数: group")

    choice = lookup_choice(group_name, tree) if tree else None
    resolved = None
    if group_name in groups:
        resolved = group_name
    elif choice is not None:
        if choice.key in groups:
            resolved = choice.key
        elif choice.group_name in groups:
            resolved = choice.group_name
    if resolved is None:
        resolved = resolve_group_query(group_name, groups, tree or ())

    group = groups.get(resolved) if resolved else None
    current = _group_value(group, "now", "") if group else ""
    # 先取出子项列表再判定：把 `choice is not None` 的收窄留在同一处，
    # 否则 use_tree 这个中间布尔量会让收窄失效（mypy 看不到它蕴含非空）
    children = choice.children if choice is not None else []
    use_tree = bool(children and not raw and not json_output and group_name not in groups)
    if use_tree:
        items = [child.key for child in children]
        labels = {child.key: child.display for child in children}
        current_keys = {child.key for child in children if child.current}
    else:
        raw_items = list(_group_value(group, "all", []) or [])
        items = raw_items if raw or json_output else [item for item in raw_items if not is_panel_info_node(item)]
        labels = {item: normalize_name(item) for item in items}
        current_keys = {current} if current else set()

    if json_output:
        emit_json(
            "list-nodes",
            {
                "group": resolved or group_name,
                "current": current,
                "nodes": [
                    {
                        "name": str(item),
                        "display_name": labels.get(item, normalize_name(item)),
                        "current": item == current or item in current_keys,
                    }
                    for item in items
                ],
            },
        )
        return 0
    if raw:
        for item in items:
            prefix = "* " if item == current or item in current_keys else "  "
            print(f"{prefix}{item}")
        return 0

    display_target = choice.display if choice is not None else group_name
    _print_section("摘要")
    print(f"目标组: {display_target}")
    if resolved and resolved != display_target:
        print(f"内部组: {resolved}")
    print(f"当前选择: {_accent(normalize_name(current))}")
    print(f"候选数: {len(items)}")
    print()
    _print_section("列表")
    for item in items:
        is_curr = item == current or item in current_keys
        label = "当前" if is_curr else "候选"
        item_text = labels.get(item, normalize_name(item))
        styled_label = _style(label, ANSI_BOLD, ANSI_GREEN) if is_curr else _style(label, ANSI_DIM)
        styled_item = _accent(item_text) if is_curr else item_text
        print(f"{styled_label}  {styled_item}")
    return 0


def _render_ai_status(groups: dict, raw: bool, json_output: bool = False) -> int:
    # 三个 HTTP 语义探测（含瞬时失败重试）是这条命令的主要耗时；与 status 面板一致，
    # 用 Spinner 兜住，免得终端在探测期间看起来卡死
    with Spinner("正在探测 AI 出口…", delay=0.4, enabled=not (raw or json_output)):
        probe_report = run_ai_probe(default_paths())
        route = _resolve_ai_route(groups)
    probe_ok = all(item.ok for item in probe_report.results)
    probe_status = _probe_summary_status(probe_report)

    if json_output:
        emit_json(
            "ai-status",
            {
                "route": route,
                "probe_status": probe_status,
                "probes": [
                    {"name": item.name, "url": item.url, "ok": item.ok, "detail": item.detail}
                    for item in probe_report.results
                ],
                "groups": {
                    name: {
                        "type": _group_value(group, "type", "-"),
                        "current": _group_value(group, "now", "-"),
                        "alive": _group_value(group, "alive"),
                        "delay": _group_value(group, "delay"),
                    }
                    for name in AI_STATUS_GROUPS
                    if (group := groups.get(name)) is not None
                },
            },
            ok=probe_ok,
            warnings=[item.detail for item in probe_report.results if not item.ok],
            recommended_actions=["cproxy incident"] if not probe_ok else [],
        )
        return 0 if probe_ok else 1

    if raw:
        for name in AI_STATUS_GROUPS:
            group = groups.get(name)
            if group is None:
                print(f"{name}: 缺失")
                continue
            # delay 取**出口节点**的真实延迟：fixed fallback 组自身 history 恒空，
            # 直接读组会打出 None（旧实现即如此）
            _node, exit_delay, _alive = _ai_exit(groups, name)
            print(
                f"{name}: type={_group_value(group, 'type', '-')} now={_group_value(group, 'now', '-')} "
                f"alive={_group_value(group, 'alive', '-')} delay={exit_delay}"
            )
        print(f"AI-MODE: {'auto' if route['auto_mode'] else 'manual'} 池={route['active_group']}")
        print(
            f"AI-EXIT: node={route['active_node']} delay={route['active_delay']} "
            f"alive={route['active_alive']}"
        )
        print(
            f"AI-STANDBY: group={route['standby_group']} node={route['standby_node']} "
            f"delay={route['standby_delay']} alive={route['standby_alive']}"
        )
        print(f"AI-PROBE: {probe_status}")
        for item in probe_report.results:
            print(f"AI-PROBE-ITEM: name={item.name} ok={item.ok} detail={item.detail} url={item.url}")
        return 0

    # ── AI 出口（三条出口合成一张表）────────────────────────────
    exits = [
        ("通用", str(route["active_group"]), route["active_node"], route["active_delay"], route["active_alive"]),
        (
            "备用",
            str(route["standby_group"]),
            route["standby_node"],
            route["standby_delay"],
            route["standby_alive"],
        ),
    ]
    if route["gemini_present"]:
        exits.append(
            ("Gemini", AI_GEMINI_GROUP, route["gemini_node"], route["gemini_delay"], route["gemini_alive"])
        )
    exit_rows: list[list[TableCell]] = [
        [
            (role, ()),
            (group, ()),
            (normalize_name(node), (ANSI_CYAN,)),
            _exit_delay_cell(delay),
            _exit_state_cell(delay, alive),
        ]
        for role, group, node, delay, alive in exits
    ]
    exit_headers = ["角色", "池", "当前节点", "延迟", "状态"]
    exit_aligns = ["l", "l", "l", "r", "l"]
    exit_widths = _table_widths(exit_headers, exit_rows)
    # 模式放在区块标题里（它对三行都一样，不该占一列）。
    # 表格自带边框，区块标题下不再叠一条分隔线。
    _print_section(f"AI 出口（{route['mode_label']}）", rule=False)
    _render_table(exit_headers, exit_rows, aligns=exit_aligns, widths=exit_widths, grid=True)

    # ── 连通性 ────────────────────────────────────────────────
    # 详情列按需出现：全通过时它是空的，白占宽度还多一个无信息的表头。
    # 另外 detail 只在失败时给出——成功时的 HTTP 401/403 是「可达但需鉴权」，
    # 摆在「正常」旁边会被读成故障码。
    show_detail = any(not item.ok and item.detail for item in probe_report.results)
    probe_headers = ["状态", "目标", "地址", *(["详情"] if show_detail else [])]
    probe_rows: list[list[TableCell]] = [
        [
            _status_cell("正常" if item.ok else "失败"),
            (item.name, ()),
            (item.url, (ANSI_DIM,)),
            *([(item.detail if not item.ok else "", ())] if show_detail else []),
        ]
        for item in probe_report.results
    ]
    probe_widths = _table_widths(probe_headers, probe_rows)
    print()
    _print_section(f"连通性（探测 {probe_status}）", rule=False)
    _render_table(probe_headers, probe_rows, widths=probe_widths, grid=True)

    # ── 候选池（只讲拓扑）──────────────────────────────────────
    # 刻意不列延迟：延迟属于「出口健康」，上面三条出口行已按各池组**自身的健康检查**
    # 给出（那正是 mihomo 做故障转移决策的依据）；这里若另按节点实测值列一遍，
    # 同一节点就会在同一屏出现两个口径的数，读起来互相打架。
    present = [name for name in AI_STATUS_GROUPS if groups.get(name) is not None]
    pool_rows: list[list[TableCell]] = []
    for name in present:
        group = groups[name]
        node = resolve_ai_leaf(groups, name)
        candidates = len(_group_value(group, "all", []) or [])
        if name == AI_AUTO_GROUP:
            # 自动池的 `now` 指向区域组（AI-US/AI-SG）而非真实出口——显式标出，
            # 免得「当前 AI-US」被误读成一个节点
            pointer = normalize_name(_group_value(group, "now", "-"))
            kind, target = "自动池", f"{pointer} → {normalize_name(node)}"
        else:
            # AI-US/AI-SG 是自动池的两个区域臂（被 AI-AUTO 引用），其余组直接挂节点
            kind = "区域组" if name in {AI_US_GROUP, AI_SG_GROUP} else "直连池"
            target = normalize_name(node)
        pool_rows.append([(name, ()), (kind, ()), (str(candidates), ()), (target, (ANSI_CYAN,))])
    pool_headers = ["池", "类型", "候选", "当前指向"]
    pool_aligns = ["l", "l", "r", "l"]
    pool_widths = _table_widths(pool_headers, pool_rows)
    print()
    _print_section("候选池", rule=False)
    _render_table(pool_headers, pool_rows, aligns=pool_aligns, widths=pool_widths, grid=True)
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


def _render_status(raw: bool, process_top: int = STATUS_PROCESS_TOP_DEFAULT, json_output: bool = False) -> int:
    paths = default_paths()
    # 慢点集中在这段收集（systemctl 探测 + AI 路由 API），延迟 spinner 只在真慢时出现；
    # 后续的 metrics/流量/订阅信息都是本地读取，且打印开始前 spinner 已退出，不会互擦
    with Spinner("正在收集状态…", delay=0.4, enabled=not (raw or json_output)):
        snapshot = get_status(paths)
        config_state = "已就绪" if snapshot.runtime_ready else "待刷新"
        status_text = "运行中" if snapshot.running else "未运行"
        api_text = "不可访问"
        ai_mode = "-"
        ai_summary = "-"

        gemini_summary = "-"
        try:
            route = _resolve_ai_route(QueryService(paths).get_ai_status_groups())
            api_text = "可访问"
            ai_mode = str(route["mode_label"])
            ai_summary = str(route["active_summary"])
            if route.get("gemini_present"):
                gemini_summary = str(route["gemini_summary"])
        except APIUnavailableError:
            pass

    recommended_actions: list[str] = []
    warnings: list[str] = []
    if not snapshot.runtime_ready:
        warnings.append("运行配置尚未生成")
        recommended_actions.append("cproxy render")
    if snapshot.runtime_stale:
        warnings.append("运行实例尚未应用磁盘上的最新运行配置")
        recommended_actions.append("cproxy restart")
    if not snapshot.running:
        warnings.append("cproxy 代理进程未运行")
        recommended_actions.append("cproxy start")
    if api_text != "可访问":
        warnings.append("Mihomo API 不可访问")
        recommended_actions.append("cproxy logs --lines 100")

    if json_output:
        metrics = collect_runtime_metrics(paths, snapshot.pid)
        traffic = _today_traffic_summary(paths)
        subscriptions = [
            {"label": label, **usage.to_dict()}
            for label, usage in display_entries(paths)
        ]
        emit_json(
            "status",
            {
                "version": __version__,
                "source_config": snapshot.source_config,
                "runtime_config": snapshot.runtime_config,
                "controller": snapshot.controller,
                "port": snapshot.port,
                "runtime_ready": snapshot.runtime_ready,
                "runtime_stale": snapshot.runtime_stale,
                "running": snapshot.running,
                "pid": snapshot.pid,
                "api_available": api_text == "可访问",
                "ai_mode": ai_mode,
                "ai_route": ai_summary,
                "ai_gemini_route": gemini_summary,
                "metrics": {
                    "connections": _connection_count(paths) if api_text == "可访问" else None,
                    "uptime_seconds": metrics.uptime_seconds,
                    "memory_bytes": metrics.memory_bytes,
                    "log_bytes": metrics.log_bytes,
                },
                "traffic_today": traffic,
                "subscriptions": subscriptions,
            },
            ok=not warnings,
            warnings=warnings,
            recommended_actions=recommended_actions,
        )
        return 0

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
    summary_rows = [
        ("状态", _status_label(status_text)),
        ("API", _status_label(api_text)),
        ("运行配置", _status_label(config_state)),
        ("AI 路由", ai_mode),
        ("通用出口", _accent(ai_summary)),
    ]
    if gemini_summary != "-":
        summary_rows.append(("Gemini", _accent(gemini_summary)))
    _render_kv(summary_rows)

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
        print("请先执行 cproxy doctor 确认实例归属，再按建议启动 cproxy。")
    if not snapshot.runtime_ready:
        print("如需使用用户级 cproxy，请先运行 cproxy render 生成运行配置。")
    if snapshot.runtime_stale:
        if not (not snapshot.running and api_text == "可访问"):
            print()
            _print_section("建议操作")
        print("执行 cproxy restart 应用最新运行配置。")
    elif api_text != "可访问":
        print()
        _print_section("建议操作")
        print("执行 cproxy logs --lines 100 查看 API 不可访问原因。")
    return 0


def _render_group_check(report: GroupCheckReport, raw: bool, json_output: bool = False) -> int:
    if json_output:
        ok = all(item.ok for item in report.results)
        emit_json(
            "test-group",
            {
                "group": report.group_name,
                "results": [
                    {"name": item.name, "ok": item.ok, "delay": item.delay}
                    for item in report.results
                ],
            },
            ok=ok,
            warnings=[item.name for item in report.results if not item.ok],
            recommended_actions=[f"cproxy test-group {report.group_name}"] if not ok else [],
        )
        return 0 if ok else 1
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
    name_w = max((_display_width(item.name) for item in report.results), default=6)
    for item in report.results:
        padded_name = _pad_right(item.name, name_w)
        if item.ok and item.delay is not None:
            print(f"{_status_label('正常')}  {padded_name}  {item.delay}ms")
        else:
            print(f"{_status_label('失败')}  {padded_name}  -")
    return 0 if len(ok_items) == len(report.results) else 1


def _render_connectivity_report(report: ConnectivityReport, json_output: bool = False) -> int:
    passed = sum(1 for item in report.results if item.ok)
    if json_output:
        ok = passed == len(report.results)
        emit_json(
            "test",
            {
                "exit_ip": report.exit_ip,
                "results": [
                    {"name": item.name, "ok": item.ok, "detail": item.detail}
                    for item in report.results
                ],
            },
            ok=ok,
            warnings=[f"{item.name}: {item.detail}" for item in report.results if not item.ok],
            recommended_actions=["cproxy doctor", "cproxy logs --lines 100"] if not ok else [],
        )
        return 0 if ok else 1
    _print_section("摘要")
    print("目标: 代理连通性")
    print(f"可用: {passed}/{len(report.results)}")
    print(f"出口 IP: {report.exit_ip or '-'}")
    print()
    _print_section("结果")
    name_w = max((_display_width(item.name) for item in report.results), default=8)
    for item in report.results:
        padded_name = _pad_right(item.name, name_w)
        if item.ok:
            print(f"{_status_label('正常')}  {padded_name}  {item.detail}")
        else:
            print(f"{_status_label('失败')}  {padded_name}  {item.detail}")
    if passed != len(report.results):
        print()
        _print_section("建议操作")
        print("执行 cproxy doctor 查看本地配置与进程状态。")
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


def _run_bootstrap(subscription_url: str | None = None) -> int:
    paths = default_paths()
    config_path = init_user_layout(paths)
    migrated_from: Path | None = None
    initialized_from_subscription = False

    if is_placeholder_config(paths):
        migrated_path = auto_migrate_from_default_legacy(paths)
        if migrated_path is not None:
            migrated_from = Path(os.environ.get("CPROXY_LEGACY_ROOT", "/root/clash_proxy")) / "config.yaml"
            config_path = migrated_path
        else:
            if not subscription_url and sys.stdin.isatty() and not _truthy(os.environ.get("CPROXY_NONINTERACTIVE", "")):
                print("未找到可迁移配置。可输入订阅 URL 初始化；直接回车则退出。", file=sys.stderr)
                subscription_url = input("订阅 URL: ").strip()
            if not subscription_url:
                legacy_root = Path(os.environ.get("CPROXY_LEGACY_ROOT", "/root/clash_proxy"))
                legacy_config = legacy_root / "config.yaml"
                raise RuntimeError(
                    "错误: 当前配置为空，且未找到可迁移配置: "
                    f"{legacy_config}\n"
                    "下一步: cproxy bootstrap --subscription-url '<订阅地址>'\n"
                    f"或编辑 {config_path} 后重新执行 cproxy bootstrap"
                )
            with Spinner("正在下载订阅…"):
                config_path = update_source_from_subscription(paths, subscription_url)
            initialized_from_subscription = True

    with Spinner("正在渲染运行配置…") as bootstrap_spinner:
        runtime_path = render_runtime(paths)
        bootstrap_spinner.update("正在启动代理进程…")
        pid = start_process(paths)
        process = ProcessBackend(paths)
        if process.is_runtime_stale(runtime_path):
            APIBackend(paths).reload_config(str(runtime_path))
            process.mark_runtime_applied(runtime_path)
        bootstrap_spinner.update("正在校验运行状态…")
        snapshot = get_status(paths)
    if not snapshot.running:
        raise RuntimeError("错误: 代理启动后状态异常，请执行 cproxy logs --lines 100 排查")

    geodata_check = check_country_mmdb(paths)

    print("一键部署完成")
    print(f"配置文件: {config_path}")
    if migrated_from is not None:
        print(f"已自动迁移旧配置: {migrated_from}")
    if initialized_from_subscription:
        print("已从订阅初始化配置（订阅地址已隐藏）")
    print(f"运行配置: {runtime_path}")
    print(f"代理进程: 运行中 (PID: {pid})")
    if geodata_check.ok:
        print(f"GeoIP: {geodata_check.detail}")
    else:
        print(f"GeoIP: {geodata_check.detail}")
    return 0


def _render_doctor(json_output: bool = False) -> int:
    report = run_doctor(default_paths())
    if json_output:
        emit_json(
            "doctor",
            {"checks": report["checks"]},
            ok=bool(report["ok"]),
            warnings=[item["detail"] for item in report["checks"] if item["status"] == "失败"],
            recommended_actions=report["recommended_actions"],
        )
        return 0 if report["ok"] else 1

    checks = report["checks"]
    failed = [item for item in checks if item["status"] == "失败"]
    passed = [item for item in checks if item["status"] != "失败"]
    summary_status = _status_label("正常" if not failed else "失败")

    # 摘要先于体检结果：让一眼就能看出「6 项 / 1 失败」再决定要不要往下读，
    # 否则直接进 体检结果 时，本应跳过的健康项会和失败项混在一起，反而被忽略。
    _print_section("摘要")
    _render_kv_colon(
        [
            ("状态", summary_status),
            ("总项数", str(len(checks))),
            ("通过", str(len(passed))),
            ("失败", str(len(failed))),
        ]
    )
    print()
    _print_section("体检结果")
    name_w = max((_display_width(item["name"]) for item in checks), default=8)
    for item in report["checks"]:
        padded_name = _pad_right(item["name"], name_w)
        print(f"{_status_label(item['status'])}  {padded_name}  {item['detail']}")
    if report["recommended_actions"]:
        print()
        _print_section("建议操作")
        for action in report["recommended_actions"]:
            print(f"- {_accent(action)}")
    return 0 if report["ok"] else 1


def _render_security_check(strict: bool) -> int:
    report = validate_controller_security(default_paths())
    errors = [i for i in report.issues if i.severity == "error"]
    warnings = [i for i in report.issues if i.severity == "warning"]

    _print_section("安全体检摘要")
    if not report.issues:
        status_label = _status_label("正常")
        print(f"总体状态: {status_label} (安全校验通过)")
    elif errors:
        status_label = _status_label("失败")
        print(f"总体状态: {status_label} (发现高危配置风险)")
    else:
        status_label = _status_label("部分异常")
        print(f"总体状态: {status_label} (发现安全改进项)")

    print(f"高危风险: {len(errors)}")
    print(f"警告提示: {len(warnings)}")
    print()

    _print_section("检查项详情")
    if not report.issues:
        print("OK: security configuration passed (外部控制接口安全配置达标)")
    else:
        for issue in report.issues:
            sev_label = _status_label("失败" if issue.severity == "error" else "部分异常")
            print(f"{sev_label}  {issue.severity.upper()}: {issue.code}: {issue.detail}")

    if errors or warnings:
        print()
        _print_section("修复建议")
        for issue in report.issues:
            if issue.code == "missing-controller-tls":
                print("- [建议] 可在 config.yaml 开启 external-controller-tls 并配置证书保护控制端口")
            elif issue.code == "non-loopback-controller":
                print("- [修复] 请修改 config.yaml 中的 external-controller，绑定至 127.0.0.1 避免公网暴露")
            else:
                print(f"- [{issue.code}] 请检查对应安全项配置")

    if any(issue.severity == "error" or (strict and issue.severity == "warning") for issue in report.issues):
        return 1
    return 0


def _render_snapshots(paths, raw: bool, as_json: bool = False) -> int:
    entries = list_snapshots(paths)
    if as_json:
        # 逐个解析时间：既让 `created_at` 的 Optional 收窄成立，也避免同一项被解析三次
        payload = []
        for idx, entry in enumerate(entries):
            created = parse_snapshot_time(entry)
            payload.append(
                {
                    "name": entry.name,
                    "kind": snapshot_kind(entry),
                    "size": entry.stat().st_size,
                    "relative_time": format_relative_time(created),
                    "tier": snapshot_tier_label(entry, idx),
                    "created_at": created.isoformat() if created else None,
                }
            )
        emit_json("snapshots", {"snapshots": payload, "count": len(entries), "directory": str(snapshots_dir(paths))})
        return 0
    if raw:
        for entry in entries:
            print(entry.name)
        return 0

    _print_section("摘要")
    _render_kv_colon(
        [
            ("快照数", str(len(entries))),
            ("快照目录", str(snapshots_dir(paths))),
        ]
    )
    if entries:
        print()
        _print_section("列表")
        rows = []
        for idx, entry in enumerate(entries):
            kind = snapshot_kind(entry)
            name = entry.name
            rel_time = format_relative_time(parse_snapshot_time(entry))
            tier = snapshot_tier_label(entry, idx)
            size = format_bytes(entry.stat().st_size)
            rows.append((kind, name, rel_time, f"[{tier}]", size))

        kind_width = max((_display_width(r[0]) for r in rows), default=4)
        name_width = max((_display_width(r[1]) for r in rows), default=4)
        time_width = max((_display_width(r[2]) for r in rows), default=6)
        tier_width = max((_display_width(r[3]) for r in rows), default=6)
        size_width = max((_display_width(r[4]) for r in rows), default=6)

        kind_header = _pad_right("类型", kind_width)
        name_header = _pad_right("快照文件名", name_width)
        time_header = _pad_right("生成时间", time_width)
        tier_header = _pad_right("保留阶梯", tier_width)
        size_header = _pad_left("大小", size_width)
        print(f"{kind_header}  {name_header}  {time_header}  {tier_header}  {size_header}")

        for kind, name, rel_time, tier, size in rows:
            p_kind = _pad_right(kind, kind_width)
            p_name = _pad_right(name, name_width)
            p_time = _pad_right(rel_time, time_width)
            p_tier = _pad_right(tier, tier_width)
            p_size = _pad_left(size, size_width)
            print(f"{p_kind}  {p_name}  {p_time}  {p_tier}  {p_size}")
    return 0


def _render_colored_diff(diff_lines: Sequence[str], max_lines: int = 40) -> None:
    count = 0
    for line in diff_lines:
        count += 1
        if count > max_lines:
            print(f"{ANSI_DIM}… (余下 {len(diff_lines) - max_lines} 行差异已折叠){ANSI_RESET}")
            break
        text = line.rstrip("\r\n")
        if text.startswith("---") or text.startswith("+++"):
            print(f"{ANSI_BOLD}{text}{ANSI_RESET}")
        elif text.startswith("@@"):
            print(f"\033[36m{text}{ANSI_RESET}")
        elif text.startswith("+"):
            print(f"\033[32m{text}{ANSI_RESET}")
        elif text.startswith("-"):
            print(f"\033[31m{text}{ANSI_RESET}")
        else:
            print(text)


def _render_snapshot_diff(paths, target_name: str | None = None, raw: bool = False, as_json: bool = False) -> int:
    if target_name:
        candidate = snapshots_dir(paths) / Path(target_name).name
        if not candidate.is_file():
            raise RuntimeError(f"错误: 快照不存在: {target_name}")
        snapshot = candidate
    else:
        snapshots = list_snapshots(paths)
        if not snapshots:
            raise RuntimeError("错误: 没有可供比对的快照")
        snapshot = snapshots[0]

    diff_lines = snapshot_diff(paths, snapshot)
    if as_json:
        emit_json(
            "snapshots_diff",
            {
                "snapshot": snapshot.name,
                "kind": snapshot_kind(snapshot),
                "diff": diff_lines,
                "has_diff": bool(diff_lines),
            },
        )
        return 0
    if raw:
        for line in diff_lines:
            print(line, end="")
        return 0

    _print_section(f"配置差异对比 ({snapshot.name})")
    if not diff_lines:
        print("快照内容与当前对应配置文件完全一致（无差异）。")
    else:
        _render_colored_diff(diff_lines)
    return 0


def _run_compact_snapshots(
    paths,
    kind: str | None = None,
    dry_run: bool = False,
    raw: bool = False,
    as_json: bool = False,
) -> int:
    report = compact_snapshots(paths, kind=kind, dry_run=dry_run)
    if as_json:
        emit_json(
            "snapshots_compact",
            {
                "dry_run": report.dry_run,
                "total_before": report.total_before,
                "total_after": report.total_after,
                "kept_count": len(report.kept),
                "pruned_count": len(report.pruned),
                "bytes_reclaimed": report.bytes_reclaimed,
                "kept": report.kept,
                "pruned": report.pruned,
            },
        )
        return 0
    if raw:
        for name in report.pruned:
            print(name)
        return 0

    mode_label = "演练（Dry Run）" if report.dry_run else "已完成"
    _print_section(f"快照压缩 - {mode_label}")
    _render_kv_colon(
        [
            ("压缩前总数", str(report.total_before)),
            ("保留快照数", str(report.total_after)),
            ("清理快照数", str(len(report.pruned))),
            ("释放空间", f"{report.bytes_reclaimed} 字节"),
            ("模式", "演练（未实际删除文件）" if report.dry_run else "已实际删除"),
        ]
    )
    if report.pruned:
        print()
        _print_section("已清理快照" if not report.dry_run else "拟清理快照")
        for name in report.pruned:
            print(f"- {name}")
    return 0


def _run_rollback(paths, name: str | None) -> int:
    from .interactive import interactive_supported, select_one

    if name:
        candidate = snapshots_dir(paths) / Path(name).name
        if not candidate.is_file():
            raise RuntimeError(f"错误: 快照不存在: {name}")
        snapshot = candidate
    else:
        if interactive_supported():
            all_snapshots = list_snapshots(paths)
            if not all_snapshots:
                raise RuntimeError("错误: 没有可用的快照")

            # 构造带相对时间与阶梯标签的交互菜单
            items = [s.name for s in all_snapshots]
            annotations = {
                s.name: f"{format_relative_time(parse_snapshot_time(s))} [{snapshot_tier_label(s, i)}]"
                for i, s in enumerate(all_snapshots)
            }
            chosen = select_one("选择要回滚的快照（Esc / q 取消）", items, annotations=annotations)
            if chosen is None:
                print("已取消回滚。")
                return 0
            snapshot = snapshots_dir(paths) / chosen

            # 展示差异预览
            diff_lines = snapshot_diff(paths, snapshot)
            print()
            _print_section("差异预览")
            if diff_lines:
                _render_colored_diff(diff_lines, max_lines=30)
            else:
                print("提示: 快照内容与当前目标文件完全一致")

            try:
                reply = input("\n是否确认回滚到此快照？[Y/n]: ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                print("\n已取消回滚。")
                return 0
            if reply not in ("", "y", "yes"):
                print("已取消回滚。")
                return 0
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
            with Spinner("正在重启代理以应用回滚…", delay=0.3):
                restart_process(paths)
            print("代理已重启以应用回滚")
        else:
            print("提示: 代理未运行，配置将在下次启动时生效")
    else:
        print("提示: 原始配置已恢复，执行 cproxy render 使其生效")
    return 0


def _render_refresh(report: RefreshReport, raw: bool, json_output: bool = False) -> int:
    if json_output:
        warnings = [
            f"附加订阅 {item.name}: {item.detail or item.status}"
            for item in report.extra_subscriptions
            if item.status == "失败"
        ]
        if report.subscription == "失败":
            warnings.insert(0, report.subscription_detail or "主订阅更新失败")
        emit_json(
            "refresh",
            report,
            ok=not warnings,
            warnings=warnings,
            recommended_actions=["cproxy doctor"] if warnings else [],
        )
        return 0 if not warnings else 1
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
    # 旧版用裸 `f"订阅更新: ..."` 拼接，长 detail 时会撞列；
    # 现在按最长 key 对齐，多字段也不会错位。
    rows: list[tuple[str, str]] = []
    subscription_label = report.subscription
    if report.subscription_detail:
        subscription_label = f"{subscription_label}  {report.subscription_detail}"
    rows.append(("订阅更新", subscription_label))
    for extra in report.extra_subscriptions:
        extra_label = extra.status
        if extra.detail:
            extra_label = f"{extra_label}  {extra.detail}"
        rows.append((f"附加订阅 {extra.name}", extra_label))
    # runtime_path 是 Path | None；空值时整行略去（_render_kv_colon 本就会跳过空值行）
    if report.runtime_path:
        rows.append(("运行配置", str(report.runtime_path)))
    if report.restarted:
        rows.append(("代理", "已重启应用新配置（连接已中断）"))
    elif report.hot_reloaded:
        rows.append(("代理", "已热重载应用新配置（连接不中断）"))
    elif report.was_running:
        rows.append(("代理", "运行中"))
    else:
        rows.append(("代理", "未运行（跳过重启与探测）"))
    _render_kv_colon(rows)
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


def _format_history_time(ts_val: Any) -> str:
    try:
        ts = float(ts_val)
        dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        local_dt = dt.astimezone()
        local_str = local_dt.strftime("%Y-%m-%d %H:%M")
        rel = format_relative_time(dt)
        if any(keyword in rel for keyword in ("前", "昨天")):
            return f"{local_str} ({rel})"
        return local_str
    except Exception:
        return str(ts_val)


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
        time_label = _format_history_time(item.get("ts", "-"))
        profile = item.get("profile", "-")
        strategy = item.get("strategy", "-")
        curr = normalize_name(item.get("current", "-"))
        best = normalize_name(item.get("best", "-"))
        switched = item.get("switched") is True
        stable = item.get("stable") is True
        reason = item.get("skip_reason") or item.get("reason", "-")

        if switched:
            tag = _style("[已自动切换]", ANSI_BOLD, ANSI_GREEN)
        elif stable:
            tag = _style("[稳定保持]", ANSI_CYAN)
        else:
            tag = _style("[未达标]", ANSI_YELLOW)

        print(f"• {time_label}  {profile} ({strategy})  {tag}")
        if best and best != "-" and best != "None":
            print(f"  当前: {curr}  ->  推荐: {_accent(best)}")
        else:
            print(f"  当前: {curr}")
        print(f"  原因: {reason}")
        print()
    return 0


def _render_ai_connections(paths, raw: bool) -> int:
    with Spinner("正在连接 Mihomo API…", delay=0.4, enabled=not raw):
        connections = get_ai_connections(paths)
    if raw:
        for conn in connections:
            print(f"AI_CONNECTION\t{conn.host}\tcount={conn.count}\troute={conn.route}")
        return 0

    total_conns = sum(conn.count for conn in connections)
    _print_section("摘要")
    print(f"活动目标数: {len(connections)}")
    print(f"活动连接数: {_accent(str(total_conns))}")
    print()

    _print_section("AI 连接")
    if not connections:
        print("未发现 ChatGPT/Claude/Gemini/Antigravity/GitHub 相关活动连接")
        return 0

    host_width = max((_display_width(conn.host) for conn in connections), default=12)
    host_width = max(host_width, _display_width("目标域名"))
    print(f"{_pad_right('目标域名', host_width)}  {'连接数':<10}  出口链路")
    for conn in connections:
        active_label = f"{conn.count} active"
        styled_active = _style(f"{active_label:<10}", ANSI_GREEN)
        print(f"{_pad_right(conn.host, host_width)}  {styled_active}  {conn.route}")
    return 0


def _render_incident(paths, profile: str) -> int:
    with Spinner("正在收集故障排查信息（含多轮探测）…"):
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


def _render_kv_colon(rows: list[tuple[str, str]]) -> None:
    """按东亚宽度对齐输出「key: value」行；空值行自动跳过。

    与 :func:`_render_kv` 的区别：分隔符是 ``": "`` 而非多空格，
    适合「目标组: AI-AUTO」「订阅更新: 已更新」这类人类可读的冒号场景，
    也兼容外部断言（测试仍可命中 ``"AI 路由:"`` 等冒号子串）。

    标签按 display width 对齐（CJK 字符 2 列宽），最长标签后**直接**接
    ``": value"``，不会在冒号前补额外空白——保留 ``"目标组: AI-AUTO"``
    这种紧凑格式比强行做列对齐更重要（后者会让冒号漂在奇怪的位置）。
    """
    visible = [(label, value) for label, value in rows if value]
    if not visible:
        return
    width = max(_display_width(label) for label, _ in visible)
    for label, value in visible:
        print(f"{_pad_right(label, width)}: {value}")


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


def _traffic_row_payload(row) -> dict:
    payload = {"label": row.label, "download": row.download, "upload": row.upload}
    if hasattr(row, "proxy_download"):
        payload["proxy_download"] = row.proxy_download
        payload["proxy_upload"] = row.proxy_upload
    return payload


def _render_traffic(
    paths,
    *,
    action: str,
    days: int,
    by: str | None,
    top: int,
    raw: bool,
    json_output: bool = False,
) -> int:
    service = TrafficService(paths)
    if action == "collect":
        result = service.collect()
        if json_output:
            emit_json("traffic", {"action": action, "result": result})
            return 0
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
        if json_output:
            processes = service.process_breakdown(days=days, top=top)
            emit_json(
                "traffic",
                {
                    "action": action,
                    "since": audit["since"],
                    "until": audit["until"],
                    "proxy_download": audit["proxy_download"],
                    "proxy_upload": audit["proxy_upload"],
                    "direct_download": audit["direct_download"],
                    "direct_upload": audit["direct_upload"],
                    "hosts": [_traffic_row_payload(row) for row in audit["rows"]],
                    "processes": [_traffic_row_payload(row) for row in processes.rows],
                    "process_total": processes.total,
                },
            )
            return 0
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
    if json_output:
        emit_json(
            "traffic",
            {
                "action": action,
                "since": report["since"],
                "until": report["until"],
                "total_download": report["total_download"],
                "total_upload": report["total_upload"],
                "daily": [_traffic_row_payload(row) for row in report["daily"]],
                "dimensions": {
                    name: [_traffic_row_payload(row) for row in rows]
                    for name, rows in report["dimensions"].items()
                },
            },
        )
        return 0
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
    warnings: list[str] = []
    with Spinner("正在检测出口 IP 纯净度…", enabled=not raw):
        report = service.check(ip=ip, group=group, node=node, timeout=timeout, warn=warnings.append)
    # 恢复失败等警告延迟到 Spinner 退出后再打，否则会被动画帧整行擦掉
    for warning in warnings:
        print(warning, file=sys.stderr)

    if raw:
        print(
            f"IP_CHECK\tip={report.ip}\tcountry={report.country}\tcity={report.city}"
            f"\tisp={report.isp}\tasn={report.asn}\tip_type={report.ip_type}"
            f"\tnative={report.native_type}\trisk={report.risk if report.risk is not None else '-'}"
            f"\tverdict={report.verdict}\tsignals={','.join(report.signals) or '-'}"
            f"\tblocklist={','.join(report.blocklist_listed) or 'clean'}"
            f"\tdatacenter={report.datacenter or '-'}"
        )
        for service_status in report.services:
            print(f"IP_SERVICE\t{service_status.key}\t{service_status.status}")
        for source_score in report.sources:
            print(f"IP_SOURCE\t{source_score.source}\trisk={source_score.risk}\tweight={source_score.weight}")
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
        for service_status in report.ai_services:
            print(f"  {service_status.key:<10} {service_status.label}")
    if report.services and not report.ai_services:
        print()
        print("服务快照:")
        for service_status in report.services:
            print(f"  {service_status.key:<10} {service_status.label}")

    if report.sources:
        print()
        print("多源评分:")
        for source_score in report.sources:
            print(f"  {source_score.source:<14} risk={source_score.risk:<3} weight={source_score.weight}")

    if report.switched:
        print()
        print(
            f"检测期间临时切换 {report.switch_group} -> {normalize_name(node or '')}"
            f"，已恢复为 {normalize_name(report.previous_selection)}"
        )
    return 0
