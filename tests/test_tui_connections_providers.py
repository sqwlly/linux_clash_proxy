import asyncio
from types import SimpleNamespace

from textual.app import App, ComposeResult
from textual.widgets import DataTable

from cproxy.backend.models import ConnectionEntry, ProviderEntry
from cproxy.config import AppPaths
from cproxy.tui.screens import connections as connections_module
from cproxy.tui.screens import providers as providers_module
from cproxy.tui.screens.connections import ConnectionsScreen
from cproxy.tui.screens.providers import ProvidersScreen


class _ScreenApp(App):
    def __init__(self, screen):
        super().__init__()
        self._body = screen

    def compose(self) -> ComposeResult:
        yield self._body


async def _wait_for(predicate, pilot, timeout: float = 5.0) -> None:
    for _ in range(int(timeout / 0.05)):
        if predicate():
            return
        await pilot.pause(0.05)
    raise AssertionError("等待超时")


def test_connections_screen_closes_selected_connection(monkeypatch, tmp_path):
    closed = []

    class FakeQueryService:
        def __init__(self, paths):
            self.paths = paths

        def list_connections(self):
            return [
                ConnectionEntry(
                    id="conn-1",
                    host="example.test",
                    process="curl",
                    rule="MATCH",
                    proxy_chain=["Proxy", "Node A"],
                    upload=1024,
                    download=2048,
                )
            ]

        def close_connection(self, connection_id):
            closed.append(connection_id)

    monkeypatch.setattr(connections_module, "QueryService", FakeQueryService)
    paths = AppPaths(tmp_path / "config", tmp_path / "data", tmp_path / "state")

    async def run_case():
        app = _ScreenApp(ConnectionsScreen(paths))
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(0.1)
            table = app.query_one("#connections-table", DataTable)
            table.move_cursor(row=0, animate=False)
            app.query_one(ConnectionsScreen).action_close_selected()
            await pilot.pause(0.1)

    asyncio.run(run_case())

    assert closed == ["conn-1"]


def test_connections_screen_requires_second_close_all_press(monkeypatch, tmp_path):
    calls = SimpleNamespace(close_all=0)

    class FakeQueryService:
        def __init__(self, paths):
            self.paths = paths

        def list_connections(self):
            return [
                ConnectionEntry("conn-1", "example.test", "curl", "MATCH", ["Proxy"], 1, 2),
            ]

        def close_all_connections(self):
            calls.close_all += 1

    monkeypatch.setattr(connections_module, "QueryService", FakeQueryService)
    paths = AppPaths(tmp_path / "config", tmp_path / "data", tmp_path / "state")

    async def run_case():
        app = _ScreenApp(ConnectionsScreen(paths))
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(0.1)
            screen = app.query_one(ConnectionsScreen)
            screen.action_close_all()
            await pilot.pause(0.1)
            assert calls.close_all == 0
            screen.action_close_all()
            await pilot.pause(0.1)

    asyncio.run(run_case())

    assert calls.close_all == 1


def test_providers_screen_updates_selected_provider(monkeypatch, tmp_path):
    updated = []

    class FakeQueryService:
        def __init__(self, paths):
            self.paths = paths

        def list_proxy_providers(self):
            return [ProviderEntry("corp", "HTTP", "HTTP", 2, "2026-05-31T12:00:00Z")]

        def update_proxy_provider(self, name):
            updated.append(name)

    monkeypatch.setattr(providers_module, "QueryService", FakeQueryService)
    paths = AppPaths(tmp_path / "config", tmp_path / "data", tmp_path / "state")

    async def run_case():
        app = _ScreenApp(ProvidersScreen(paths))
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(0.1)
            table = app.query_one("#providers-table", DataTable)
            table.move_cursor(row=0, animate=False)
            app.query_one(ProvidersScreen).action_update_provider()
            await pilot.pause(0.1)

    asyncio.run(run_case())

    assert updated == ["corp"]


def _connection(connection_id: str, host: str, upload: int = 1, download: int = 2) -> ConnectionEntry:
    return ConnectionEntry(connection_id, host, "proc", "MATCH", ["Proxy"], upload, download)


def test_connections_refresh_keeps_cursor_on_same_connection(monkeypatch, tmp_path):
    state = SimpleNamespace(
        connections=[
            _connection("conn-1", "a.test"),
            _connection("conn-2", "b.test"),
            _connection("conn-3", "c.test"),
        ]
    )

    class FakeQueryService:
        def __init__(self, paths):
            self.paths = paths

        def list_connections(self):
            return state.connections

    monkeypatch.setattr(connections_module, "QueryService", FakeQueryService)
    paths = AppPaths(tmp_path / "config", tmp_path / "data", tmp_path / "state")

    async def run_case():
        app = _ScreenApp(ConnectionsScreen(paths))
        async with app.run_test(size=(100, 30)) as pilot:
            await _wait_for(lambda: app.query_one("#connections-table", DataTable).row_count == 3, pilot)
            screen = app.query_one(ConnectionsScreen)
            table = app.query_one("#connections-table", DataTable)
            table.move_cursor(row=1, animate=False)
            assert screen._selected_connection().id == "conn-2"

            # 删除光标上方的 conn-1，并更新 conn-3 的流量单元格
            state.connections = [_connection("conn-2", "b.test"), _connection("conn-3", "c.test", upload=4096)]
            screen.refresh_data()
            await _wait_for(lambda: table.row_count == 2, pilot)
            await pilot.pause(0.1)

            assert screen._selected_connection().id == "conn-2"
            assert table.cursor_row == 0
            assert table.get_row("conn-3")[4] == "4.0 KB"

    asyncio.run(run_case())


def test_connections_filter_keeps_cursor_when_row_survives(monkeypatch, tmp_path):
    state = SimpleNamespace(
        connections=[_connection("conn-1", "alpha.test"), _connection("conn-2", "beta.test")]
    )

    class FakeQueryService:
        def __init__(self, paths):
            self.paths = paths

        def list_connections(self):
            return state.connections

    monkeypatch.setattr(connections_module, "QueryService", FakeQueryService)
    paths = AppPaths(tmp_path / "config", tmp_path / "data", tmp_path / "state")

    async def run_case():
        app = _ScreenApp(ConnectionsScreen(paths))
        async with app.run_test(size=(100, 30)) as pilot:
            await _wait_for(lambda: app.query_one("#connections-table", DataTable).row_count == 2, pilot)
            screen = app.query_one(ConnectionsScreen)
            table = app.query_one("#connections-table", DataTable)
            table.move_cursor(row=1, animate=False)

            filter_input = app.query_one("#connections-filter")
            filter_input.value = "beta"
            await pilot.pause(0.1)

            assert table.row_count == 1
            assert screen._selected_connection().id == "conn-2"

    asyncio.run(run_case())


def test_providers_refresh_keeps_cursor_on_same_provider(monkeypatch, tmp_path):
    def provider(name: str, count: int) -> ProviderEntry:
        return ProviderEntry(name, "HTTP", "HTTP", count, "2026-05-31T12:00:00Z")

    state = SimpleNamespace(providers=[provider("alpha", 1), provider("beta", 2), provider("gamma", 3)])

    class FakeQueryService:
        def __init__(self, paths):
            self.paths = paths

        def list_proxy_providers(self):
            return state.providers

    monkeypatch.setattr(providers_module, "QueryService", FakeQueryService)
    paths = AppPaths(tmp_path / "config", tmp_path / "data", tmp_path / "state")

    async def run_case():
        app = _ScreenApp(ProvidersScreen(paths))
        async with app.run_test(size=(100, 30)) as pilot:
            await _wait_for(lambda: app.query_one("#providers-table", DataTable).row_count == 3, pilot)
            screen = app.query_one(ProvidersScreen)
            table = app.query_one("#providers-table", DataTable)
            table.move_cursor(row=1, animate=False)
            assert screen._selected_provider().name == "beta"

            # 删除光标上方的 alpha，更新 gamma 的节点数
            state.providers = [provider("beta", 2), provider("gamma", 9)]
            screen.refresh_data()
            await _wait_for(lambda: table.row_count == 2, pilot)
            await pilot.pause(0.1)

            assert screen._selected_provider().name == "beta"
            assert table.cursor_row == 0
            assert table.get_row("gamma")[3] == "9"

    asyncio.run(run_case())
