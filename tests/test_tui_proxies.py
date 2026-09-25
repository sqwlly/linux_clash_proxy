import asyncio
from types import SimpleNamespace

from textual.app import App, ComposeResult
from textual.widgets import Button, DataTable

from cproxy.backend.models import DelayCheckResult, GroupCheckReport, ProxyGroup
from cproxy.config import AppPaths
from cproxy.tui.screens import proxies as proxies_module
from cproxy.tui.screens.proxies import ProxiesScreen


class _ProxiesApp(App):
    def __init__(self, paths: AppPaths):
        super().__init__()
        self.paths = paths

    def compose(self) -> ComposeResult:
        yield ProxiesScreen(self.paths)


def _group(current: str = "Node A", name: str = "AI-MANUAL", candidates: list[str] | None = None) -> ProxyGroup:
    return ProxyGroup(
        name=name,
        type="select",
        current=current,
        candidates=candidates or ["Node A", "Node B"],
    )


def test_proxies_screen_can_switch_selected_node(monkeypatch, tmp_path):
    switches = []

    class FakeQueryService:
        def __init__(self, paths):
            self.paths = paths

        def load_context(self, require_api=False):
            return SimpleNamespace(groups={"AI-MANUAL": _group()}, api_available=True)

        def switch_group(self, group, target):
            switches.append((group, target))
            return _group(target)

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


def test_proxies_screen_refresh_uses_latest_group_after_switch(monkeypatch, tmp_path):
    current = {"value": "Node A"}

    class FakeQueryService:
        def __init__(self, paths):
            self.paths = paths

        def load_context(self, require_api=False):
            return SimpleNamespace(groups={"AI-MANUAL": _group(current["value"])}, api_available=True)

        def switch_group(self, group, target):
            current["value"] = target
            return _group(target)

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
            assert "Node B" in str(screen.query_one("#current-node").render())
            first_row = str(nodes_table.get_row_at(0)[0])
            second_row = str(nodes_table.get_row_at(1)[0])
            assert "●" not in first_row
            assert "●" in second_row

    asyncio.run(run_case())


def test_proxies_screen_reports_api_unavailable_without_switching(monkeypatch, tmp_path):
    switches = []

    class FakeQueryService:
        def __init__(self, paths):
            self.paths = paths

        def load_context(self, require_api=False):
            return SimpleNamespace(groups={"AI-MANUAL": _group()}, api_available=False)

        def switch_group(self, group, target):
            switches.append((group, target))

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
            assert "API 不可访问" in str(screen.query_one("#proxy-action-status").render())

    asyncio.run(run_case())

    assert switches == []


def test_proxies_screen_left_right_and_escape_change_focus(monkeypatch, tmp_path):
    class FakeQueryService:
        def __init__(self, paths):
            self.paths = paths

        def load_context(self, require_api=False):
            return SimpleNamespace(groups={"AI-MANUAL": _group()}, api_available=True)

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


def test_proxies_screen_group_cursor_does_not_rebuild_nodes_until_entering(monkeypatch, tmp_path):
    class FakeQueryService:
        def __init__(self, paths):
            self.paths = paths

        def load_context(self, require_api=False):
            return SimpleNamespace(
                groups={
                    "AI-MANUAL": _group(),
                    "CyberGuard": _group("Node C", name="CyberGuard", candidates=["Node C", "Node D"]),
                },
                api_available=True,
            )

    monkeypatch.setattr(proxies_module, "QueryService", FakeQueryService)
    paths = AppPaths(tmp_path / "config", tmp_path / "data", tmp_path / "state")

    async def run_case():
        app = _ProxiesApp(paths)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(0.1)
            screen = app.query_one(ProxiesScreen)
            groups_table = app.query_one("#groups-table", DataTable)
            nodes_table = app.query_one("#nodes-table", DataTable)
            groups_table.focus()
            groups_table.move_cursor(row=0, animate=False)

            updates = []
            original_update = screen._update_nodes_table

            def counted_update():
                updates.append(screen._current_group.name if screen._current_group else "")
                original_update()

            screen._update_nodes_table = counted_update
            await pilot.press("down")
            await pilot.pause(0.1)
            assert updates == []
            assert screen._current_group.name == "AI-MANUAL"

            await pilot.press("right")
            await pilot.pause(0.1)
            assert updates == ["CyberGuard"]
            assert screen._current_group.name == "CyberGuard"
            assert app.focused is nodes_table
            assert "Node C" in str(screen.query_one("#current-node").render())

    asyncio.run(run_case())


def test_proxies_screen_escape_on_groups_is_safe_without_parent_tabs(monkeypatch, tmp_path):
    class FakeQueryService:
        def __init__(self, paths):
            self.paths = paths

        def load_context(self, require_api=False):
            return SimpleNamespace(groups={"AI-MANUAL": _group()}, api_available=True)

    monkeypatch.setattr(proxies_module, "QueryService", FakeQueryService)
    paths = AppPaths(tmp_path / "config", tmp_path / "data", tmp_path / "state")

    async def run_case():
        app = _ProxiesApp(paths)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(0.1)
            groups_table = app.query_one("#groups-table", DataTable)
            groups_table.focus()
            await pilot.press("escape")
            assert app.focused is groups_table

    asyncio.run(run_case())


def test_proxies_screen_down_moves_node_cursor(monkeypatch, tmp_path):
    class FakeQueryService:
        def __init__(self, paths):
            self.paths = paths

        def load_context(self, require_api=False):
            return SimpleNamespace(groups={"AI-MANUAL": _group()}, api_available=True)

    monkeypatch.setattr(proxies_module, "QueryService", FakeQueryService)
    paths = AppPaths(tmp_path / "config", tmp_path / "data", tmp_path / "state")

    async def run_case():
        app = _ProxiesApp(paths)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(0.1)
            await pilot.press("right")
            nodes_table = app.query_one("#nodes-table", DataTable)
            nodes_table.move_cursor(row=0, animate=False)
            assert nodes_table.cursor_row == 0
            await pilot.press("down")
            assert nodes_table.cursor_row == 1
            await pilot.press("down")
            assert app.focused is app.query_one("#btn-switch-node", Button)

    asyncio.run(run_case())


def test_proxies_screen_left_column_is_usage_entries(monkeypatch, tmp_path):
    class FakeQueryService:
        def __init__(self, paths):
            self.paths = paths

        def load_context(self, require_api=False):
            return SimpleNamespace(
                groups={
                    "AI-MANUAL": _group(),
                    "CyberGuard": _group("Node C", name="CyberGuard", candidates=["Node C", "Node D"]),
                    "🇯🇵 Japan": _group("JP-1", name="🇯🇵 Japan", candidates=["JP-1"]),
                },
                api_available=True,
            )

        def match_rule_group(self):
            return "CyberGuard"

    monkeypatch.setattr(proxies_module, "QueryService", FakeQueryService)
    paths = AppPaths(tmp_path / "config", tmp_path / "data", tmp_path / "state")

    async def run_case():
        app = _ProxiesApp(paths)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(0.1)
            groups_table = app.query_one("#groups-table", DataTable)
            keys = [str(row.key.value) for row in groups_table.ordered_rows]
            assert keys == ["CyberGuard", "AI-MANUAL"]
            assert "🇯🇵 Japan" not in keys
            assert "默认流量" in str(groups_table.get_row_at(0)[0])
            assert "AI 出口" in str(groups_table.get_row_at(1)[0])

    asyncio.run(run_case())


def test_proxies_delay_test_streams_progress_and_preserves_cursor(monkeypatch, tmp_path):
    class FakeQueryService:
        def __init__(self, paths):
            self.paths = paths

        def load_context(self, require_api=False):
            return SimpleNamespace(groups={"AI-MANUAL": _group()}, api_available=True)

    class FakeDiagnostics:
        def __init__(self, paths):
            self.paths = paths

        def test_group(self, group_name, *, on_progress=None):
            on_progress(1, 2, "Node A", 120)
            on_progress(2, 2, "Node B", None)
            return GroupCheckReport(
                group_name=group_name,
                results=[
                    DelayCheckResult(name="Node A", ok=True, delay=120),
                    DelayCheckResult(name="Node B", ok=False, delay=None),
                ],
            )

    monkeypatch.setattr(proxies_module, "QueryService", FakeQueryService)
    monkeypatch.setattr(proxies_module, "DiagnosticsService", FakeDiagnostics)
    paths = AppPaths(tmp_path / "config", tmp_path / "data", tmp_path / "state")

    async def run_case():
        app = _ProxiesApp(paths)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(0.1)
            screen = app.query_one(ProxiesScreen)
            nodes_table = screen.query_one("#nodes-table", DataTable)
            nodes_table.move_cursor(row=1, animate=False)

            streamed = []
            original = screen._on_test_progress

            def spy(group_name, current_name, done, total, node_name, delay):
                original(group_name, current_name, done, total, node_name, delay)
                streamed.append((done, str(nodes_table.get_row(node_name)[1])))

            screen._on_test_progress = spy
            screen.action_test_delay()
            await pilot.pause(0.3)

            assert streamed == [(1, "[#a3e635]120ms[/]"), (2, "[#fb7185]失败[/]")]
            assert nodes_table.row_count == 2
            assert nodes_table.cursor_row == 1
            assert "测速完成" in str(screen.query_one("#proxy-action-status").render())

    asyncio.run(run_case())


def test_proxies_refresh_updates_cells_in_place_without_cursor_events(monkeypatch, tmp_path):
    current = {"value": "Node A"}

    class FakeQueryService:
        def __init__(self, paths):
            self.paths = paths

        def load_context(self, require_api=False):
            return SimpleNamespace(groups={"AI-MANUAL": _group(current["value"])}, api_available=True)

    monkeypatch.setattr(proxies_module, "QueryService", FakeQueryService)
    paths = AppPaths(tmp_path / "config", tmp_path / "data", tmp_path / "state")

    original_handler = ProxiesScreen.on_data_table_row_highlighted

    def counting_handler(self, event):
        highlights.append(event.row_key.value)
        original_handler(self, event)

    highlights = []
    monkeypatch.setattr(ProxiesScreen, "on_data_table_row_highlighted", counting_handler)

    async def run_case():
        app = _ProxiesApp(paths)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(0.1)
            screen = app.query_one(ProxiesScreen)
            nodes_table = screen.query_one("#nodes-table", DataTable)
            nodes_table.move_cursor(row=1, animate=False)
            await pilot.pause(0.05)
            highlights.clear()

            current["value"] = "Node B"
            screen.refresh_data()
            await pilot.pause(0.3)

            assert nodes_table.cursor_row == 1
            first_row = str(nodes_table.get_row_at(0)[0])
            second_row = str(nodes_table.get_row_at(1)[0])
            assert "●" not in first_row and "Node A" in first_row
            assert "●" in second_row and "Node B" in second_row
            assert highlights == []

    asyncio.run(run_case())
