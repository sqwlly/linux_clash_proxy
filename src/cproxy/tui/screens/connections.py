from __future__ import annotations

from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.coordinate import Coordinate
from textual.widget import Widget
from textual.widgets import Button, Label

from ...api import APIUnavailableError
from ...backend.models import ConnectionEntry
from ...config import AppPaths
from ...services.query import QueryService
from ..widgets import NavigationDataTable as DataTable
from ..widgets import NavigationInput as Input

_PLACEHOLDER_ROW_KEY = "__placeholder__"


def _connection_row_key(connection: ConnectionEntry, index: int) -> str:
    return connection.id or f"connection-{index}"


def _cursor_row_key(table: DataTable) -> str | None:
    """返回当前光标所在行的 row key（字符串），无有效光标时返回 None。"""
    if table.row_count == 0 or table.cursor_row is None or table.cursor_row >= table.row_count:
        return None
    try:
        return table.coordinate_to_cell_key(Coordinate(table.cursor_row, 0))[0].value
    except Exception:
        return None


def _sync_table_rows(table: DataTable, column_keys, desired: list[tuple[str, tuple[str, ...]]]) -> None:
    """按 row key 增量同步表格：更新变化单元格、增删行，避免 clear+重建造成的闪烁与光标丢失。"""
    desired_keys = {key for key, _ in desired}
    for row_key in list(table.rows.keys()):
        if row_key.value not in desired_keys:
            table.remove_row(row_key)
    for key, cells in desired:
        if key in table.rows:
            existing = table.get_row(key)
            for column_key, old, new in zip(column_keys, existing, cells):
                if old != new:
                    table.update_cell(key, column_key, new)
        else:
            table.add_row(*cells, key=key)


class ConnectionsScreen(Widget):
    BINDINGS = [
        Binding("r", "refresh_data", "刷新"),
        Binding("/", "focus_filter", "筛选"),
        Binding("x", "close_selected", "断开"),
        Binding("a", "close_all", "全部断开"),
    ]

    def __init__(self, paths: AppPaths, **kwargs):
        super().__init__(**kwargs)
        self.paths = paths
        self._connections: list[ConnectionEntry] = []
        self._visible_connections: list[ConnectionEntry] = []
        self._confirm_close_all = False
        self._filter_text = ""

    def compose(self) -> ComposeResult:
        with Vertical():
            with Vertical(classes="panel output-panel"):
                with Horizontal(classes="panel-header"):
                    yield Label("活动连接", classes="panel-title")
                    yield Label("─", id="connections-status", classes="status-strip")
                yield Input(placeholder="筛选主机 / 代理链路 / 进程…", id="connections-filter", classes="table-filter")
                yield DataTable(id="connections-table")
                yield Label("─", id="connection-detail", classes="current-info")
                with Horizontal(classes="toolbar"):
                    yield Button("断开", id="btn-close-connection", classes="action-button danger-button")
                    yield Button("全部断开", id="btn-close-all-connections", classes="action-button danger-button")
                    yield Button("刷新", id="btn-refresh-connections", classes="action-button muted-button")
                yield Label(
                    "↑↓ 移动  x 断开  a 全部断开  r 刷新",
                    id="connections-action-status", classes="action-status",
                )

    def on_mount(self) -> None:
        table = self.query_one("#connections-table", DataTable)
        self._table_columns = table.add_columns("主机", "规则", "代理链路", "进程", "上传", "下载")
        table.cursor_type = "row"
        table.show_header = True
        if not list(self.app.query("#main-tabs")):
            self.call_later(self.refresh_data)

    def refresh_data(self) -> None:
        self._confirm_close_all = False
        status = self.query_one("#connections-status", Label)
        status.update("[#f6c177]刷新中…[/]")
        self._load_connections()

    @work(thread=True, exclusive=True, group="connections-refresh")
    def _load_connections(self) -> None:
        try:
            connections = QueryService(self.paths).list_connections()
            self.app.call_from_thread(self._apply_connections, connections, None)
        except Exception as exc:
            self.app.call_from_thread(self._apply_connections, [], exc)

    def _apply_connections(self, connections: list[ConnectionEntry], error: Exception | None) -> None:
        status = self.query_one("#connections-status", Label)
        if error is not None:
            self._connections = []
            self._visible_connections = []
            if isinstance(error, APIUnavailableError):
                status.update("[#fb7185]○ API 不可访问[/]")
                placeholder = ("[#fb7185]Mihomo API 不可访问[/]", "─", "─", "─", "─", "─")
            else:
                status.update(f"[#fb7185]错误: {error}[/]")
                placeholder = (f"错误: {error}", "─", "─", "─", "─", "─")
            table = self.query_one("#connections-table", DataTable)
            _sync_table_rows(table, self._table_columns, [(_PLACEHOLDER_ROW_KEY, placeholder)])
            self._update_connection_detail(None)
            return

        self._connections = connections
        self._render_visible_connection_rows()

    def _render_visible_connection_rows(self) -> None:
        table = self.query_one("#connections-table", DataTable)
        previous_key = _cursor_row_key(table)
        self._visible_connections = self._filtered_connections()
        self._update_connection_status()

        if not self._visible_connections:
            placeholder = (f"[#8b98aa]{self._empty_state_text()}[/]", "─", "─", "─", "─", "─")
            _sync_table_rows(table, self._table_columns, [(_PLACEHOLDER_ROW_KEY, placeholder)])
            self._update_connection_detail(None)
            return

        desired: list[tuple[str, tuple[str, ...]]] = []
        by_key: dict[str, ConnectionEntry] = {}
        for index, connection in enumerate(self._visible_connections):
            key = _connection_row_key(connection, index)
            by_key[key] = connection
            desired.append(
                (
                    key,
                    (
                        self._compact_text(connection.host, 40),
                        connection.rule,
                        self._compact_text(" -> ".join(connection.proxy_chain) or "─", 34),
                        self._compact_text(connection.process, 24),
                        self._format_bytes(connection.upload),
                        self._format_bytes(connection.download),
                    ),
                )
            )
        _sync_table_rows(table, self._table_columns, desired)

        # 表格保持插入序（增量同步只追加新行），_visible_connections 按表格行序对齐，
        # 保证 cursor_row 索引映射不变
        self._visible_connections = [
            connection for row_key in table.rows if (connection := by_key.get(row_key.value)) is not None
        ]
        self._restore_connection_cursor(previous_key)
        self._update_connection_detail(self._selected_connection())

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-close-connection":
            self.action_close_selected()
        elif event.button.id == "btn-close-all-connections":
            self.action_close_all()
        elif event.button.id == "btn-refresh-connections":
            self.refresh_data()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id != "connections-filter":
            return
        self._filter_text = event.value.strip().lower()
        self._render_visible_connection_rows()

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.data_table.id == "connections-table":
            self._update_connection_detail(self._selected_connection())

    def action_close_selected(self) -> None:
        connection = self._selected_connection()
        status = self.query_one("#connections-action-status", Label)
        if connection is None or not connection.id:
            status.update("[#f6c177]尚未选择可断开的连接[/]")
            return
        status.update(f"[#f6c177]正在断开: {connection.host}…[/]")
        self._close_connections(connection.id, connection.host)

    def action_close_all(self) -> None:
        status = self.query_one("#connections-action-status", Label)
        if not self._connections:
            status.update("[#f6c177]没有活动连接[/]")
            return

        if not self._confirm_close_all:
            self._confirm_close_all = True
            if self._filter_text:
                status.update("[#f6c177]筛选条件会被忽略；请再次触发以断开全部活动连接[/]")
            else:
                status.update("[#f6c177]请再次触发“断开全部连接”确认[/]")
            return
        status.update("[#f6c177]正在断开全部连接…[/]")
        self._close_connections(None, "全部连接")

    @work(thread=True, exclusive=True, group="connections-action")
    def _close_connections(self, connection_id: str | None, label: str) -> None:
        try:
            service = QueryService(self.paths)
            if connection_id is None:
                service.close_all_connections()
            else:
                service.close_connection(connection_id)
            self.app.call_from_thread(self._finish_close, label, None)
        except Exception as exc:
            self.app.call_from_thread(self._finish_close, label, exc)

    def _finish_close(self, label: str, error: Exception | None) -> None:
        status = self.query_one("#connections-action-status", Label)
        if error is None:
            status.update(f"[#a3e635]已断开: {label}[/]")
            self.refresh_data()
        elif isinstance(error, APIUnavailableError):
            status.update("[#fb7185]API 不可访问[/]")
        else:
            status.update(f"[#fb7185]断开失败: {error}[/]")

    def _selected_connection(self) -> ConnectionEntry | None:
        table = self.query_one("#connections-table", DataTable)
        if table.cursor_row is None or table.cursor_row >= len(self._visible_connections):
            return None
        return self._visible_connections[table.cursor_row]

    def _restore_connection_cursor(self, row_key: str | None) -> None:
        table = self.query_one("#connections-table", DataTable)
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

    def _filtered_connections(self) -> list[ConnectionEntry]:
        if not self._filter_text:
            return list(self._connections)
        return [
            connection
            for connection in self._connections
            if self._filter_text
            in " ".join(
                [
                    connection.host,
                    connection.rule,
                    " ".join(connection.proxy_chain),
                    connection.process,
                    connection.id or "",
                ]
            ).lower()
        ]

    def _render_connection_rows(self) -> None:
        self._render_visible_connection_rows()

    def _update_connection_detail(self, connection: ConnectionEntry | None) -> None:
        detail = self.query_one("#connection-detail", Label)
        if connection is None:
            detail.update("[#8b98aa]选择连接后查看完整主机、进程、代理链路和 ID[/]")
            return
        detail.update(
            "\n".join(
                [
                    f"[#8b98aa]主机[/] {connection.host}",
                    f"[#8b98aa]进程[/] {connection.process or '─'}",
                    f"[#8b98aa]链路[/] {' -> '.join(connection.proxy_chain) or '─'}",
                    f"[#8b98aa]ID[/] {connection.id or '─'}",
                ]
            )
        )

    def action_focus_filter(self) -> None:
        self.query_one("#connections-filter", Input).focus()

    def _update_connection_status(self) -> None:
        status = self.query_one("#connections-status", Label)
        if self._filter_text:
            status.update(f"[#a3e635]● {len(self._visible_connections)} / {len(self._connections)} 个活动连接[/]")
        else:
            status.update(f"[#a3e635]● {len(self._connections)} 个活动连接[/]")

    def _empty_state_text(self) -> str:
        if self._connections and self._filter_text:
            return "没有匹配的连接"
        return "没有活动连接"

    def _compact_text(self, value: str, limit: int) -> str:
        if len(value) <= limit:
            return value
        return value[: limit - 1] + "…"

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
