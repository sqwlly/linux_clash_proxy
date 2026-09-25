from __future__ import annotations

import asyncio

from textual.app import App, ComposeResult
from textual.widget import Widget
from textual.widgets import TabbedContent, TabPane

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
