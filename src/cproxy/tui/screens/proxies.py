from __future__ import annotations

from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widget import Widget
from textual.widgets import Button, Label

from ...api import APIUnavailableError
from ...backend.models import ProxyGroup
from ...config import AppPaths
from ...process import restart_process
from ...runtime import render_runtime
from ...services.query import QueryService
from ..widgets import NavigationDataTable as DataTable


class ProxiesScreen(Widget):
    BINDINGS = [
        Binding("enter", "activate_row", "打开/选择", priority=True),
        Binding("right", "focus_nodes", "节点", priority=True),
        Binding("left", "focus_groups", "分组", priority=True),
        Binding("escape", "back", "返回", priority=True),
        Binding("s", "select_node", "切换"),
        Binding("t", "test_delay", "测速"),
        Binding("r", "refresh_data", "刷新"),
        Binding("g", "focus_groups", "分组"),
        Binding("n", "focus_nodes", "节点"),
    ]

    def __init__(self, paths: AppPaths, **kwargs):
        super().__init__(**kwargs)
        self.paths = paths
        self._groups: list[ProxyGroup] = []
        self._current_group: ProxyGroup | None = None
        self._api_available = False

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label("节点与分组", classes="page-title")
            with Horizontal(classes="workbench-row"):
                with Vertical(classes="proxy-group-card split-sidebar proxy-sidebar"):
                    yield Label("分组", classes="proxy-group-title")
                    yield DataTable(id="groups-table")
                with Vertical(classes="proxy-group-card split-main proxy-main"):
                    yield Label("节点", classes="proxy-group-title")
                    yield Label("─", id="current-node", classes="node-current")
                    yield Label("─", id="api-status", classes="status-strip")
                    yield DataTable(id="nodes-table")
                    with Horizontal(classes="toolbar"):
                        yield Button("切换", id="btn-switch-node", classes="action-button success-button")
                        yield Button("测速", id="btn-test-delay", classes="action-button primary-button")
                        yield Button("刷新", id="btn-refresh-proxies", classes="action-button muted-button")
                        yield Button("重启", id="btn-restart-proxy", classes="action-button muted-button")
                    yield Label(
                        "↑↓ 移动  ←/Esc 返回分组  → 进入节点  Enter/s 切换",
                        id="proxy-action-status", classes="action-status",
                    )

    def on_mount(self) -> None:
        self._init_tables()
        if not list(self.app.query("#main-tabs")):
            self.call_later(self.refresh_data)

    def _init_tables(self) -> None:
        groups_table = self.query_one("#groups-table", DataTable)
        groups_table.add_columns("名称", "类型", "当前选择")
        groups_table.cursor_type = "row"
        groups_table.show_header = True
        groups_table.navigation_next_handler = self.action_focus_nodes

        nodes_table = self.query_one("#nodes-table", DataTable)
        nodes_table.add_columns("节点", "延迟")
        nodes_table.cursor_type = "row"
        nodes_table.show_header = True
        nodes_table.navigation_previous_handler = self.action_focus_groups


    def refresh_data(self) -> None:
        self.query_one("#api-status", Label).update("[#f6c177]刷新中…[/]")
        self._load_groups()

    @work(thread=True, exclusive=True, group="proxies-refresh")
    def _load_groups(self) -> None:
        try:
            service = QueryService(self.paths)
            context = service.load_context(require_api=False)
            self.app.call_from_thread(self._apply_groups, context, None)
        except Exception as exc:
            self.app.call_from_thread(self._apply_groups, None, str(exc))

    def _apply_groups(self, context, error: str | None) -> None:
        groups_table = self.query_one("#groups-table", DataTable)
        if error is not None or context is None:
            self._api_available = False
            self._update_api_status()
            groups_table.clear()
            groups_table.add_row(f"错误: {error}", "─", "─")
            return

        self._api_available = context.api_available
        self._groups = list(context.groups.values())
        self._update_api_status()

        groups_table.clear()

        rendered_group_count = 0
        rendered_groups: list[ProxyGroup] = []
        for group in self._groups:
            group_type = str(group.type).lower()
            if group_type in {"selector", "select", "fallback", "url-test", "load-balance"}:
                groups_table.add_row(group.name, group.type, group.current or "─", key=group.name)
                rendered_group_count += 1
                rendered_groups.append(group)

        if not rendered_group_count:
            groups_table.add_row("[#8b98aa]没有可显示的代理组[/]", "─", "─")
            self._current_group = None
            self._update_nodes_table()
            return

        previous_group_name = self._current_group.name if self._current_group else None
        chosen = next((group for group in rendered_groups if group.name == previous_group_name), None)
        if chosen is None:
            selectable = [group for group in rendered_groups if str(group.type).lower() in {"selector", "select"}]
            fallback = [
                group for group in rendered_groups if str(group.type).lower() in {"fallback", "url-test", "load-balance"}
            ]
            chosen = selectable[0] if selectable else fallback[0] if fallback else None
        if chosen:
            self._current_group = chosen
            self._update_nodes_table()
            groups_table.move_cursor(row=rendered_groups.index(chosen), animate=False)

    def _update_api_status(self) -> None:
        label = self.query_one("#api-status", Label)
        if self._api_available:
            label.update("[#a3e635]● API 已连接[/]")
        else:
            label.update("[#f6c177]○ 仅显示运行配置；切换前请启动或重启代理[/]")

    def _update_nodes_table(self) -> None:
        nodes_table = self.query_one("#nodes-table", DataTable)
        previous_node = self._current_node_key(nodes_table)
        nodes_table.clear()

        current_label = self.query_one("#current-node", Label)

        if not self._current_group:
            current_label.update("[#8b98aa]尚未选择分组[/]")
            nodes_table.add_row("[#8b98aa]选择分组后查看节点[/]", "─")
            return

        current_label.update(f"[#a3e635]● {self._current_group.current}[/]")

        for node in self._current_group.candidates:
            is_current = node == self._current_group.current
            delay = "─"
            if is_current and self._current_group.delay:
                delay = f"{self._current_group.delay}ms"
            prefix = "[#a3e635]●[/] " if is_current else "  "
            nodes_table.add_row(f"{prefix}{node}", delay, key=node)

        preferred_node = previous_node or self._current_group.current
        self._move_nodes_cursor(preferred_node)

    def _set_current_group(self, group_name: str, focus_nodes: bool) -> None:
        for group in self._groups:
            if group.name == group_name:
                self._current_group = group
                self._update_nodes_table()
                if focus_nodes:
                    self.action_focus_nodes()
                return

    def _current_node_key(self, table: DataTable) -> str | None:
        if table.cursor_row is None or table.cursor_row >= len(table.ordered_rows):
            return None
        return str(table.ordered_rows[table.cursor_row].key.value)

    def _move_nodes_cursor(self, node_name: str | None) -> None:
        if not self._current_group or not node_name:
            return
        try:
            row_index = self._current_group.candidates.index(node_name)
        except ValueError:
            return
        self.query_one("#nodes-table", DataTable).move_cursor(row=row_index, animate=False)

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.data_table.id != "groups-table":
            return
        group_name = str(event.row_key.value)
        self.query_one("#proxy-action-status", Label).update(f"[#8b98aa]已选分组: {group_name}[/]")

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if event.data_table.id == "groups-table":
            self._set_current_group(str(event.row_key.value), focus_nodes=True)
        elif event.data_table.id == "nodes-table":
            self.action_select_node()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-switch-node":
            self.action_select_node()
        elif event.button.id == "btn-test-delay":
            self.action_test_delay()
        elif event.button.id == "btn-refresh-proxies":
            self.refresh_data()
        elif event.button.id == "btn-restart-proxy":
            self.action_restart_proxy()

    def action_focus_groups(self) -> None:
        self.query_one("#groups-table", DataTable).focus()

    def action_focus_nodes(self) -> None:
        self._set_current_group_from_cursor(focus_nodes=False)
        self.query_one("#nodes-table", DataTable).focus()

    def action_activate_row(self) -> None:
        focused = getattr(self.app, "focused", None)
        if isinstance(focused, DataTable) and focused.id == "nodes-table":
            self.action_select_node()
        elif isinstance(focused, DataTable) and focused.id == "groups-table":
            self.action_focus_nodes()
        else:
            self.action_focus_groups()

    def _set_current_group_from_cursor(self, focus_nodes: bool) -> None:
        groups_table = self.query_one("#groups-table", DataTable)
        if groups_table.cursor_row is None or groups_table.cursor_row >= len(groups_table.ordered_rows):
            return
        row = groups_table.ordered_rows[groups_table.cursor_row]
        self._set_current_group(str(row.key.value), focus_nodes=focus_nodes)

    def action_back(self) -> None:
        focused = getattr(self.app, "focused", None)
        if not (isinstance(focused, DataTable) and focused.id == "groups-table"):
            self.action_focus_groups()
            self.query_one("#proxy-action-status", Label).update("[#8b98aa]已返回分组列表[/]")
        else:
            for tabbed in self.app.query("#main-tabs"):
                for child in tabbed.walk_children():
                    if child.__class__.__name__ == "ContentTabs":
                        child.focus()
                        return
                tabbed.focus()
                return

    def action_select_node(self) -> None:
        if not self._current_group:
            return

        nodes_table = self.query_one("#nodes-table", DataTable)
        if nodes_table.cursor_row is None:
            self.query_one("#proxy-action-status", Label).update("[#f6c177]尚未选择节点[/]")
            return

        if not self._api_available:
            self.query_one("#proxy-action-status", Label).update(
                "[#f6c177]API 不可访问；请点击重启或执行 cproxy restart[/]"
            )
            return

        row = nodes_table.ordered_rows[nodes_table.cursor_row]
        node_name = str(row.key.value)

        if str(self._current_group.type).lower() not in {"selector", "select"}:
            self.notify(f"分组 [{self._current_group.name}] 不支持手动切换", severity="warning")
            return

        group_name = self._current_group.name
        self.query_one("#proxy-action-status", Label).update(f"[#f6c177]正在切换 {group_name} → {node_name}…[/]")
        self._switch_node(group_name, node_name)

    @work(thread=True, exclusive=True, group="proxy-action")
    def _switch_node(self, group_name: str, node_name: str) -> None:
        try:
            QueryService(self.paths).switch_group(group_name, node_name)
            self.app.call_from_thread(self._finish_switch_node, group_name, node_name, None)
        except Exception as exc:
            self.app.call_from_thread(self._finish_switch_node, group_name, node_name, exc)

    def _finish_switch_node(self, group_name: str, node_name: str, error: Exception | None) -> None:
        status = self.query_one("#proxy-action-status", Label)
        if error is None:
            status.update(f"[#a3e635]已切换 {group_name} → {node_name}[/]")
            self.notify(f"已切换: {group_name} → {node_name}", severity="information")
            self.refresh_data()
        elif isinstance(error, APIUnavailableError):
            status.update("[#fb7185]API 不可访问[/]")
            self.notify("API 不可访问", severity="error")
        else:
            status.update(f"[#fb7185]切换失败: {error}[/]")
            self.notify(f"切换失败: {error}", severity="error")

    def action_restart_proxy(self) -> None:
        self.query_one("#proxy-action-status", Label).update("[#f6c177]正在生成配置并重启…[/]")
        self._restart_proxy()

    @work(thread=True, exclusive=True, group="proxy-action")
    def _restart_proxy(self) -> None:
        try:
            runtime_path = render_runtime(self.paths)
            pid = restart_process(self.paths)
            self.app.call_from_thread(self._finish_restart, runtime_path, pid, None)
        except Exception as exc:
            self.app.call_from_thread(self._finish_restart, None, None, exc)

    def _finish_restart(self, runtime_path, pid: int | None, error: Exception | None) -> None:
        status = self.query_one("#proxy-action-status", Label)
        if error is None:
            status.update(f"[#a3e635]已重启 PID {pid}；运行配置 {runtime_path}[/]")
            self.refresh_data()
        else:
            status.update(f"[#fb7185]重启失败: {error}[/]")
            self.notify(f"重启失败: {error}", severity="error")

    def action_test_delay(self) -> None:
        if not self._current_group:
            return
        group_name = self._current_group.name
        current_name = self._current_group.current
        self.query_one("#proxy-action-status", Label).update(f"[#f6c177]正在测速: {group_name}…[/]")
        self._test_delay(group_name, current_name)

    @work(thread=True, exclusive=True, group="proxy-action")
    def _test_delay(self, group_name: str, current_name: str | None) -> None:
        try:
            from ...diagnostics import test_group
            report = test_group(self.paths, group_name)
            self.app.call_from_thread(self._finish_test_delay, group_name, current_name, report, None)
        except Exception as exc:
            self.app.call_from_thread(self._finish_test_delay, group_name, current_name, None, exc)

    def _finish_test_delay(self, group_name: str, current_name: str | None, report, error: Exception | None) -> None:
        status = self.query_one("#proxy-action-status", Label)
        if error is not None:
            message = "API 不可访问" if isinstance(error, APIUnavailableError) else f"测速失败: {error}"
            status.update(f"[#fb7185]{message}[/]")
            self.notify(message, severity="error")
            return

        nodes_table = self.query_one("#nodes-table", DataTable)
        nodes_table.clear()
        self.query_one("#current-node", Label).update(f"[#a3e635]● {current_name}[/]")
        for result in report.results:
            is_current = result.name == current_name
            prefix = "[#a3e635]●[/] " if is_current else "  "
            if result.ok and result.delay:
                if result.delay < 200:
                    delay = f"[#a3e635]{result.delay}ms[/]"
                elif result.delay < 500:
                    delay = f"[#f6c177]{result.delay}ms[/]"
                else:
                    delay = f"[#fb7185]{result.delay}ms[/]"
            else:
                delay = "[#fb7185]失败[/]"
            nodes_table.add_row(f"{prefix}{result.name}", delay, key=result.name)
        status.update(f"[#a3e635]测速完成: {group_name}[/]")
        self.notify(f"测速完成: {group_name}", severity="information")
