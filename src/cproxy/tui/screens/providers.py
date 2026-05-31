from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widget import Widget
from textual.widgets import Button, Label

from ...api import APIUnavailableError
from ...backend.models import ProviderEntry
from ...config import AppPaths
from ...services.query import QueryService
from ..widgets import NavigationDataTable as DataTable


class ProvidersScreen(Widget):
    BINDINGS = [
        Binding("r", "refresh_data", "Refresh"),
        Binding("u", "update_provider", "Update"),
    ]

    def __init__(self, paths: AppPaths, **kwargs):
        super().__init__(**kwargs)
        self.paths = paths
        self._providers: list[ProviderEntry] = []

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label("Providers", classes="page-title")
            with Vertical(classes="panel output-panel"):
                yield Label("Proxy Providers", classes="panel-title")
                yield Label("─", id="providers-status", classes="status-strip")
                yield DataTable(id="providers-table")
                with Horizontal(classes="toolbar"):
                    yield Button("Update", id="btn-update-provider", classes="action-button primary-button")
                    yield Button("Refresh", id="btn-refresh-providers", classes="action-button muted-button")
                yield Label("up/down: move  u: update selected  r: refresh", id="providers-action-status", classes="action-status")

    def on_mount(self) -> None:
        table = self.query_one("#providers-table", DataTable)
        table.add_columns("Name", "Type", "Vehicle", "Nodes", "Updated")
        table.cursor_type = "row"
        table.show_header = True
        self.call_later(self.refresh_data)

    def refresh_data(self) -> None:
        status = self.query_one("#providers-status", Label)
        table = self.query_one("#providers-table", DataTable)
        table.clear()

        try:
            service = QueryService(self.paths)
            self._providers = service.list_proxy_providers()
            status.update(f"[#a3e635]● {len(self._providers)} providers[/]")

            if not self._providers:
                table.add_row("[#8b98aa]No proxy providers[/]", "─", "─", "0", "─")
                return

            for provider in self._providers:
                table.add_row(
                    provider.name,
                    provider.type,
                    provider.vehicle,
                    str(provider.proxy_count),
                    provider.updated_at,
                    key=provider.name,
                )
        except APIUnavailableError:
            self._providers = []
            status.update("[#fb7185]○ API unavailable[/]")
            table.add_row("[#fb7185]Mihomo API unavailable[/]", "─", "─", "0", "─")
        except Exception as e:
            self._providers = []
            status.update(f"[#fb7185]Error: {e}[/]")
            table.add_row(f"Error: {e}", "─", "─", "0", "─")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-update-provider":
            self.action_update_provider()
        elif event.button.id == "btn-refresh-providers":
            self.refresh_data()

    def action_update_provider(self) -> None:
        provider = self._selected_provider()
        status = self.query_one("#providers-action-status", Label)
        if provider is None:
            status.update("[#f6c177]No provider selected[/]")
            return

        try:
            QueryService(self.paths).update_proxy_provider(provider.name)
            status.update(f"[#a3e635]Updated provider: {provider.name}[/]")
            self.refresh_data()
        except APIUnavailableError:
            status.update("[#fb7185]API unavailable[/]")
        except Exception as e:
            status.update(f"[#fb7185]Update failed: {e}[/]")

    def _selected_provider(self) -> ProviderEntry | None:
        table = self.query_one("#providers-table", DataTable)
        if table.cursor_row is None or table.cursor_row >= len(self._providers):
            return None
        return self._providers[table.cursor_row]
