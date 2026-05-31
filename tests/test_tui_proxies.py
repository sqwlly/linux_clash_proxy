import asyncio
from pathlib import Path

from textual.app import App, ComposeResult
from textual.widgets import DataTable

from cproxy.backend.models import ProxyGroup
from cproxy.config import AppPaths
from cproxy.tui.screens import proxies as proxies_module
from cproxy.tui.screens.proxies import ProxiesScreen


class _ProxiesApp(App):
    def __init__(self, paths: AppPaths):
        super().__init__()
        self.paths = paths

    def compose(self) -> ComposeResult:
        yield ProxiesScreen(self.paths)


def test_proxies_screen_can_switch_selected_node(monkeypatch, tmp_path):
    switches = []

    class FakeQueryService:
        def __init__(self, paths):
            self.paths = paths

        def list_groups(self):
            return [
                ProxyGroup(
                    name="AI-MANUAL",
                    type="select",
                    current="Node A",
                    candidates=["Node A", "Node B"],
                )
            ]

        def switch_group(self, group, target):
            switches.append((group, target))
            return ProxyGroup(name=group, type="select", current=target, candidates=["Node A", "Node B"])

    monkeypatch.setattr(proxies_module, "QueryService", FakeQueryService)
    paths = AppPaths(tmp_path / "config", tmp_path / "data", tmp_path / "state")

    async def run_case():
        app = _ProxiesApp(paths)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(0.1)
            screen = app.query_one(ProxiesScreen)
            nodes_table = screen.query_one("#nodes-table", DataTable)
            nodes_table.move_cursor(row=1, animate=False)
            screen.action_select_node()
            await pilot.pause(0.1)

    asyncio.run(run_case())

    assert switches == [("AI-MANUAL", "Node B")]


def test_proxies_screen_left_right_and_escape_change_focus(monkeypatch, tmp_path):
    class FakeQueryService:
        def __init__(self, paths):
            self.paths = paths

        def list_groups(self):
            return [
                ProxyGroup(
                    name="AI-MANUAL",
                    type="select",
                    current="Node A",
                    candidates=["Node A", "Node B"],
                )
            ]

    monkeypatch.setattr(proxies_module, "QueryService", FakeQueryService)
    paths = AppPaths(tmp_path / "config", tmp_path / "data", tmp_path / "state")

    async def run_case():
        app = _ProxiesApp(paths)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(0.1)
            await pilot.press("right")
            assert app.focused is app.query_one("#nodes-table", DataTable)
            await pilot.press("escape")
            assert app.focused is app.query_one("#groups-table", DataTable)
            await pilot.press("right")
            await pilot.press("left")
            assert app.focused is app.query_one("#groups-table", DataTable)

    asyncio.run(run_case())
