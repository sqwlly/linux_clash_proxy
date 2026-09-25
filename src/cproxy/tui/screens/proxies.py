from __future__ import annotations

from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widget import Widget
from textual.widgets import Button, Label

from ...api import APIUnavailableError
from ...backend.models import ProxyGroup
from ...config import AppPaths, read_config
from ...process import restart_process
from ...runtime import render_runtime
from ...services.diagnostics import DiagnosticsService
from ...services.query import QueryService
from ...services.switch_tree import SwitchChoice, build_switch_tree, extra_subscription_names
from ..widgets import NavigationDataTable as DataTable

_PLACEHOLDER_KEY = "__placeholder__"
_ERROR_KEY = "__error__"


def _sync_table_rows(table: DataTable, rows: dict[str, tuple[str, ...]]) -> None:
    """按 row key 增量同步表格内容：更新变化单元格、补充新行、移除消失行。

    相比 clear() 后整表重建，光标位置、滚动偏移与选择状态都得以保留。
    """
    existing = {str(row_key.value): row_key for row_key in table.rows}
    for key in list(existing):
        if key not in rows:
            table.remove_row(existing[key])
    column_keys = [column.key for column in table.ordered_columns]
    for key, cells in rows.items():
        if key in table.rows:
            for column_key, value in zip(column_keys, cells):
                if table.get_cell(key, column_key) != value:
                    table.update_cell(key, column_key, value, update_width=True)
        else:
            table.add_row(*cells, key=key)


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
        self._tree_entries: list[SwitchChoice] = []
        self._current_group: ProxyGroup | None = None
        self._current_entry: SwitchChoice | None = None
        self._drilled: SwitchChoice | None = None
        self._api_available = False

    def compose(self) -> ComposeResult:
        with Vertical():
            with Horizontal(classes="workbench-row"):
                with Vertical(classes="proxy-group-card split-sidebar proxy-sidebar"):
                    yield Label("用途", classes="proxy-group-title")
                    yield DataTable(id="groups-table")
                with Vertical(classes="proxy-group-card split-main proxy-main"):
                    with Horizontal(classes="panel-header"):
                        yield Label("─", id="current-node", classes="node-current")
                        yield Label("─", id="api-status", classes="status-strip")
                    yield DataTable(id="nodes-table")
                    with Horizontal(classes="toolbar"):
                        yield Button("切换", id="btn-switch-node", classes="action-button primary-button")
                        yield Button("测速", id="btn-test-delay", classes="action-button muted-button")
                        yield Button("刷新", id="btn-refresh-proxies", classes="action-button muted-button")
                        yield Button("重启", id="btn-restart-proxy", classes="action-button danger-button")
                    yield Label(
                        "↑↓ 移动  ←/Esc 返回  → 进入  Enter 下钻或切换",
                        id="proxy-action-status", classes="action-status",
                    )

    def on_mount(self) -> None:
        self._init_tables()
        if not list(self.app.query("#main-tabs")):
            self.call_later(self.refresh_data)

    def _init_tables(self) -> None:
        groups_table = self.query_one("#groups-table", DataTable)
        groups_table.add_columns(("用途", "usage"), ("当前", "current"))
        groups_table.cursor_type = "row"
        groups_table.show_header = True
        groups_table.navigation_next_handler = self.action_focus_nodes

        nodes_table = self.query_one("#nodes-table", DataTable)
        nodes_table.add_columns(("节点", "node"), ("延迟", "delay"))
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
            match_group = service.match_rule_group() if hasattr(service, "match_rule_group") else None
            try:
                extra = extra_subscription_names(read_config(self.paths))
            except Exception:
                extra = []
            delays = {
                name: group.delay for name, group in context.groups.items() if group.delay is not None
            }
            tree = build_switch_tree(
                context.groups,
                match_group=match_group,
                extra_names=extra,
                delays=delays,
            )
            self.app.call_from_thread(self._apply_groups, context, tree, None)
        except Exception as exc:
            self.app.call_from_thread(self._apply_groups, None, (), str(exc))

    def _apply_groups(self, context, tree, error: str | None) -> None:
        groups_table = self.query_one("#groups-table", DataTable)
        if error is not None or context is None:
            self._api_available = False
            self._update_api_status()
            _sync_table_rows(groups_table, {_ERROR_KEY: (f"错误: {error}", "─")})
            return

        self._api_available = context.api_available
        self._groups = list(context.groups.values())
        self._tree_entries = list(tree or ())
        self._drilled = None
        self._update_api_status()

        rows: dict[str, tuple[str, str]] = {}
        for entry in self._tree_entries:
            group = context.groups.get(entry.key)
            current = group.current if group else "─"
            rows[entry.key] = (entry.display, current or "─")
        if not rows:
            rows[_PLACEHOLDER_KEY] = ("[#8b98aa]没有可显示的代理组[/]", "─")
        _sync_table_rows(groups_table, rows)

        if not self._tree_entries:
            self._current_group = None
            self._current_entry = None
            self._update_nodes_table()
            return

        previous_group_name = self._current_group.name if self._current_group else None
        chosen = next((entry for entry in self._tree_entries if entry.key == previous_group_name), None)
        if chosen is None:
            chosen = self._tree_entries[0]
        self._current_entry = chosen
        self._current_group = context.groups.get(chosen.key)
        self._update_nodes_table()
        groups_table.move_cursor(row=self._tree_entries.index(chosen), animate=False)

    def _update_api_status(self) -> None:
        label = self.query_one("#api-status", Label)
        if self._api_available:
            label.update("[#a3e635]● API 已连接[/]")
        else:
            label.update("[#f6c177]○ 仅显示运行配置；切换前请启动或重启代理[/]")

    def _visible_choices(self) -> tuple[SwitchChoice, ...]:
        if self._drilled is not None:
            return self._drilled.children
        if self._current_entry is not None:
            return self._current_entry.children
        return ()

    def _update_nodes_table(self) -> None:
        nodes_table = self.query_one("#nodes-table", DataTable)
        previous_node = self._current_node_key(nodes_table)

        current_label = self.query_one("#current-node", Label)
        rows: dict[str, tuple[str, str]] = {}

        if not self._current_group:
            current_label.update("[#8b98aa]尚未选择分组[/]")
            rows[_PLACEHOLDER_KEY] = ("[#8b98aa]选择分组后查看节点[/]", "─")
        else:
            if self._drilled is not None:
                current_label.update(f"[#a3e635]● {self._drilled.display}[/]")
            else:
                current_label.update(f"[#a3e635]● {self._current_group.current}[/]")
            choices = self._visible_choices()
            if not choices:
                for node in self._current_group.candidates:
                    is_current = node == self._current_group.current
                    delay = "─"
                    if is_current and self._current_group.delay:
                        delay = f"{self._current_group.delay}ms"
                    prefix = "[#a3e635]●[/] " if is_current else "  "
                    rows[node] = (f"{prefix}{node}", delay)
            else:
                for choice in choices:
                    prefix = "[#a3e635]●[/] " if choice.current else "  "
                    rows[choice.key] = (f"{prefix}{choice.display}", choice.annotation or "─")

        _sync_table_rows(nodes_table, rows)

        if not self._current_group:
            return

        choices = self._visible_choices()
        if choices:
            preferred = previous_node or next((choice.key for choice in choices if choice.current), None)
        else:
            preferred = previous_node or self._current_group.current
        self._move_nodes_cursor(preferred)

    def _set_current_group(self, group_name: str, focus_nodes: bool) -> None:
        self._drilled = None
        self._current_entry = next((entry for entry in self._tree_entries if entry.key == group_name), None)
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
        if not node_name:
            return
        choices = self._visible_choices()
        if choices:
            keys = [choice.key for choice in choices]
        else:
            keys = list(self._current_group.candidates if self._current_group else [])
        try:
            row_index = keys.index(node_name)
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
        if self._drilled is not None and isinstance(focused, DataTable) and focused.id == "nodes-table":
            self._drilled = None
            self._update_nodes_table()
            self.query_one("#proxy-action-status", Label).update("[#8b98aa]已返回上一级[/]")
            return
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

        row = nodes_table.ordered_rows[nodes_table.cursor_row]
        node_name = str(row.key.value)
        choice = next((item for item in self._visible_choices() if item.key == node_name), None)
        if choice is not None and choice.drillable:
            self._drilled = choice
            self._update_nodes_table()
            self.query_one("#nodes-table", DataTable).focus()
            self.query_one("#proxy-action-status", Label).update(f"[#8b98aa]已进入 {choice.display}[/]")
            return

        if not self._api_available:
            self.query_one("#proxy-action-status", Label).update(
                "[#f6c177]API 不可访问；请点击重启或执行 cproxy restart[/]"
            )
            return

        path = list(choice.path) if choice is not None else [(self._current_group.name, node_name)]
        if not path:
            self.notify(f"[{self._current_group.name}] 该项不能直接切换", severity="warning")
            return

        if choice is None and str(self._current_group.type).lower() not in {"selector", "select"}:
            self.notify(f"分组 [{self._current_group.name}] 不支持手动切换", severity="warning")
            return

        group_name, target_name = path[-1]
        self.query_one("#proxy-action-status", Label).update(f"[#f6c177]正在切换 {group_name} → {target_name}…[/]")
        if len(path) == 1:
            self._switch_node(path[0][0], path[0][1])
        else:
            self._switch_path(path)

    @work(thread=True, exclusive=True, group="proxy-action")
    def _switch_node(self, group_name: str, node_name: str) -> None:
        try:
            QueryService(self.paths).switch_group(group_name, node_name)
            self.app.call_from_thread(self._finish_switch_node, group_name, node_name, None)
        except Exception as exc:
            self.app.call_from_thread(self._finish_switch_node, group_name, node_name, exc)

    @work(thread=True, exclusive=True, group="proxy-action")
    def _switch_path(self, path: list[tuple[str, str]]) -> None:
        group_name, node_name = path[-1]
        try:
            QueryService(self.paths).switch_path(path)
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
        group_name = self._drilled.key if self._drilled is not None else self._current_group.name
        current_name = self._current_group.current
        if self._drilled is not None:
            drilled_group = next((group for group in self._groups if group.name == self._drilled.key), None)
            if drilled_group is not None:
                current_name = drilled_group.current
        self.query_one("#proxy-action-status", Label).update(f"[#f6c177]正在测速: {group_name}…[/]")
        self._test_delay(group_name, current_name)

    @work(thread=True, exclusive=True, group="proxy-action")
    def _test_delay(self, group_name: str, current_name: str | None) -> None:
        def on_progress(done: int, total: int, node_name: str, delay: int | None) -> None:
            try:
                self.app.call_from_thread(self._on_test_progress, group_name, current_name, done, total, node_name, delay)
            except Exception:
                # 界面已卸载或应用正在关闭，丢弃进度即可
                pass

        try:
            report = DiagnosticsService(self.paths).test_group(group_name, on_progress=on_progress)
            self.app.call_from_thread(self._finish_test_delay, group_name, current_name, report, None)
        except Exception as exc:
            self.app.call_from_thread(self._finish_test_delay, group_name, current_name, None, exc)

    def _delay_view_matches(self, group_name: str) -> bool:
        if self._drilled is not None:
            return self._drilled.key == group_name
        return self._current_group is not None and self._current_group.name == group_name

    @staticmethod
    def _format_delay_cell(delay: int | None) -> str:
        if not delay:
            return "[#fb7185]失败[/]"
        if delay < 200:
            return f"[#a3e635]{delay}ms[/]"
        if delay < 500:
            return f"[#f6c177]{delay}ms[/]"
        return f"[#fb7185]{delay}ms[/]"

    def _on_test_progress(
        self,
        group_name: str,
        current_name: str | None,
        done: int,
        total: int,
        node_name: str,
        delay: int | None,
    ) -> None:
        result_text = f"{delay}ms" if delay else "失败"
        self.query_one("#proxy-action-status", Label).update(
            f"[#f6c177]正在测速 {done}/{total} · {node_name} · {result_text}[/]"
        )
        if not self._delay_view_matches(group_name):
            return
        nodes_table = self.query_one("#nodes-table", DataTable)
        delay_cell = self._format_delay_cell(delay)
        if node_name in nodes_table.rows:
            nodes_table.update_cell(node_name, "delay", delay_cell, update_width=True)
        else:
            prefix = "[#a3e635]●[/] " if node_name == current_name else "  "
            nodes_table.add_row(f"{prefix}{node_name}", delay_cell, key=node_name)

    def _finish_test_delay(self, group_name: str, current_name: str | None, report, error: Exception | None) -> None:
        status = self.query_one("#proxy-action-status", Label)
        if error is not None:
            message = "API 不可访问" if isinstance(error, APIUnavailableError) else f"测速失败: {error}"
            status.update(f"[#fb7185]{message}[/]")
            self.notify(message, severity="error")
            return

        self.query_one("#current-node", Label).update(f"[#a3e635]● {current_name}[/]")
        if self._delay_view_matches(group_name):
            nodes_table = self.query_one("#nodes-table", DataTable)
            for result in report.results:
                delay_cell = self._format_delay_cell(result.delay if (result.ok and result.delay) else None)
                if result.name in nodes_table.rows:
                    nodes_table.update_cell(result.name, "delay", delay_cell, update_width=True)
                else:
                    prefix = "[#a3e635]●[/] " if result.name == current_name else "  "
                    nodes_table.add_row(f"{prefix}{result.name}", delay_cell, key=result.name)
        status.update(f"[#a3e635]测速完成: {group_name}[/]")
        self.notify(f"测速完成: {group_name}", severity="information")
