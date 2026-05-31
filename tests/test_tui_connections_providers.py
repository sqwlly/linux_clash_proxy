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
