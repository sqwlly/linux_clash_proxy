from __future__ import annotations

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
        Binding("enter", "activate_row", "Open/Select", priority=True),
        Binding("right", "focus_nodes", "Nodes", priority=True),
        Binding("left", "focus_groups", "Groups", priority=True),
        Binding("escape", "back", "Back", priority=True),
        Binding("s", "select_node", "Switch"),
        Binding("t", "test_delay", "Test"),
        Binding("r", "refresh_data", "Refresh"),
        Binding("g", "focus_groups", "Groups"),
        Binding("n", "focus_nodes", "Nodes"),
    ]

    def __init__(self, paths: AppPaths, **kwargs):
        super().__init__(**kwargs)
        self.paths = paths
        self._groups: list[ProxyGroup] = []
        self._current_group: ProxyGroup | None = None
        self._api_available = False

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label("Nodes", classes="page-title")
            with Horizontal(classes="workbench-row"):
                with Vertical(classes="proxy-group-card split-sidebar proxy-sidebar"):
                    yield Label("Groups", classes="proxy-group-title")
                    yield DataTable(id="groups-table")
                with Vertical(classes="proxy-group-card split-main proxy-main"):
                    yield Label("Nodes", classes="proxy-group-title")
                    yield Label("─", id="current-node", classes="node-current")
                    yield Label("─", id="api-status", classes="status-strip")
                    yield DataTable(id="nodes-table")
                    with Horizontal(classes="toolbar"):
                        yield Button("Switch", id="btn-switch-node", classes="action-button success-button")
                        yield Button("Test", id="btn-test-delay", classes="action-button primary-button")
                        yield Button("Refresh", id="btn-refresh-proxies", classes="action-button muted-button")
                        yield Button("Restart", id="btn-restart-proxy", classes="action-button muted-button")
                    yield Label("up/down: move  left/esc: groups  right: nodes  enter/s: switch", id="proxy-action-status", classes="action-status")

    def on_mount(self) -> None:
        self._init_tables()
        if not list(self.app.query("#main-tabs")):
            self.call_later(self.refresh_data)

    def _init_tables(self) -> None:
        groups_table = self.query_one("#groups-table", DataTable)
        groups_table.add_columns("Name", "Type", "Current")
        groups_table.cursor_type = "row"
        groups_table.show_header = True
        groups_table.navigation_next_handler = self.action_focus_nodes

        nodes_table = self.query_one("#nodes-table", DataTable)
        nodes_table.add_columns("Node", "Delay")
        nodes_table.cursor_type = "row"
        nodes_table.show_header = True
        nodes_table.navigation_previous_handler = self.action_focus_groups


    def refresh_data(self) -> None:
        try:
            service = QueryService(self.paths)
            context = service.load_context(require_api=False)
            self._api_available = context.api_available
            self._groups = list(context.groups.values())
            self._update_api_status()

            groups_table = self.query_one("#groups-table", DataTable)
            groups_table.clear()

            rendered_group_count = 0
            rendered_groups: list[ProxyGroup] = []
            for group in self._groups:
                group_type = str(group.type).lower()
                if group_type in {"selector", "select", "fallback", "url-test", "load-balance"}:
                    groups_table.add_row(
                        group.name,
                        group.type,
                        group.current or "─",
                        key=group.name,
                    )
                    rendered_group_count += 1
                    rendered_groups.append(group)

            if not rendered_group_count:
                groups_table.add_row("[#8b98aa]No switchable groups[/]", "─", "─")
                self._current_group = None
                self._update_nodes_table()
                return

            previous_group_name = self._current_group.name if self._current_group else None
            chosen = next((group for group in rendered_groups if group.name == previous_group_name), None)
            if chosen is None:
                selectable = [group for group in rendered_groups if str(group.type).lower() in {"selector", "select"}]
                fallback = [
                    group
                    for group in rendered_groups
                    if str(group.type).lower() in {"fallback", "url-test", "load-balance"}
                ]
                chosen = selectable[0] if selectable else fallback[0] if fallback else None
            if chosen:
                self._current_group = chosen
                self._update_nodes_table()
                groups_table.move_cursor(row=rendered_groups.index(chosen), animate=False)

        except Exception as e:
            self._api_available = False
            self._update_api_status()
            groups_table = self.query_one("#groups-table", DataTable)
            groups_table.clear()
            groups_table.add_row(f"Error: {e}", "─", "─")

    def _update_api_status(self) -> None:
        label = self.query_one("#api-status", Label)
        if self._api_available:
            label.update("[#a3e635]● API connected[/]")
        else:
            label.update("[#f6c177]○ Runtime view only; start/restart proxy before switching[/]")

    def _update_nodes_table(self) -> None:
        nodes_table = self.query_one("#nodes-table", DataTable)
        previous_node = self._current_node_key(nodes_table)
        nodes_table.clear()

        current_label = self.query_one("#current-node", Label)

        if not self._current_group:
            current_label.update("[#8b98aa]No group selected[/]")
            nodes_table.add_row("[#8b98aa]Select a group to view nodes[/]", "─")
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
        self.query_one("#proxy-action-status", Label).update(f"[#8b98aa]Selected group: {group_name}[/]")

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
            self.query_one("#proxy-action-status", Label).update("[#8b98aa]Back to groups[/]")
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
            self.query_one("#proxy-action-status", Label).update("[#f6c177]No node selected[/]")
            return

        if not self._api_available:
            self.query_one("#proxy-action-status", Label).update(
                "[#f6c177]API unavailable; use Restart or run cproxy restart[/]"
            )
            return

        try:
            row = nodes_table.ordered_rows[nodes_table.cursor_row]
            node_name = str(row.key.value)

            if str(self._current_group.type).lower() not in {"selector", "select"}:
                self.notify(f"Group [{self._current_group.name}] is not selectable", severity="warning")
                return

            service = QueryService(self.paths)
            service.switch_group(self._current_group.name, node_name)
            self.query_one("#proxy-action-status", Label).update(
                f"[#a3e635]Switched {self._current_group.name} -> {node_name}[/]"
            )
            self.notify(f"Switched: {self._current_group.name} → {node_name}", severity="information")
            self.refresh_data()

        except APIUnavailableError:
            self.query_one("#proxy-action-status", Label).update("[#fb7185]API unavailable[/]")
            self.notify("API unavailable", severity="error")
        except Exception as e:
            self.query_one("#proxy-action-status", Label).update(f"[#fb7185]Switch failed: {e}[/]")
            self.notify(f"Switch failed: {e}", severity="error")

    def action_restart_proxy(self) -> None:
        status = self.query_one("#proxy-action-status", Label)
        try:
            runtime_path = render_runtime(self.paths)
            pid = restart_process(self.paths)
            status.update(f"[#a3e635]Restarted PID {pid}; runtime {runtime_path}[/]")
            self.refresh_data()
        except Exception as e:
            status.update(f"[#fb7185]Restart failed: {e}[/]")
            self.notify(f"Restart failed: {e}", severity="error")

    def action_test_delay(self) -> None:
        if not self._current_group:
            return

        try:
            from ...diagnostics import test_group
            report = test_group(self.paths, self._current_group.name)

            nodes_table = self.query_one("#nodes-table", DataTable)
            nodes_table.clear()

            current_label = self.query_one("#current-node", Label)
            current_label.update(f"[#a3e635]● {self._current_group.current}[/]")

            for result in report.results:
                is_current = result.name == self._current_group.current
                prefix = "[#a3e635]●[/] " if is_current else "  "
                if result.ok and result.delay:
                    if result.delay < 200:
                        delay = f"[#a3e635]{result.delay}ms[/]"
                    elif result.delay < 500:
                        delay = f"[#f6c177]{result.delay}ms[/]"
                    else:
                        delay = f"[#fb7185]{result.delay}ms[/]"
                else:
                    delay = "[#fb7185]FAIL[/]"
                nodes_table.add_row(f"{prefix}{result.name}", delay, key=result.name)

            self.notify(f"Test complete: {self._current_group.name}", severity="information")

        except APIUnavailableError:
            self.notify("API unavailable", severity="error")
        except Exception as e:
            self.notify(f"Test failed: {e}", severity="error")
