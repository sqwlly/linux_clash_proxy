from __future__ import annotations

import time

from textual import events
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.widgets import (
    Button,
    Checkbox,
    DataTable,
    Footer,
    Header,
    Input,
    Switch,
    TabbedContent,
    TabPane,
    TextArea,
)

from ..config import AppPaths, default_paths
from .screens.dashboard import DashboardScreen
from .screens.proxies import ProxiesScreen
from .screens.providers import ProvidersScreen
from .screens.connections import ConnectionsScreen
from .screens.ai_route import AIRouteScreen
from .screens.subscriptions import SubscriptionsScreen
from .screens.config_editor import ConfigEditorScreen
from .screens.logs import LogsScreen
from .screens.system_proxy import SystemProxyScreen


class CProxyApp(App):
    TITLE = "CProxy"
    SUB_TITLE = "Mihomo Proxy Manager"

    CSS_PATH = "styles.tcss"
    TAB_ORDER = [
        "dashboard",
        "proxies",
        "providers",
        "connections",
        "ai-route",
        "subscriptions",
        "config",
        "system-proxy",
        "logs",
    ]

    BINDINGS = [
        Binding("q", "quit", "Quit", priority=True),
        Binding("ctrl+r", "refresh_all", "Refresh Page", priority=True),
        Binding("[", "previous_tab", "Prev Tab", priority=True),
        Binding("]", "next_tab", "Next Tab", priority=True),
        Binding("ctrl+left", "previous_tab", "Prev Tab", priority=True),
        Binding("ctrl+right", "next_tab", "Next Tab", priority=True),
        Binding("escape", "back", "Back", priority=True),
        Binding("1", "switch_tab('dashboard')", "Overview"),
        Binding("2", "switch_tab('proxies')", "Nodes"),
        Binding("3", "switch_tab('providers')", "Providers"),
        Binding("4", "switch_tab('connections')", "Connections"),
        Binding("5", "switch_tab('ai-route')", "AI Route"),
        Binding("6", "switch_tab('subscriptions')", "Subs"),
        Binding("7", "switch_tab('config')", "Config"),
        Binding("8", "switch_tab('system-proxy')", "Proxy"),
        Binding("9", "switch_tab('logs')", "Logs"),
    ]

    def __init__(self, paths: AppPaths | None = None):
        super().__init__()
        self.paths = paths or default_paths()
        self._last_refresh_by_tab: dict[str, float] = {}

    def on_mount(self) -> None:
        self._apply_density_class()

    def on_resize(self, event: events.Resize) -> None:
        self._apply_density_class(event.size.width)

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with TabbedContent(initial="dashboard", id="main-tabs"):
            with TabPane("Overview", id="dashboard"):
                yield DashboardScreen(self.paths)
            with TabPane("Nodes", id="proxies"):
                yield ProxiesScreen(self.paths)
            with TabPane("Providers", id="providers"):
                yield ProvidersScreen(self.paths)
            with TabPane("Connections", id="connections"):
                yield ConnectionsScreen(self.paths)
            with TabPane("AI Route", id="ai-route"):
                yield AIRouteScreen(self.paths)
            with TabPane("Subs", id="subscriptions"):
                yield SubscriptionsScreen(self.paths)
            with TabPane("Config", id="config"):
                yield ConfigEditorScreen(self.paths)
            with TabPane("Proxy", id="system-proxy"):
                yield SystemProxyScreen(self.paths)
            with TabPane("Logs", id="logs"):
                yield LogsScreen(self.paths)
        yield Footer()

    def _apply_density_class(self, width: int | None = None) -> None:
        terminal_width = width if width is not None else self.size.width
        self.set_class(terminal_width < 100, "compact")

    def action_refresh_all(self) -> None:
        self._refresh_active_page(force=True)

    def _refresh_active_page(self, force: bool = False) -> None:
        tabbed = self.query_one(TabbedContent)
        now = time.monotonic()
        last_refresh = self._last_refresh_by_tab.get(tabbed.active, 0)
        if not force and now - last_refresh < 1:
            return
        self._last_refresh_by_tab[tabbed.active] = now
        refresh_targets = {
            "dashboard": (DashboardScreen, "refresh_data"),
            "proxies": (ProxiesScreen, "refresh_data"),
            "providers": (ProvidersScreen, "refresh_data"),
            "connections": (ConnectionsScreen, "refresh_data"),
            "ai-route": (AIRouteScreen, "refresh_data"),
            "subscriptions": (SubscriptionsScreen, "refresh_data"),
            "config": (ConfigEditorScreen, "refresh_data"),
            "system-proxy": (SystemProxyScreen, "refresh_data"),
            "logs": (LogsScreen, "refresh_data"),
        }
        target = refresh_targets.get(tabbed.active)
        if target is None:
            return
        target_type, refresh_action = target
        for screen in self.query(target_type):
            getattr(screen, refresh_action)()
            return

    def action_switch_tab(self, tab_id: str) -> None:
        self._set_active_tab(tab_id)

    def action_next_tab(self) -> None:
        self._move_tab(1)

    def action_previous_tab(self) -> None:
        self._move_tab(-1)

    def _move_tab(self, offset: int) -> None:
        tabbed = self.query_one(TabbedContent)
        current = tabbed.active
        if current not in self.TAB_ORDER:
            self._set_active_tab(self.TAB_ORDER[0])
            return
        index = self.TAB_ORDER.index(current)
        self._set_active_tab(self.TAB_ORDER[(index + offset) % len(self.TAB_ORDER)])

    def _set_active_tab(self, tab_id: str) -> None:
        tabbed = self.query_one(TabbedContent)
        tabbed.active = tab_id
        self.call_later(self._refresh_active_page)

    def on_tabbed_content_tab_activated(self, event: TabbedContent.TabActivated) -> None:
        self.call_later(self._refresh_active_page)

    def on_key(self, event: events.Key) -> None:
        focused = self.focused
        if focused is not None and focused.__class__.__name__ == "ContentTabs":
            if event.key == "l":
                self._move_tab(1)
                event.stop()
            elif event.key == "h":
                self._move_tab(-1)
                event.stop()
            elif event.key in {"down", "enter"}:
                self._focus_active_tab_content()
                event.stop()
            return

        if event.key in {"down", "up"} and isinstance(focused, (Button, Checkbox, Input, Switch)):
            if event.key == "down":
                self.action_focus_next()
            else:
                self.action_focus_previous()
            event.stop()
            return

        if event.key in {"left", "right"} and isinstance(focused, (Button, Checkbox, Switch)):
            if event.key == "right":
                self.action_focus_next()
            else:
                self.action_focus_previous()
            event.stop()

    def action_back(self) -> None:
        tabbed = self.query_one(TabbedContent)
        focused = self.focused
        if tabbed.active == "proxies" and isinstance(focused, DataTable) and focused.id == "nodes-table":
            self.query_one("#groups-table", DataTable).focus()
            return
        if focused is not None and focused.__class__.__name__ != "ContentTabs":
            self._focus_top_tabs()

    def _focus_top_tabs(self) -> None:
        tabbed = self.query_one(TabbedContent)
        for child in tabbed.walk_children():
            if child.__class__.__name__ == "ContentTabs":
                child.focus()
                return
        tabbed.focus()

    def _focus_active_tab_content(self) -> None:
        focused = self.focused
        if focused is not None and focused.__class__.__name__ != "ContentTabs":
            return

        tabbed = self.query_one(TabbedContent)
        active_id = tabbed.active
        target_ids = {
            "proxies": "#groups-table",
            "providers": "#providers-table",
            "connections": "#connections-table",
            "ai-route": "#ai-probe-table",
            "subscriptions": "#sub-url-input",
            "config": "#btn-config-save",
            "system-proxy": "#switch-http",
            "logs": "#btn-log-clear",
        }
        selector = target_ids.get(active_id)
        if selector is None:
            return
        target = self.query_one(selector)
        if isinstance(target, (DataTable, Input, TextArea)) or getattr(target, "can_focus", False):
            target.focus()


def run_tui(paths: AppPaths | None = None) -> None:
    app = CProxyApp(paths)
    app.run()
