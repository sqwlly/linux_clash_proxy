from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widget import Widget
from textual.widgets import Button, Label

from ...api import APIUnavailableError
from ...backend.models import ConnectionEntry
from ...config import AppPaths
from ...services.query import QueryService
from ..widgets import NavigationDataTable as DataTable


class ConnectionsScreen(Widget):
    BINDINGS = [
        Binding("r", "refresh_data", "Refresh"),
        Binding("x", "close_selected", "Close"),
        Binding("a", "close_all", "Close All"),
    ]

    def __init__(self, paths: AppPaths, **kwargs):
        super().__init__(**kwargs)
        self.paths = paths
        self._connections: list[ConnectionEntry] = []
        self._confirm_close_all = False

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label("Connections", classes="page-title")
            with Vertical(classes="panel output-panel"):
                yield Label("Active Connections", classes="panel-title")
                yield Label("─", id="connections-status", classes="status-strip")
                yield DataTable(id="connections-table")
                with Horizontal(classes="toolbar"):
                    yield Button("Close", id="btn-close-connection", classes="action-button danger-button")
                    yield Button("Close All", id="btn-close-all-connections", classes="action-button danger-button")
                    yield Button("Refresh", id="btn-refresh-connections", classes="action-button primary-button")
                yield Label("up/down: move  x: close selected  a: close all  r: refresh", id="connections-action-status", classes="action-status")

    def on_mount(self) -> None:
        table = self.query_one("#connections-table", DataTable)
        table.add_columns("Host", "Rule", "Proxy", "Process", "Up", "Down")
        table.cursor_type = "row"
        table.show_header = True
        self.call_later(self.refresh_data)

    def refresh_data(self) -> None:
        self._confirm_close_all = False
        status = self.query_one("#connections-status", Label)
        table = self.query_one("#connections-table", DataTable)
        table.clear()

        try:
            service = QueryService(self.paths)
            self._connections = service.list_connections()
            status.update(f"[#a3e635]● {len(self._connections)} active[/]")

            if not self._connections:
                table.add_row("[#8b98aa]No active connections[/]", "─", "─", "─", "─", "─")
                return

            for connection in self._connections:
                table.add_row(
                    connection.host,
                    connection.rule,
                    " → ".join(connection.proxy_chain) or "─",
                    connection.process,
                    self._format_bytes(connection.upload),
                    self._format_bytes(connection.download),
                    key=connection.id or f"connection-{len(table.rows)}",
                )
        except APIUnavailableError:
            self._connections = []
            status.update("[#fb7185]○ API unavailable[/]")
            table.add_row("[#fb7185]Mihomo API unavailable[/]", "─", "─", "─", "─", "─")
        except Exception as e:
            self._connections = []
            status.update(f"[#fb7185]Error: {e}[/]")
            table.add_row(f"Error: {e}", "─", "─", "─", "─", "─")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-close-connection":
            self.action_close_selected()
        elif event.button.id == "btn-close-all-connections":
            self.action_close_all()
        elif event.button.id == "btn-refresh-connections":
            self.refresh_data()

    def action_close_selected(self) -> None:
        connection = self._selected_connection()
        status = self.query_one("#connections-action-status", Label)
        if connection is None or not connection.id:
            status.update("[#f6c177]No closeable connection selected[/]")
            return

        try:
            QueryService(self.paths).close_connection(connection.id)
            status.update(f"[#a3e635]Closed: {connection.host}[/]")
            self.refresh_data()
        except APIUnavailableError:
            status.update("[#fb7185]API unavailable[/]")
        except Exception as e:
            status.update(f"[#fb7185]Close failed: {e}[/]")

    def action_close_all(self) -> None:
        status = self.query_one("#connections-action-status", Label)
        if not self._connections:
            status.update("[#f6c177]No active connections[/]")
            return

        if not self._confirm_close_all:
            self._confirm_close_all = True
            status.update("[#f6c177]Press Close All again to confirm[/]")
            return

        try:
            QueryService(self.paths).close_all_connections()
            status.update("[#a3e635]Closed all connections[/]")
            self.refresh_data()
        except APIUnavailableError:
            status.update("[#fb7185]API unavailable[/]")
        except Exception as e:
            status.update(f"[#fb7185]Close all failed: {e}[/]")

    def _selected_connection(self) -> ConnectionEntry | None:
        table = self.query_one("#connections-table", DataTable)
        if table.cursor_row is None or table.cursor_row >= len(self._connections):
            return None
        return self._connections[table.cursor_row]

    def _format_bytes(self, value: int) -> str:
        if value <= 0:
            return "0 B"
        units = ["B", "KB", "MB", "GB", "TB"]
        index = 0
        number = float(value)
        while number >= 1024 and index < len(units) - 1:
            number /= 1024
            index += 1
        return f"{number:.1f} {units[index]}"
