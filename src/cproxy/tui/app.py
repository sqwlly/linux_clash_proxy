from __future__ import annotations

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.widgets import Footer, Header, TabbedContent, TabPane

from ..config import AppPaths, default_paths
from .screens.dashboard import DashboardScreen
from .screens.proxies import ProxiesScreen
from .screens.ai_route import AIRouteScreen
from .screens.subscriptions import SubscriptionsScreen
from .screens.config_editor import ConfigEditorScreen
from .screens.logs import LogsScreen
from .screens.system_proxy import SystemProxyScreen


class CProxyApp(App):
    TITLE = "CProxy"
    SUB_TITLE = "Mihomo Proxy Manager"

    CSS_PATH = "styles.tcss"

    BINDINGS = [
        Binding("q", "quit", "Quit", priority=True),
        Binding("ctrl+r", "refresh_all", "Refresh", priority=True),
        Binding("1", "switch_tab('dashboard')", "Overview"),
        Binding("2", "switch_tab('proxies')", "Nodes"),
        Binding("3", "switch_tab('ai-route')", "AI Route"),
        Binding("4", "switch_tab('subscriptions')", "Subs"),
        Binding("5", "switch_tab('config')", "Config"),
        Binding("6", "switch_tab('system-proxy')", "Proxy"),
        Binding("7", "switch_tab('logs')", "Logs"),
    ]

    def __init__(self, paths: AppPaths | None = None):
        super().__init__()
        self.paths = paths or default_paths()

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with TabbedContent(initial="dashboard", id="main-tabs"):
            with TabPane("Overview", id="dashboard"):
                yield DashboardScreen(self.paths)
            with TabPane("Nodes", id="proxies"):
                yield ProxiesScreen(self.paths)
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

    def action_refresh_all(self) -> None:
        for screen in self.query(DashboardScreen):
            screen.refresh_data()
        for screen in self.query(ProxiesScreen):
            screen.refresh_data()
        for screen in self.query(AIRouteScreen):
            screen.refresh_data()

    def action_switch_tab(self, tab_id: str) -> None:
        tabbed = self.query_one(TabbedContent)
        tabbed.active = tab_id


def run_tui(paths: AppPaths | None = None) -> None:
    app = CProxyApp(paths)
    app.run()
