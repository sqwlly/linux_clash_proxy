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
from .connections import _PLACEHOLDER_ROW_KEY, _cursor_row_key, _sync_table_rows


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
            with Vertical(classes="panel output-panel"):
                with Horizontal(classes="panel-header"):
                    yield Label("代理提供方", classes="panel-title")
                    yield Label("─", id="providers-status", classes="status-strip")
                yield DataTable(id="providers-table")
                with Horizontal(classes="toolbar"):
                    yield Button("更新", id="btn-update-provider", classes="action-button primary-button")
                    yield Button("刷新", id="btn-refresh-providers", classes="action-button muted-button")
                yield Label(
                    "↑↓ 移动  u 更新  r 刷新",
                    id="providers-action-status", classes="action-status",
                )

    def on_mount(self) -> None:
        table = self.query_one("#providers-table", DataTable)
        self._table_columns = table.add_columns("名称", "类型", "载体", "节点数", "更新时间")
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
        previous_key = _cursor_row_key(table)
        if error is not None:
            self._providers = []
            if isinstance(error, APIUnavailableError):
                status.update("[#fb7185]○ API 不可访问[/]")
                placeholder = ("[#fb7185]Mihomo API 不可访问[/]", "─", "─", "0", "─")
            else:
                status.update(f"[#fb7185]错误: {error}[/]")
                placeholder = (f"错误: {error}", "─", "─", "0", "─")
            _sync_table_rows(table, self._table_columns, [(_PLACEHOLDER_ROW_KEY, placeholder)])
            return

        self._providers = providers
        status.update(f"[#a3e635]● {len(self._providers)} 个提供方[/]")

        if not self._providers:
            _sync_table_rows(
                table,
                self._table_columns,
                [(_PLACEHOLDER_ROW_KEY, ("[#8b98aa]没有代理提供方[/]", "─", "─", "0", "─"))],
            )
            return

        desired = [
            (
                provider.name,
                (provider.name, provider.type, provider.vehicle, str(provider.proxy_count), provider.updated_at),
            )
            for provider in self._providers
        ]
        _sync_table_rows(table, self._table_columns, desired)

        # 表格保持插入序，_providers 按表格行序对齐，保证 cursor_row 索引映射不变
        by_key = {provider.name: provider for provider in self._providers}
        self._providers = [
            provider for row_key in table.rows if (provider := by_key.get(row_key.value)) is not None
        ]
        self._restore_provider_cursor(previous_key)

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

    def _restore_provider_cursor(self, row_key: str | None) -> None:
        table = self.query_one("#providers-table", DataTable)
        if table.row_count == 0:
            return
        if row_key:
            try:
                table.move_cursor(row=table.get_row_index(row_key), animate=False)
                return
            except Exception:
                pass
        if table.cursor_row is None or table.cursor_row >= table.row_count:
            table.move_cursor(row=table.row_count - 1, animate=False)
