from __future__ import annotations

import asyncio

from textual.app import App, ComposeResult
from textual.widget import Widget
from textual.widgets import Label, TabbedContent, TabPane

from cproxy.config import AppPaths
from cproxy.tui.screens.dashboard import DashboardScreen


class _TabbedApp(App):
    def __init__(self, paths):
        super().__init__()
        self.paths = paths

    def compose(self) -> ComposeResult:
        with TabbedContent(initial="dash", id="main-tabs"):
            with TabPane("概览", id="dash"):
                yield DashboardScreen(self.paths)
            with TabPane("其他", id="other"):
                yield Widget()


def test_dashboard_ai_cards_render_resolved_exit(ai_route_groups, tmp_path):
    """仪表盘 AI 卡片此前只读组自身的 delay——fixed fallback 组恒空，于是永远显示 `─`
    与 `?`；现在走统一的解析，延迟与健康都取自出口节点。"""
    paths = AppPaths(tmp_path / "config", tmp_path / "data", tmp_path / "state")

    async def run_case():
        app = _TabbedApp(paths)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(0.2)
            dashboard = app.query_one(DashboardScreen)
            dashboard._apply_ai_route(ai_route_groups)

            mode = str(dashboard.query_one("#dash-ai-mode", Label).render())
            active = str(dashboard.query_one("#dash-ai-active", Label).render())
            standby = str(dashboard.query_one("#dash-ai-standby", Label).render())
            gemini = str(dashboard.query_one("#dash-ai-gemini", Label).render())

            assert "兜底 · 优选池" in mode
            assert "→ -" not in active
            assert "AI-MANUAL → 🇰🇷韩国 01 IEPL 1.7X CM (140ms)" in active
            assert "140ms" in gemini
            assert "─" not in active  # 旧实现在这里显示 `(─)`
            assert "AI-US → 🇺🇸美国 01 IEPL 1.7X CM (210ms)" in standby

    asyncio.run(run_case())


def test_dashboard_ai_cards_keep_placeholder_when_groups_missing(tmp_path):
    paths = AppPaths(tmp_path / "config", tmp_path / "data", tmp_path / "state")

    async def run_case():
        app = _TabbedApp(paths)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(0.2)
            dashboard = app.query_one(DashboardScreen)
            dashboard._apply_ai_route({})
            # 组未配置时不拼半截数据，保持初始占位
            assert "─" in str(dashboard.query_one("#dash-ai-active", Label).render())

    asyncio.run(run_case())


def test_dashboard_timer_pauses_when_tab_hidden_and_resumes_on_show(monkeypatch, tmp_path):
    refresh_calls = []

    original_refresh = DashboardScreen.refresh_data

    def counting_refresh(self):
        refresh_calls.append(1)
        original_refresh(self)

    monkeypatch.setattr(DashboardScreen, "refresh_data", counting_refresh)
    paths = AppPaths(tmp_path / "config", tmp_path / "data", tmp_path / "state")

    async def run_case():
        app = _TabbedApp(paths)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(0.2)
            dashboard = app.query_one(DashboardScreen)
            tabbed = app.query_one(TabbedContent)

            # 初始挂载刷新一次（on_mount 的 call_later），timer 未暂停
            assert refresh_calls == [1]
            assert dashboard._timer_paused is False

            tabbed.active = "other"
            await pilot.pause(0.2)
            assert dashboard._timer_paused is True

            tabbed.active = "dash"
            await pilot.pause(0.2)
            assert dashboard._timer_paused is False
            # 回到 tab 立即刷新一次
            assert len(refresh_calls) == 2

    asyncio.run(run_case())
