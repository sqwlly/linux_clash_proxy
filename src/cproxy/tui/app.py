from __future__ import annotations

import time

from textual import events
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.theme import Theme
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
from .screens.ai_route import AIRouteScreen
from .screens.config_editor import ConfigEditorScreen
from .screens.connections import ConnectionsScreen
from .screens.dashboard import DashboardScreen
from .screens.logs import LogsScreen
from .screens.providers import ProvidersScreen
from .screens.proxies import ProxiesScreen
from .screens.subscriptions import SubscriptionsScreen
from .screens.system_proxy import SystemProxyScreen


# 主题令牌（与 styles.tcss 的 $primary/$warning/... 同源）。
# 这些颜色会被 Textual 的 TCSS 变量系统使用：CSS 里写 ``$primary`` 就会自动
# 解析为当前主题的 primary 字段。改这里 = 改全套 widget 默认色。
DARK_THEME = Theme(
    name="cproxy-dark",
    primary="#5eead4",          # teal：默认强调色（Header / Tab focus / DataTable focus）
    secondary="#8794a6",        # muted text：Footer / Tabs 未激活态
    warning="#f2c166",          # 警告黄：AI 卡片 / 备用面板 / Footer 键名
    error="#fb7185",            # 错误红：danger-button / Toast.-error
    success="#a3e635",          # 成功绿：traffic 卡片 / 当前节点 / Toast.-information
    accent="#5eead4",
    foreground="#d9e2ec",       # 主文本
    background="#0f1419",       # 屏幕底色
    surface="#151c26",          # 卡片底色
    panel="#101821",            # Header / Footer / Tabs 底色
    dark=True,
)

# 浅色主题：为亮底重新调一组对比度合理的色（teal 加深、warning 偏橙、success 加深）；
# Textual 切换主题后会立刻把 $primary/$background/... 替换成这套值，CSS 里
# 写 ``$primary`` 的地方自动跟着变。注意：内嵌 Rich markup（dashboard 里的
# ``[#a3e635]...[/]``）不走主题系统——它们是固定语义色（运行中=绿、停止=红），
# 故意跨主题保持一致。
LIGHT_THEME = Theme(
    name="cproxy-light",
    primary="#0e7490",
    secondary="#475569",
    warning="#b45309",
    error="#be123c",
    success="#15803d",
    accent="#0e7490",
    foreground="#1f2937",
    background="#f8fafc",
    surface="#ffffff",
    panel="#e2e8f0",
    dark=False,
)


class CProxyApp(App):
    TITLE = "CProxy"
    SUB_TITLE = "Mihomo 代理管理器"
    ENABLE_COMMAND_PALETTE = False

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
        Binding("q", "quit", "退出", priority=True),
        Binding("f5", "refresh_all", "刷新当前页", priority=True),
        Binding("[", "previous_tab", "上一页", priority=True),
        Binding("]", "next_tab", "下一页", priority=True),
        Binding("ctrl+left", "previous_tab", "上一页", priority=True),
        Binding("ctrl+right", "next_tab", "下一页", priority=True),
        Binding("ctrl+t", "toggle_theme", "切换主题", priority=False),
        Binding("escape", "back", "返回", priority=True),
        Binding("1", "switch_tab('dashboard')", "概览", show=False),
        Binding("2", "switch_tab('proxies')", "节点", show=False),
        Binding("3", "switch_tab('providers')", "提供方", show=False),
        Binding("4", "switch_tab('connections')", "连接", show=False),
        Binding("5", "switch_tab('ai-route')", "AI 路由", show=False),
        Binding("6", "switch_tab('subscriptions')", "订阅", show=False),
        Binding("7", "switch_tab('config')", "配置", show=False),
        Binding("8", "switch_tab('system-proxy')", "代理环境", show=False),
        Binding("9", "switch_tab('logs')", "日志", show=False),
    ]

    def __init__(self, paths: AppPaths | None = None, theme: str = "cproxy-dark"):
        # 主题**必须**先于 CSS 解析注册——TCSS 里的 ``$primary`` 是在 CSS
        # 解析时查表拿值，没注册的主题对应的变量会拿不到。但 ``register_theme``
        # 依赖 ``self._registered_themes``，而它是在父类 ``__init__`` 里创建的，
        # 所以这里只能先把父类跑起来，再补注册。
        super().__init__()
        self.register_theme(DARK_THEME)
        self.register_theme(LIGHT_THEME)
        self.theme = theme
        self.paths = paths or default_paths()
        self._last_refresh_by_tab: dict[str, float] = {}

    def on_mount(self) -> None:
        self._apply_density_class()

    def on_resize(self, event: events.Resize) -> None:
        self._apply_density_class(event.size.width)

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with TabbedContent(initial="dashboard", id="main-tabs"):
            with TabPane("概览", id="dashboard"):
                yield DashboardScreen(self.paths)
            with TabPane("节点", id="proxies"):
                yield ProxiesScreen(self.paths)
            with TabPane("提供方", id="providers"):
                yield ProvidersScreen(self.paths)
            with TabPane("连接", id="connections"):
                yield ConnectionsScreen(self.paths)
            with TabPane("AI 路由", id="ai-route"):
                yield AIRouteScreen(self.paths)
            with TabPane("订阅", id="subscriptions"):
                yield SubscriptionsScreen(self.paths)
            with TabPane("配置", id="config"):
                yield ConfigEditorScreen(self.paths)
            with TabPane("代理环境", id="system-proxy"):
                yield SystemProxyScreen(self.paths)
            with TabPane("日志", id="logs"):
                yield LogsScreen(self.paths)
        yield Footer()

    def _apply_density_class(self, width: int | None = None) -> None:
        terminal_width = width if width is not None else self.size.width
        self.set_class(terminal_width < 100, "compact")

    def action_refresh_all(self) -> None:
        self._refresh_active_page(force=True)

    def action_toggle_theme(self) -> None:
        # 在我们注册的两套主题之间翻转。Textual 的 ``theme`` reactive 在赋值
        # 时会触发 ``watch_theme``，把新主题的 ``$primary``/``$background``/...
        # 重新写一遍 CSS 变量——所以 styles.tcss 里写的 ``$primary`` 之类的
        # 选择器全部自动跟着换，无需手动重画 widget。
        self.theme = "cproxy-light" if self.theme == "cproxy-dark" else "cproxy-dark"

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
