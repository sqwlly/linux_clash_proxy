"""用户面向的 switch 树：用途入口 → 地区/自动 → 节点。

Mihomo 内部组名（`AI-MANUAL`、`CyberGuard`）保持不变；
CLI `cproxy switch`、TUI 代理页、人读的 `list-groups` / `list-nodes` 共用这棵树。
`--raw` / `--json` 仍输出 Mihomo 原始组，供脚本使用。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..backend.models import ProxyGroup
from ..backend.runtime import (
    AI_AUTO_GROUP,
    AI_MANUAL_GROUP,
    AI_REGION_JP,
    AI_REGION_SG,
    AI_REGION_US,
    AI_SG_GROUP,
    AI_US_GROUP,
)
from .nodelist import (
    SUBSCRIPTION_REGION_ORDER,
    SWITCH_REGION_LABELS,
    is_panel_info_node,
    match_region,
)
from .probe import resolve_current_leaf

SELECTABLE_TYPES = frozenset({"selector", "select"})
AUTO_TYPES = frozenset({"url-test", "urltest", "fallback", "load-balance", "loadbalance"})
AI_INTERNAL_POOLS = frozenset({AI_US_GROUP, AI_SG_GROUP})
AI_REGION_GROUPS = frozenset({AI_REGION_JP, AI_REGION_US, AI_REGION_SG})
AI_REGION_DISPLAY = {
    AI_REGION_JP: "日本",
    AI_REGION_US: "美国",
    AI_REGION_SG: "新加坡",
}
AUTO_NAMES = frozenset({AI_AUTO_GROUP, "自动选择", "Auto", "故障转移"})
AUTO_DISPLAY = {
    AI_AUTO_GROUP: "自动",
    "自动选择": "自动",
    "Auto": "自动",
    "故障转移": "故障转移",
}
ENTRY_DEFAULT = "默认流量"
ENTRY_AI = "AI 出口"
ENTRY_GLOBAL = "全局"
DIM_STYLE = "\033[2m"
REGION_LABEL_VALUES = frozenset(SWITCH_REGION_LABELS.values())


@dataclass(frozen=True)
class SwitchChoice:
    """树上的一项。`key` 是 Mihomo 名或虚拟地区桶标签；`display` 给人看。"""

    key: str
    display: str
    annotation: str = ""
    annotation_style: str = DIM_STYLE
    drillable: bool = False
    current: bool = False
    path: tuple[tuple[str, str], ...] = ()
    children: tuple["SwitchChoice", ...] = field(default_factory=tuple)
    group_name: str = ""


def extra_subscription_names(config: dict | None) -> list[str]:
    names: list[str] = []
    if not isinstance(config, dict):
        return names
    for item in config.get("subscriptions") or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if name:
            names.append(name)
    return names


def delay_label(delay: int | None) -> str:
    """选择器里的延迟标注。

    `0` 是 mihomo 的**测速失败**标记，不是「极快」——直接显示 `0 ms` 会让人
    挑中实际不可用的节点。取不到记录则标 `-`，与「失败」区分开。
    """
    if delay is None:
        return "-"
    return "超时" if delay <= 0 else f"{delay} ms"


def delay_style(delay: int | None) -> str:
    """延迟标注的 ANSI 着色：快绿、中黄、慢/超时红、无记录暗灰。"""
    if delay is None:
        return DIM_STYLE
    if delay <= 0:
        return "\033[31m"
    if delay <= 200:
        return "\033[32m"
    if delay <= 500:
        return "\033[33m"
    return "\033[31m"


def resolve_delay(name: str, groups: dict[str, ProxyGroup], delays: dict[str, int], depth: int = 0) -> int | None:
    """取某个候选的延迟。

    候选可能是节点，也可能是组——selector 自身通常不测速，沿当前出口往下钻。
    """
    if name in delays:
        return delays[name]
    group = groups.get(name)
    if group is None or depth >= 8:
        return None
    current = getattr(group, "current", None)
    if not current or current == name:
        return None
    return resolve_delay(str(current), groups, delays, depth + 1)


def display_entry(name: str, match_group: str | None) -> str:
    if match_group and name == match_group:
        return ENTRY_DEFAULT
    if name == AI_MANUAL_GROUP:
        return ENTRY_AI
    if name == "GLOBAL":
        return ENTRY_GLOBAL
    return name


def display_child(name: str, extra_names: list[str] | tuple[str, ...] = ()) -> str:
    if name in AI_REGION_DISPLAY:
        return AI_REGION_DISPLAY[name]
    if name in AUTO_DISPLAY:
        return AUTO_DISPLAY[name]
    if name in SWITCH_REGION_LABELS:
        return SWITCH_REGION_LABELS[name]
    if name in REGION_LABEL_VALUES:
        return name
    for extra in extra_names:
        prefix = f"{extra}-"
        if name.startswith(prefix):
            code = name[len(prefix) :]
            return SWITCH_REGION_LABELS.get(code, code)
    return name


def level_title(children: tuple[SwitchChoice, ...] | list[SwitchChoice]) -> str:
    if any(child.drillable or child.annotation == "自动" for child in children):
        return "选择地区"
    return "选择节点"


def build_switch_tree(
    groups: dict[str, ProxyGroup],
    *,
    match_group: str | None = None,
    extra_names: list[str] | None = None,
    delays: dict[str, int] | None = None,
) -> tuple[SwitchChoice, ...]:
    """编出第一层用途入口，子节点已填好地区/自动与节点。"""
    extra = list(extra_names or [])
    delay_map = delays or {}
    selectable = {name for name, group in groups.items() if _is_selectable(group)}
    skip = _entry_skip_names(extra)
    ordered: list[str] = []
    seen: set[str] = set()

    def add(name: str | None) -> None:
        if not name or name in seen or name not in selectable or name in skip:
            return
        ordered.append(name)
        seen.add(name)

    add(match_group)
    add(AI_MANUAL_GROUP)
    for name in extra:
        add(name)
    add("GLOBAL")
    for name in groups:
        add(name)

    entries: list[SwitchChoice] = []
    for name in ordered:
        group = groups[name]
        children = _children_of(name, groups, extra, delay_map)
        leaf = resolve_current_leaf(groups, name) or (group.current if group.current != "-" else "")
        display = display_entry(name, match_group)
        role = _entry_role(name, match_group)
        if role and leaf:
            annotation = f"{leaf} · {role}"
        else:
            annotation = role or leaf or ""
        entries.append(
            SwitchChoice(
                key=name,
                display=display,
                annotation=annotation,
                annotation_style=DIM_STYLE,
                drillable=True,
                current=(bool(match_group) and name == match_group),
                path=(),
                children=children,
                group_name=name,
            )
        )
    return tuple(entries)


def iter_choices(entries: tuple[SwitchChoice, ...] | list[SwitchChoice]):
    """深度优先遍历整棵树。"""
    for entry in entries:
        yield entry
        yield from iter_choices(entry.children)


def lookup_choice(query: str, entries: tuple[SwitchChoice, ...]) -> SwitchChoice | None:
    """按显示名或内部名找树上的一项。先顶层，再往下。"""
    needle = query.strip()
    if not needle:
        return None
    for entry in entries:
        if entry.display == needle or entry.key == needle:
            return entry
    for entry in entries:
        found = lookup_choice(needle, entry.children)
        if found is not None:
            return found
    return None


def resolve_group_query(
    query: str,
    groups: dict[str, ProxyGroup],
    entries: tuple[SwitchChoice, ...],
) -> str:
    """把用户输入的显示名或内部名收成 Mihomo 组名。找不到就 SystemExit。"""
    needle = query.strip()
    if needle in groups:
        return needle
    choice = lookup_choice(needle, entries)
    if choice is None:
        hints = "、".join(f"{entry.display} ({entry.key})" for entry in entries[:6]) or "cproxy list-groups"
        raise SystemExit(f"错误: 未找到代理组: {query}\n提示: 试试 {hints}")
    if choice.key in groups:
        return choice.key
    if choice.group_name in groups:
        return choice.group_name
    raise SystemExit(f"错误: 未找到代理组: {query}")


def _entry_role(name: str, match_group: str | None) -> str:
    if match_group and name == match_group:
        return "决定其余流量"
    if name == AI_MANUAL_GROUP:
        return "决定 AI 出口"
    if name == "GLOBAL":
        return "绕过规则"
    return ""


def _entry_skip_names(extra_names: list[str]) -> set[str]:
    skip = set(AI_REGION_GROUPS)
    skip.update(AI_INTERNAL_POOLS)
    skip.update(REGION_LABEL_VALUES)
    skip.update(SWITCH_REGION_LABELS.keys())
    skip.update(AUTO_NAMES)
    for extra in extra_names:
        for region in SUBSCRIPTION_REGION_ORDER:
            skip.add(f"{extra}-{region}")
            skip.add(f"{extra}-{SWITCH_REGION_LABELS.get(region, region)}")
    return skip


def _is_selectable(group: ProxyGroup) -> bool:
    return str(group.type).lower() in SELECTABLE_TYPES


def _is_auto(group: ProxyGroup) -> bool:
    return str(group.type).lower() in AUTO_TYPES


def _is_nested_group(name: str, groups: dict[str, ProxyGroup]) -> bool:
    child = groups.get(name)
    return child is not None and bool(child.candidates)


def _internal_nested_names(nested: list[str], groups: dict[str, ProxyGroup]) -> set[str]:
    """被同级其它子组引用的组（如 AI-US 挂在 AI-AUTO 下），不在这一层再列一遍。"""
    covered: set[str] = set()
    nested_set = set(nested)
    for name in nested:
        child = groups.get(name)
        if child is None:
            continue
        for member in child.candidates:
            if member in nested_set and member != name:
                covered.add(member)
    return covered


def _order_nested(nested: list[str], parent_name: str, groups: dict[str, ProxyGroup]) -> list[str]:
    skip = _internal_nested_names(nested, groups) | AI_INTERNAL_POOLS
    visible = [name for name in nested if name not in skip]
    auto: list[str] = []
    regions: list[str] = []
    rest: list[str] = []
    for name in visible:
        child = groups.get(name)
        if name in AUTO_NAMES or (child is not None and _is_auto(child)):
            auto.append(name)
        elif name in AI_REGION_GROUPS or name in REGION_LABEL_VALUES or name in SWITCH_REGION_LABELS:
            regions.append(name)
        else:
            rest.append(name)
    regions.sort(key=_region_rank)
    if parent_name == AI_MANUAL_GROUP:
        return regions + rest + auto
    return auto + regions + rest


def _region_rank(name: str) -> int:
    preferred_ai = {AI_REGION_JP: 0, AI_REGION_US: 1, AI_REGION_SG: 2}
    if name in preferred_ai:
        return preferred_ai[name]
    for index, code in enumerate(SUBSCRIPTION_REGION_ORDER):
        if name in {code, SWITCH_REGION_LABELS.get(code, code)}:
            return index
    return 99


def _children_of(
    parent_name: str,
    groups: dict[str, ProxyGroup],
    extra_names: list[str],
    delays: dict[str, int],
) -> tuple[SwitchChoice, ...]:
    group = groups.get(parent_name)
    if group is None:
        return ()
    extra_set = set(extra_names)
    nested: list[str] = []
    leaves: list[str] = []
    for cand in group.candidates:
        if is_panel_info_node(cand) or cand in extra_set:
            continue
        if cand in AI_REGION_GROUPS and parent_name != AI_MANUAL_GROUP:
            continue
        if _is_nested_group(cand, groups):
            nested.append(cand)
        else:
            leaves.append(cand)

    choices: list[SwitchChoice] = []
    seen_displays: set[str] = set()
    for name in _order_nested(nested, parent_name, groups):
        child = groups[name]
        display = display_child(name, extra_names)
        if str(child.type).lower() in SELECTABLE_TYPES:
            node_children = _node_choices(
                name,
                child.candidates,
                groups,
                delays,
                prefix=((parent_name, name),),
                current=child.current,
            )
            choices.append(
                SwitchChoice(
                    key=name,
                    display=display,
                    annotation=f"{len(child.candidates)} 个节点",
                    annotation_style=DIM_STYLE,
                    drillable=True,
                    current=(group.current == name),
                    path=((parent_name, name),),
                    children=node_children,
                    group_name=parent_name,
                )
            )
        else:
            choices.append(
                SwitchChoice(
                    key=name,
                    display=display,
                    annotation="自动",
                    annotation_style=DIM_STYLE,
                    drillable=False,
                    current=(group.current == name),
                    path=((parent_name, name),),
                    children=(),
                    group_name=parent_name,
                )
            )
        seen_displays.add(display)

    buckets: dict[str, list[str]] = {}
    for leaf in leaves:
        buckets.setdefault(match_region(leaf), []).append(leaf)
    multi_region = sum(1 for members in buckets.values() if members) >= 2
    if multi_region:
        current_region = match_region(group.current) if group.current in leaves else None
        for region in SUBSCRIPTION_REGION_ORDER:
            members = buckets.get(region) or []
            if not members:
                continue
            label = SWITCH_REGION_LABELS.get(region, region)
            if label in seen_displays:
                continue
            current_node = group.current if group.current in members else None
            node_children = _node_choices(
                parent_name,
                members,
                groups,
                delays,
                prefix=(),
                current=current_node,
            )
            choices.append(
                SwitchChoice(
                    key=label,
                    display=label,
                    annotation=f"{len(members)} 个节点",
                    annotation_style=DIM_STYLE,
                    drillable=True,
                    current=(current_node is not None) or (current_region == region),
                    path=(),
                    children=node_children,
                    group_name=parent_name,
                )
            )
            seen_displays.add(label)
    else:
        for leaf in leaves:
            delay = resolve_delay(leaf, groups, delays)
            choices.append(
                SwitchChoice(
                    key=leaf,
                    display=leaf,
                    annotation=delay_label(delay),
                    annotation_style=delay_style(delay),
                    drillable=False,
                    current=(group.current == leaf),
                    path=((parent_name, leaf),),
                    children=(),
                    group_name=parent_name,
                )
            )
    if parent_name == AI_MANUAL_GROUP and choices:
        auto = [choice for choice in choices if choice.key in AUTO_NAMES or choice.annotation == "自动"]
        rest = [choice for choice in choices if choice not in auto]
        rest.sort(key=lambda choice: {"日本": 0, "美国": 1, "新加坡": 2}.get(choice.display, 99))
        choices = rest + auto
    return tuple(choices)


def _node_choices(
    owner_name: str,
    candidates: list[str],
    groups: dict[str, ProxyGroup],
    delays: dict[str, int],
    *,
    prefix: tuple[tuple[str, str], ...],
    current: str | None,
) -> tuple[SwitchChoice, ...]:
    items: list[SwitchChoice] = []
    for name in candidates:
        if is_panel_info_node(name):
            continue
        if _is_nested_group(name, groups):
            items.append(
                SwitchChoice(
                    key=name,
                    display=display_child(name),
                    annotation="组",
                    annotation_style=DIM_STYLE,
                    drillable=False,
                    current=(name == current),
                    path=(*prefix, (owner_name, name)),
                    children=(),
                    group_name=owner_name,
                )
            )
            continue
        delay = resolve_delay(name, groups, delays)
        items.append(
            SwitchChoice(
                key=name,
                display=name,
                annotation=delay_label(delay),
                annotation_style=delay_style(delay),
                drillable=False,
                current=(name == current),
                path=(*prefix, (owner_name, name)),
                children=(),
                group_name=owner_name,
            )
        )
    return tuple(items)
