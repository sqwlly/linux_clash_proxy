"""`backend.runtime.resolve_ai_route` 的单元测试。

这里是 AI 出口解析的单一定义点，CLI（`ai-status` / `status` / `incident`）与 TUI
（仪表盘 / AI 路由面板）都依赖它。此前 CLI 与 TUI 各写一套，而 CLI 那套假定
`AI-MANUAL.now` 一定是 AI-* 组名——改 fallback 兜底后它是**真实节点**，于是「出口」
显示成 `-`。这些用例把该不变式钉住。

现场数据（`ai_route_groups` fixture）与 TUI 渲染测试共用，见 `conftest.py`。
"""

from cproxy.backend.models import ProxyGroup
from cproxy.backend.runtime import resolve_ai_leaf, resolve_ai_route

KR_NODE = "🇰🇷韩国 01 | IEPL 1.7X | CM"
US_NODE = "🇺🇸美国 01 | IEPL 1.7X | CM"
SG_NODE = "🇸🇬新加坡 01 | 1X"


def _fallback(name, now, candidates=()):
    return ProxyGroup(name=name, type="Fallback", current=now, candidates=list(candidates))


# --- 兜底模式（AI-MANUAL.now 是真实节点）---------------------------------------


def test_manual_mode_resolves_pinned_node_as_exit(ai_route_groups):
    route = resolve_ai_route(ai_route_groups)

    assert route.present is True
    assert route.auto_mode is False
    assert route.pool == "AI-MANUAL"
    assert route.active.group == "AI-MANUAL"
    assert route.active.node == KR_NODE
    # 组自身 history 为空（mihomo 不健康检查 fixed fallback），延迟回退到出口节点
    assert route.active.delay == 140
    assert route.active.alive is True


def test_manual_mode_standby_maps_by_node_region(ai_route_groups):
    """备用按**出口节点地区**取另一侧：钉在美国时应给 AI-SG，而不是它自己所在的 AI-US。"""
    ai_route_groups["AI-MANUAL"] = _fallback("AI-MANUAL", US_NODE, ["AI-AUTO", KR_NODE, US_NODE])

    route = resolve_ai_route(ai_route_groups)

    assert route.active.node == US_NODE
    assert route.standby.group == "AI-SG"
    assert route.standby.node == SG_NODE


def test_manual_mode_standby_defaults_to_us_for_other_regions(ai_route_groups):
    route = resolve_ai_route(ai_route_groups)

    assert route.standby.group == "AI-US"
    assert route.standby.node == US_NODE
    assert route.standby.delay == 210


# --- 自动模式（AI-MANUAL.now 是 AI-AUTO 组）-----------------------------------


def test_auto_mode_descends_through_pool_to_leaf(ai_route_groups):
    ai_route_groups["AI-MANUAL"] = _fallback("AI-MANUAL", "AI-AUTO", ["AI-AUTO", KR_NODE])

    route = resolve_ai_route(ai_route_groups)

    assert route.auto_mode is True
    assert route.pool == "AI-US"  # AI-AUTO 当前选中的区域组
    assert route.active.group == "AI-US"
    assert route.active.node == US_NODE
    assert route.active.delay == 210  # 区域组自身的 history
    assert route.standby.group == "AI-SG"  # 池组显式成对


# --- Gemini ---------------------------------------------------------------


def test_gemini_delay_falls_back_to_node_history(ai_route_groups):
    route = resolve_ai_route(ai_route_groups)

    assert route.gemini is not None
    assert route.gemini.group == "AI-GEMINI"
    assert route.gemini.node == KR_NODE
    assert route.gemini.delay == 140


def test_gemini_absent_yields_none(ai_route_groups):
    route = resolve_ai_route({k: v for k, v in ai_route_groups.items() if k != "AI-GEMINI"})

    assert route.gemini is None


# --- 降级与环守卫 ----------------------------------------------------------


def test_missing_groups_degrade_without_raising():
    route = resolve_ai_route({})

    assert route.present is False
    assert route.auto_mode is False
    assert route.active.node == "AI-MANUAL"
    assert route.active.delay is None


def test_leaf_resolution_survives_self_referential_group():
    assert resolve_ai_leaf({"LOOP": _fallback("LOOP", "LOOP", ["LOOP"])}, "LOOP") == "LOOP"


def test_leaf_resolution_survives_two_cycle():
    groups = {
        "A": _fallback("A", "B", ["B"]),
        "B": _fallback("B", "A", ["A"]),
    }

    assert resolve_ai_leaf(groups, "A") == "A"


def test_leaf_of_name_absent_from_snapshot_is_the_name():
    groups = {"AI-US": _fallback("AI-US", "还没进快照的节点")}

    assert resolve_ai_leaf(groups, "AI-US") == "AI-US"
    assert resolve_ai_leaf(groups, "还没进快照的节点") == "还没进快照的节点"
