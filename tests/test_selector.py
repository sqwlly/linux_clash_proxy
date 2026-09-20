"""交互式选择器与 `switch` 缺参接入的测试。

交互路径通过 `keys=` 注入按键流来测，因此不需要真终端；终端相关的行为
（tcsetattr 恢复）用 monkeypatch 断言。
"""

from __future__ import annotations

from argparse import Namespace

import pytest

from cproxy.backend.models import ProxyGroup
from cproxy.cli import _resolve_switch
from cproxy.interactive import NotATerminalError, _footer_line, _redraw, _window, select_one
from cproxy.services.switch_tree import delay_label, delay_style, resolve_delay


class _FakeStdin:
    def isatty(self) -> bool:
        return True

    def fileno(self) -> int:
        return 0


class _FakeService:
    def __init__(
        self,
        groups: list[ProxyGroup] | None = None,
        delays: dict[str, int] | None = None,
        match_group: str | None = None,
    ):
        self._groups = groups or [_group("G1", "Selector", ["node-1", "node-2"])]
        self._delays = delays or {}
        self._match_group = match_group

    def list_groups(self):
        groups: list[ProxyGroup] = []
        seen: set[str] = set()
        for group in self._groups:
            delay = self._delays.get(group.name, group.delay)
            if delay != group.delay:
                group = ProxyGroup(
                    name=group.name,
                    type=group.type,
                    current=group.current,
                    candidates=list(group.candidates),
                    alive=group.alive,
                    delay=delay,
                    source=group.source,
                )
            groups.append(group)
            seen.add(group.name)
        for name, delay in self._delays.items():
            if name not in seen:
                groups.append(ProxyGroup(name=name, type="Compatible", current="-", candidates=[], delay=delay))
                seen.add(name)
        return groups

    def get_group(self, name: str) -> ProxyGroup:
        for group in self._groups:
            if group.name == name:
                return group
        raise AssertionError(f"未预期的分组: {name}")

    def node_delays(self) -> dict[str, int]:
        return dict(self._delays)

    def match_rule_group(self) -> str | None:
        return self._match_group


def _group(name: str, group_type: str, candidates: list[str]) -> ProxyGroup:
    return ProxyGroup(name=name, type=group_type, current=candidates[0], candidates=list(candidates))


# --- 选择器核心行为（注入按键流）---


def test_enter_confirms_first_item():
    assert select_one("标题", ["a", "b", "c"], keys=["\r"]) == "a"


def test_j_and_down_move_selection():
    assert select_one("标题", ["a", "b", "c"], keys=["j", "\r"]) == "b"
    assert select_one("标题", ["a", "b", "c"], keys=["\x1b[B", "\x1b[B", "\r"]) == "c"


def test_k_and_up_move_selection():
    assert select_one("标题", ["a", "b", "c"], keys=["j", "k", "\r"]) == "a"
    # 首项再上移绕到末尾
    assert select_one("标题", ["a", "b", "c"], keys=["\x1b[A", "\r"]) == "c"


def test_selection_wraps_forward():
    assert select_one("标题", ["a", "b"], keys=["j", "j", "\r"]) == "a"


@pytest.mark.parametrize("key", ["q", "\x1b", "\x03"])
def test_cancel_keys_return_none(key):
    assert select_one("标题", ["a", "b"], keys=[key]) is None


def test_keyboard_interrupt_is_treated_as_cancel():
    def keys():
        yield "j"
        raise KeyboardInterrupt

    assert select_one("标题", ["a", "b"], keys=keys()) is None


def test_exhausted_key_stream_is_cancel():
    assert select_one("标题", ["a", "b"], keys=[]) is None


def test_empty_items_returns_none():
    assert select_one("标题", [], keys=["\r"]) is None


def test_current_item_is_preselected():
    assert select_one("标题", ["a", "b", "c"], current="b", keys=["\r"]) == "b"


def test_unknown_current_falls_back_to_first():
    assert select_one("标题", ["a", "b"], current="不存在", keys=["\r"]) == "a"


def test_non_tty_raises_not_a_terminal(monkeypatch):
    monkeypatch.setattr("cproxy.interactive.sys.stdin", __import__("io").StringIO())
    monkeypatch.setattr("cproxy.interactive.sys.stdout", __import__("io").StringIO())

    with pytest.raises(NotATerminalError):
        select_one("标题", ["a"])


# --- 窗口计算 ---


def test_window_shows_everything_when_short():
    assert _window(5, 2) == (0, 5)


def test_window_keeps_current_visible():
    start, end = _window(100, 50)
    assert end - start == 12
    assert start <= 50 < end


def test_window_clamps_at_both_edges():
    assert _window(100, 0)[0] == 0
    assert _window(100, 99)[1] == 100


def test_window_honors_custom_limit():
    start, end = _window(100, 50, limit=5)
    assert end - start == 5
    assert start <= 50 < end


# --- 终端状态恢复 ---


def test_terminal_is_restored_even_on_exception(monkeypatch):
    """异常路径必须还原 termios，否则用户的终端会留在 cbreak 模式。"""
    restored: list[object] = []
    monkeypatch.setattr("cproxy.interactive.interactive_supported", lambda: True)
    monkeypatch.setattr("cproxy.interactive.sys.stdin", _FakeStdin())
    monkeypatch.setattr("cproxy.interactive.termios.tcgetattr", lambda fd: ["saved-attrs"])
    monkeypatch.setattr("cproxy.interactive.termios.tcsetattr", lambda fd, when, attrs: restored.append(attrs))
    monkeypatch.setattr("cproxy.interactive.tty.setcbreak", lambda fd: None)
    monkeypatch.setattr("cproxy.interactive._write", lambda text: None)

    def boom(fd: int, size: int) -> bytes:
        raise RuntimeError("读取按键失败")

    monkeypatch.setattr("cproxy.interactive.os.read", boom)

    with pytest.raises(RuntimeError):
        select_one("标题", ["a", "b"])

    assert restored == [["saved-attrs"]]


# --- switch 的接入 ---


def test_both_args_pass_through_without_touching_service():
    """两个参数都给时原样返回——service 传 None 也不会被解引用。"""
    assert _resolve_switch(None, Namespace(group="G", target="N")) == [("G", "N")]


def test_single_arg_keeps_previous_error(capsys):
    with pytest.raises(SystemExit) as exc:
        _resolve_switch(None, Namespace(group="G", target=None))

    assert exc.value.code == 2
    assert "缺少必需参数" in capsys.readouterr().err


def test_no_args_uses_selector(monkeypatch):
    monkeypatch.setattr("cproxy.cli.select_one", lambda title, items, **kwargs: items[0])

    assert _resolve_switch(_FakeService(), Namespace(group=None, target=None)) == [("G1", "node-1")]


def test_no_args_only_offers_selectable_groups():
    """url-test 组不能手动切换，不该出现在候选里。"""
    service = _FakeService([_group("url", "URLTest", ["n"])])

    with pytest.raises(SystemExit) as exc:
        _resolve_switch(service, Namespace(group=None, target=None))

    assert exc.value.code == 1


def test_selector_receives_current_and_delays(monkeypatch):
    """选节点那一步要拿到当前值（用于高亮）与延迟（用于标注）。"""
    captured: dict = {}

    def fake_select(title, items, **kwargs):
        captured[title] = kwargs
        return items[0]

    monkeypatch.setattr("cproxy.cli.select_one", fake_select)
    service = _FakeService(delays={"node-1": 123})

    assert _resolve_switch(service, Namespace(group=None, target=None)) == [("G1", "node-1")]

    node_call = captured["选择节点"]
    assert node_call["current"] == "node-1", "应把组当前选择传给选择器做高亮"
    # 有测速记录的给毫秒数；没有的显式标 `-`（留空会让人分不清是没测过还是取数失败）
    assert node_call["annotations"] == {"node-1": "123 ms", "node-2": "-"}
    # 选入口那一步没有「当前组」的概念，不应传 current
    assert "current" not in captured["选择入口"] or captured["选择入口"]["current"] is None


def test_resolve_delay_drills_into_referenced_groups():
    """候选是组时要沿它当前出口往下钻。

    `AI-MANUAL` 的候选大多是 selector 组，而 selector 自身不测速——只查候选名
    永远得到空，各组的快慢也就无从比较（这正是「一排 `-` 看不出谁快」的成因）。
    """
    inner = _group("🇯🇵 Japan", "Selector", ["node-A"])
    outer = _group("AI-MANUAL", "Selector", ["🇯🇵 Japan"])
    groups = {g.name: g for g in (inner, outer)}
    delays = {"node-A": 123}

    assert resolve_delay("node-A", groups, delays) == 123  # 节点：直接取
    assert resolve_delay("🇯🇵 Japan", groups, delays) == 123  # 组：钻到当前节点
    assert resolve_delay("AI-MANUAL", groups, delays) == 123  # 多级组：一路钻到底
    assert resolve_delay("不存在的项", groups, delays) is None


def test_resolve_delay_survives_reference_cycle():
    """组之间互相引用成环时不能无限递归。"""
    a = _group("A", "Selector", ["B"])
    b = _group("B", "Selector", ["A"])

    assert resolve_delay("A", {"A": a, "B": b}, {}) is None


def test_delay_label_separates_timeout_from_missing():
    """延迟标注必须三态分明。

    `0` 是 mihomo 的测速失败标记——显示成 `0 ms` 会被读成「极快」，让人挑中
    实际不可用的节点；`-` 则是「没有测速记录」。两者不能混为一谈。
    """
    assert delay_label(None) == "-"
    assert delay_label(0) == "超时"
    assert delay_label(123) == "123 ms"


def test_cancel_exits_zero(monkeypatch):
    """用户按 q 取消不是错误，不该给非 0 退出码。"""
    monkeypatch.setattr("cproxy.cli.select_one", lambda *args, **kwargs: None)

    with pytest.raises(SystemExit) as exc:
        _resolve_switch(_FakeService(), Namespace(group=None, target=None))

    assert exc.value.code == 0


def test_non_tty_lists_candidates_and_exits_two(monkeypatch, capsys):
    def boom(*args, **kwargs):
        raise NotATerminalError("当前不是交互终端")

    monkeypatch.setattr("cproxy.cli.select_one", boom)

    with pytest.raises(SystemExit) as exc:
        _resolve_switch(_FakeService(), Namespace(group=None, target=None))

    assert exc.value.code == 2
    stderr = capsys.readouterr().err
    assert "不是交互终端" in stderr
    assert "G1" in stderr


# --- 搜索/过滤 ---


def test_search_filters_and_selects():
    """'/' 进入搜索模式，输入字符过滤，Enter 确认。"""
    items = ["🇺🇸 United States丨01", "🇯🇵 Japan丨01", "🇸🇬 Singapore丨01"]
    # /jap → 过滤出 Japan → Enter 确认
    result = select_one("标题", items, keys=["/", "j", "a", "p", "\r"])
    assert result == "🇯🇵 Japan丨01"


def test_search_case_insensitive():
    """搜索不区分大小写。"""
    items = ["Alpha", "Beta", "Gamma"]
    result = select_one("标题", items, keys=["/", "B", "E", "\r"])
    assert result == "Beta"


def test_search_fuzzy_match():
    """模糊匹配：query 的字符按顺序出现即可。"""
    from cproxy.interactive import _fuzzy_match

    assert _fuzzy_match("🇺🇸 United States丨01", "us")
    assert _fuzzy_match("Singapore", "sg")
    assert _fuzzy_match("Japan", "jpn")
    assert not _fuzzy_match("Japan", "xyz")


def test_search_esc_exits_search_and_restores_list():
    """Esc 退出搜索后恢复完整列表，再按 Enter 能正常选中。"""
    items = ["a", "b", "c"]
    # /x (过滤掉所有) → Esc (恢复) → Enter (选中第一个)
    result = select_one("标题", items, keys=["/", "x", "\x1b", "\r"])
    assert result == "a"


def test_search_backspace_deletes_char():
    """搜索模式中退格删除最后一个字符。"""
    items = ["alpha", "beta", "gamma"]
    # /be → 匹配 beta → 退格 → 查询变成 "b" → 仍匹配 beta → Enter
    result = select_one("标题", items, keys=["/", "b", "e", "\x7f", "\r"])
    assert result == "beta"


def test_search_empty_backspace_exits_search():
    """搜索模式下查询为空时退格退出搜索。"""
    items = ["a", "b"]
    # / 进入搜索 → 退格（空查询→退出搜索）→ j 向下 → Enter
    result = select_one("标题", items, keys=["/", "\x7f", "j", "\r"])
    assert result == "b"


def test_search_navigate_with_arrows():
    """搜索模式内仍可用方向键移动。"""
    items = ["ab", "ac", "bc"]
    # /a → 匹配 ab, ac → ↓ 移到 ac → Enter
    result = select_one("标题", items, keys=["/", "a", "\x1b[B", "\r"])
    assert result == "ac"


def test_delay_style_coloring():
    """延迟着色阈值：≤200 绿、≤500 黄、>500 红、超时红、无记录暗灰。"""
    assert "\033[2m" in delay_style(None)      # dim
    assert "\033[31m" in delay_style(0)         # red (超时)
    assert "\033[32m" in delay_style(100)       # green
    assert "\033[32m" in delay_style(200)       # green (边界)
    assert "\033[33m" in delay_style(300)       # yellow
    assert "\033[33m" in delay_style(500)       # yellow (边界)
    assert "\033[31m" in delay_style(600)       # red (慢)


def test_annotation_styles_passed_through(monkeypatch):
    """_resolve_switch 传给 select_one 的 annotation_styles 应含每个候选。"""
    captured: dict = {}

    def fake_select(title, items, **kwargs):
        captured[title] = kwargs
        return items[0]

    monkeypatch.setattr("cproxy.cli.select_one", fake_select)
    service = _FakeService(delays={"node-1": 123, "node-2": 0})

    _resolve_switch(service, Namespace(group=None, target=None))

    node_call = captured["选择节点"]
    assert "annotation_styles" in node_call
    styles = node_call["annotation_styles"]
    assert "node-1" in styles
    assert "node-2" in styles


def test_interactive_switch_does_not_refetch_after_list(monkeypatch):
    """选完分组后不应再打 get_group / node_delays——那两下会让节点列表出现前卡一下。"""

    class Counting(_FakeService):
        def __init__(self):
            super().__init__(delays={"node-1": 123})
            self.get_calls = 0
            self.delay_calls = 0

        def get_group(self, name: str) -> ProxyGroup:
            self.get_calls += 1
            return super().get_group(name)

        def node_delays(self) -> dict[str, int]:
            self.delay_calls += 1
            return super().node_delays()

    service = Counting()
    monkeypatch.setattr("cproxy.cli.select_one", lambda title, items, **kwargs: items[0])
    assert _resolve_switch(service, Namespace(group=None, target=None)) == [("G1", "node-1")]
    assert service.get_calls == 0
    assert service.delay_calls == 0


# --- 返回上一级 / 擦除残帧 ---


def test_left_arrow_ignored_without_back_hint():
    """未开启 back_hint 时左方向键不是取消，Enter 仍能选中。"""
    assert select_one("标题", ["a", "b"], keys=["\x1b[D", "\r"]) == "a"


def test_left_arrow_returns_none_when_back_hinted():
    assert select_one("标题", ["a", "b"], back_hint=True, keys=["\x1b[D"]) is None


def test_h_returns_none_when_back_hinted():
    assert select_one("标题", ["a", "b"], back_hint=True, keys=["h"]) is None


def test_h_is_not_back_without_hint():
    assert select_one("标题", ["a", "b"], keys=["h", "\r"]) == "a"


def test_footer_back_hint_says_return():
    assert "返回" in _footer_line(back_hint=True)
    assert "取消" not in _footer_line(back_hint=True)
    assert "取消" in _footer_line(back_hint=False)


def test_node_cancel_returns_to_group_selector(monkeypatch):
    """节点列表取消应回到分组选择，而不是直接退出。"""
    calls: list[str] = []

    def fake_select(title, items, **kwargs):
        calls.append(title)
        if title == "选择节点" and calls.count("选择节点") == 1:
            return None
        return items[0]

    monkeypatch.setattr("cproxy.cli.select_one", fake_select)
    assert _resolve_switch(_FakeService(), Namespace(group=None, target=None)) == [("G1", "node-1")]
    assert calls == ["选择入口", "选择节点", "选择入口", "选择节点"]


def test_switch_entry_groups_hide_ai_region_pools():
    """Japan / Singapore / United States 是 AI 地区池，不和 CyberGuard 并列。"""
    from cproxy.services.switch_tree import build_switch_tree

    groups = {
        group.name: group
        for group in [
            _group("AI-MANUAL", "Selector", ["🇯🇵 Japan"]),
            _group("CyberGuard", "Selector", ["HK-1"]),
            _group("GLOBAL", "Selector", ["DIRECT"]),
            _group("🇯🇵 Japan", "Selector", ["JP-1"]),
            _group("🇸🇬 Singapore", "Selector", ["SG-1"]),
            _group("🇺🇸 United States", "Selector", ["US-1"]),
        ]
    }
    entries = build_switch_tree(groups, match_group="CyberGuard")
    assert [entry.key for entry in entries] == ["CyberGuard", "AI-MANUAL", "GLOBAL"]
    assert [entry.display for entry in entries] == ["默认流量", "AI 出口", "全局"]
    assert "决定其余流量" in entries[0].annotation
    assert "决定 AI 出口" in entries[1].annotation
    assert "绕过规则" in entries[2].annotation


def test_lookup_choice_resolves_display_names_before_nested_regions():
    from cproxy.services.switch_tree import build_switch_tree, lookup_choice, resolve_group_query

    groups = {
        group.name: group
        for group in [
            _group("AI-MANUAL", "Selector", ["US-01", "JP-01"]),
            _group("CyberGuard", "Selector", ["日本", "美国"]),
            _group("GLOBAL", "Selector", ["DIRECT"]),
            _group("日本", "Selector", ["JP-1"]),
            _group("美国", "Selector", ["US-1"]),
        ]
    }
    entries = build_switch_tree(groups, match_group="CyberGuard")
    assert lookup_choice("默认流量", entries).key == "CyberGuard"
    assert lookup_choice("AI 出口", entries).key == "AI-MANUAL"
    assert resolve_group_query("默认流量", groups, entries) == "CyberGuard"
    assert resolve_group_query("日本", groups, entries) == "日本"


def test_switch_node_choices_keeps_direct_nodes_from_mixing_other_subs():
    """CyberGuard 按国家分组，不把挂进来的其它订阅摊成叶子。"""
    from cproxy.services.switch_tree import build_switch_tree

    nested = {
        "CyberGuard": ProxyGroup(
            name="CyberGuard",
            type="Selector",
            current="HK-1",
            candidates=["自动选择", "HK-1", "JP-1", "Mitce"],
        ),
        "自动选择": ProxyGroup(
            name="自动选择",
            type="URLTest",
            current="HK-1",
            candidates=["HK-1", "JP-1"],
        ),
        "Mitce": ProxyGroup(
            name="Mitce",
            type="Selector",
            current="Mitce-HK",
            candidates=["Mitce-HK"],
        ),
        "Mitce-HK": ProxyGroup(
            name="Mitce-HK",
            type="URLTest",
            current="Mitce HK-1",
            candidates=["Mitce HK-1", "Mitce HK-2"],
        ),
    }
    entries = build_switch_tree(nested, match_group="CyberGuard", extra_names=["Mitce"], delays={"HK-1": 80, "JP-1": 120})
    assert [entry.key for entry in entries] == ["CyberGuard", "Mitce"]
    cyber = entries[0]
    assert [child.display for child in cyber.children] == ["自动", "香港", "日本"]
    assert "Mitce" not in [child.key for child in cyber.children]
    assert "Mitce HK-1" not in [child.key for child in cyber.children]
    assert "HK-1" not in [child.display for child in cyber.children]
    auto = next(child for child in cyber.children if child.display == "自动")
    assert auto.annotation == "自动"
    assert auto.path == (("CyberGuard", "自动选择"),)
    hongkong = next(child for child in cyber.children if child.display == "香港")
    assert hongkong.children[0].key == "HK-1"
    assert hongkong.children[0].path == (("CyberGuard", "HK-1"),)
    japan = next(child for child in cyber.children if child.display == "日本")
    assert japan.children[0].key == "JP-1"
    assert hongkong.current is True


def test_switch_node_choices_lists_policy_groups_without_flattening():
    """AI-MANUAL 第二步只列地区和自动策略，不把 AI-US/AI-SG 内部池并列出来。"""
    from cproxy.services.switch_tree import build_switch_tree

    nested = {
        "AI-MANUAL": ProxyGroup(
            name="AI-MANUAL",
            type="Selector",
            current="🇯🇵 Japan",
            candidates=["🇺🇸 United States", "AI-AUTO", "AI-US", "AI-SG", "🇯🇵 Japan", "🇸🇬 Singapore"],
        ),
        "AI-AUTO": ProxyGroup(name="AI-AUTO", type="Fallback", current="AI-US", candidates=["AI-US", "AI-SG"]),
        "AI-US": ProxyGroup(name="AI-US", type="Fallback", current="US-01", candidates=["US-01", "US-02"]),
        "AI-SG": ProxyGroup(name="AI-SG", type="Fallback", current="SG-01", candidates=["SG-01"]),
        "🇯🇵 Japan": ProxyGroup(name="🇯🇵 Japan", type="Selector", current="JP-01", candidates=["JP-01"]),
        "🇺🇸 United States": ProxyGroup(
            name="🇺🇸 United States", type="Selector", current="US-01", candidates=["US-01", "US-02"]
        ),
        "🇸🇬 Singapore": ProxyGroup(name="🇸🇬 Singapore", type="Selector", current="SG-01", candidates=["SG-01"]),
    }
    entries = build_switch_tree(nested, delays={"US-01": 100, "JP-01": 150})
    ai = next(entry for entry in entries if entry.key == "AI-MANUAL")
    assert [child.display for child in ai.children] == ["日本", "美国", "新加坡", "自动"]
    assert "AI-US" not in [child.key for child in ai.children]
    assert "AI-SG" not in [child.key for child in ai.children]
    japan = next(child for child in ai.children if child.display == "日本")
    assert japan.annotation == "1 个节点"
    auto = next(child for child in ai.children if child.display == "自动")
    assert auto.annotation == "自动"
    assert auto.path == (("AI-MANUAL", "AI-AUTO"),)
    assert japan.children[0].key == "JP-01"
    assert japan.current is True


def test_switch_tree_ai_manual_buckets_nodes_without_emoji_groups():
    from cproxy.services.switch_tree import build_switch_tree

    nested = {
        "AI-MANUAL": ProxyGroup(
            name="AI-MANUAL",
            type="Selector",
            current="US-01",
            candidates=["US-01", "AI-AUTO", "JP-01", "SG-01"],
        ),
        "AI-AUTO": ProxyGroup(name="AI-AUTO", type="Fallback", current="AI-US", candidates=["AI-US", "AI-SG"]),
        "AI-US": ProxyGroup(name="AI-US", type="Fallback", current="US-01", candidates=["US-01"]),
        "AI-SG": ProxyGroup(name="AI-SG", type="Fallback", current="SG-01", candidates=["SG-01"]),
    }
    entries = build_switch_tree(nested)
    ai = next(entry for entry in entries if entry.key == "AI-MANUAL")
    assert [child.display for child in ai.children] == ["日本", "美国", "新加坡", "自动"]
    assert "🇯🇵 Japan" not in [child.key for child in ai.children]
    japan = next(child for child in ai.children if child.display == "日本")
    assert japan.children[0].path == (("AI-MANUAL", "JP-01"),)
    auto = next(child for child in ai.children if child.display == "自动")
    assert auto.path == (("AI-MANUAL", "AI-AUTO"),)


def test_resolve_switch_returns_leaf_switch_path(monkeypatch):
    nested = [
        _group("AI-MANUAL", "Selector", ["JP"]),
        _group("JP", "Selector", ["JP-01", "JP-02"]),
    ]
    service = _FakeService(nested)

    def fake_select(title, items, **kwargs):
        if title == "选择入口":
            return items[0]
        if title == "选择地区":
            return items[0]
        return "JP-02"

    monkeypatch.setattr("cproxy.cli.select_one", fake_select)
    assert _resolve_switch(service, Namespace(group=None, target=None)) == [
        ("AI-MANUAL", "JP"),
        ("JP", "JP-02"),
    ]


def test_back_to_group_keeps_previous_selection(monkeypatch):
    """从节点列表返回后，分组选择应停在刚才那一组。"""
    group_currents: list[object] = []

    def fake_select(title, items, **kwargs):
        if title == "选择入口":
            group_currents.append(kwargs.get("current"))
            return items[0]
        if len(group_currents) == 1:
            return None
        return items[0]

    monkeypatch.setattr("cproxy.cli.select_one", fake_select)
    assert _resolve_switch(_FakeService(), Namespace(group=None, target=None)) == [("G1", "node-1")]
    assert group_currents[0] is None
    assert group_currents[1] == "G1"


def test_redraw_clears_previous_frame_before_writing(monkeypatch):
    """重绘必须先擦掉上一帧，否则短行会在长行尾巴上留下残字。"""
    writes: list[str] = []
    monkeypatch.setattr("cproxy.interactive._write", writes.append)
    monkeypatch.setattr("cproxy.interactive._visible_capacity", lambda **kwargs: 12)

    drawn = _redraw(["alpha", "beta"], 0, "标题", 0)
    assert drawn > 0
    first = "".join(writes)
    assert not first.startswith("\033[") or "A\r" not in first[:20]

    writes.clear()
    _redraw(["alpha", "beta"], 1, "标题", drawn)
    payload = "".join(writes)
    assert payload.startswith(f"\033[{drawn}A\r\033[J")
    assert payload.index("\033[J") < payload.index("beta")


def test_selector_erases_its_frame_on_exit(monkeypatch):
    """select_one 退出时必须擦掉菜单，否则下一级会画在下面、看起来像回不去。"""
    writes: list[str] = []
    monkeypatch.setattr("cproxy.interactive.interactive_supported", lambda: True)
    monkeypatch.setattr("cproxy.interactive.sys.stdin", _FakeStdin())
    monkeypatch.setattr("cproxy.interactive.termios.tcgetattr", lambda fd: ["saved-attrs"])
    monkeypatch.setattr("cproxy.interactive.termios.tcsetattr", lambda fd, when, attrs: None)
    monkeypatch.setattr("cproxy.interactive.tty.setcbreak", lambda fd: None)
    monkeypatch.setattr("cproxy.interactive._write", writes.append)
    monkeypatch.setattr("cproxy.interactive._visible_capacity", lambda **kwargs: 12)

    keys = iter(["\r"])
    monkeypatch.setattr("cproxy.interactive._terminal_read_key", lambda fd: lambda: next(keys, None))

    assert select_one("标题", ["a", "b"]) == "a"

    assert writes[0] == "\033[?25l"
    assert writes[-1] == "\033[?25h"
    # 退出时单独一帧：CUU 回到菜单起点再 CSI J 擦掉
    assert any(w.startswith("\033[") and "A\r" in w and w.endswith("\033[J") for w in writes)
