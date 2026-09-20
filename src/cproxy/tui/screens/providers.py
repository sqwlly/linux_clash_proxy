from __future__ import annotations

from textual import work
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
        Binding("r", "refresh_data", "刷新"),
        Binding("u", "update_provider", "更新"),
    ]

    def __init__(self, paths: AppPaths, **kwargs):
        super().__init__(**kwargs)
        self.paths = paths
        self._providers: list[ProviderEntry] = []

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label("代理提供方", classes="page-title")
            with Vertical(classes="panel output-panel"):
                yield Label("代理 Provider", classes="panel-title")
                yield Label("─", id="providers-status", classes="status-strip")
                yield DataTable(id="providers-table")
                with Horizontal(classes="toolbar"):
                    yield Button("更新选中项", id="btn-update-provider", classes="action-button primary-button")
                    yield Button("刷新", id="btn-refresh-providers", classes="action-button muted-button")
                yield Label(
                    "↑↓ 移动  u 更新选中项  r 刷新",
                    id="providers-action-status", classes="action-status",
                )

    def on_mount(self) -> None:
        table = self.query_one("#providers-table", DataTable)
        table.add_columns("名称", "类型", "载体", "节点数", "更新时间")
        table.cursor_type = "row"
        table.show_header = True
        if not list(self.app.query("#main-tabs")):
            self.call_later(self.refresh_data)

    def refresh_data(self) -> None:
        status = self.query_one("#providers-status", Label)
        status.update("[#f6c177]刷新中…[/]")
        self._load_providers()

    @work(thread=True, exclusive=True, group="providers-refresh")
    def _load_providers(self) -> None:
        try:
            providers = QueryService(self.paths).list_proxy_providers()
            self.app.call_from_thread(self._apply_providers, providers, None)
        except Exception as exc:
            self.app.call_from_thread(self._apply_providers, [], exc)

    def _apply_providers(self, providers: list[ProviderEntry], error: Exception | None) -> None:
        status = self.query_one("#providers-status", Label)
        table = self.query_one("#providers-table", DataTable)
        previous_provider = self._selected_provider_name()
        table.clear()
        if error is not None:
            self._providers = []
            if isinstance(error, APIUnavailableError):
                status.update("[#fb7185]○ API 不可访问[/]")
                table.add_row("[#fb7185]Mihomo API 不可访问[/]", "─", "─", "0", "─")
            else:
                status.update(f"[#fb7185]错误: {error}[/]")
                table.add_row(f"错误: {error}", "─", "─", "0", "─")
            return

        self._providers = providers
        status.update(f"[#a3e635]● {len(self._providers)} 个提供方[/]")

        if not self._providers:
            table.add_row("[#8b98aa]没有代理提供方[/]", "─", "─", "0", "─")
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
        self._move_provider_cursor(previous_provider)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-update-provider":
            self.action_update_provider()
        elif event.button.id == "btn-refresh-providers":
            self.refresh_data()

    def action_update_provider(self) -> None:
        provider = self._selected_provider()
        status = self.query_one("#providers-action-status", Label)
        if provider is None:
            status.update("[#f6c177]尚未选择提供方[/]")
            return
        status.update(f"[#f6c177]正在更新: {provider.name}…[/]")
        self._update_provider(provider.name)

    @work(thread=True, exclusive=True, group="provider-action")
    def _update_provider(self, provider_name: str) -> None:
        try:
            QueryService(self.paths).update_proxy_provider(provider_name)
            self.app.call_from_thread(self._finish_provider_update, provider_name, None)
        except Exception as exc:
            self.app.call_from_thread(self._finish_provider_update, provider_name, exc)

    def _finish_provider_update(self, provider_name: str, error: Exception | None) -> None:
        status = self.query_one("#providers-action-status", Label)
        if error is None:
            status.update(f"[#a3e635]已更新提供方: {provider_name}[/]")
            self.refresh_data()
        elif isinstance(error, APIUnavailableError):
            status.update("[#fb7185]API 不可访问[/]")
        else:
            status.update(f"[#fb7185]更新失败: {error}[/]")

    def _selected_provider(self) -> ProviderEntry | None:
        table = self.query_one("#providers-table", DataTable)
        if table.cursor_row is None or table.cursor_row >= len(self._providers):
            return None
        return self._providers[table.cursor_row]

    def _selected_provider_name(self) -> str | None:
        provider = self._selected_provider()
        return provider.name if provider else None

    def _move_provider_cursor(self, provider_name: str | None) -> None:
        if not provider_name:
            return
        for row_index, provider in enumerate(self._providers):
            if provider.name == provider_name:
                self.query_one("#providers-table", DataTable).move_cursor(row=row_index, animate=False)
                return
