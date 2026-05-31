import asyncio

from textual.widgets import Button, Checkbox, DataTable, Input, Switch, TextArea

from cproxy.config import AppPaths
from cproxy.tui.app import CProxyApp


def test_tui_app_bracket_keys_switch_top_tabs(tmp_path):
    paths = AppPaths(tmp_path / "config", tmp_path / "data", tmp_path / "state")
    paths.config_dir.mkdir(parents=True, exist_ok=True)
    (paths.config_dir / "config.yaml").write_text(
        "mixed-port: 7890\nproxy-groups: []\nproxies: []\nrules: []\n",
        encoding="utf-8",
    )

    async def run_case():
        app = CProxyApp(paths)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(0.1)
            tabs = app.query_one("#main-tabs")
            assert tabs.active == "dashboard"
            await pilot.press("]")
            assert tabs.active == "proxies"
            await pilot.press("[")
            assert tabs.active == "dashboard"
            await pilot.press("[")
            assert tabs.active == "logs"

    asyncio.run(run_case())


def test_tui_app_down_from_top_tabs_enters_active_tab(tmp_path):
    paths = AppPaths(tmp_path / "config", tmp_path / "data", tmp_path / "state")
    paths.config_dir.mkdir(parents=True, exist_ok=True)
    paths.data_dir.mkdir(parents=True, exist_ok=True)
    (paths.config_dir / "config.yaml").write_text(
        "mixed-port: 7890\nproxy-groups: []\nproxies: []\nrules: []\n",
        encoding="utf-8",
    )
    (paths.data_dir / "runtime.yaml").write_text(
        "mixed-port: 7890\nproxy-groups: []\nproxies: []\nrules: []\n",
        encoding="utf-8",
    )

    async def run_case():
        app = CProxyApp(paths)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(0.1)
            tabs = app.query_one("#main-tabs")
            tabs.active = "proxies"
            tabs.focus()
            await pilot.pause(0.1)
            await pilot.press("down")
            assert app.focused is app.query_one("#groups-table", DataTable)

    asyncio.run(run_case())


def test_tui_app_arrow_keys_switch_top_tabs_when_tabbar_is_focused(tmp_path):
    paths = AppPaths(tmp_path / "config", tmp_path / "data", tmp_path / "state")
    paths.config_dir.mkdir(parents=True, exist_ok=True)
    (paths.config_dir / "config.yaml").write_text(
        "mixed-port: 7890\nproxy-groups: []\nproxies: []\nrules: []\n",
        encoding="utf-8",
    )

    async def run_case():
        app = CProxyApp(paths)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(0.1)
            tabs = app.query_one("#main-tabs")
            tabs.focus()
            await pilot.press("right")
            assert tabs.active == "proxies"
            await pilot.press("left")
            assert tabs.active == "dashboard"

    asyncio.run(run_case())


def test_tui_app_escape_returns_from_content_to_top_tabs(tmp_path):
    paths = AppPaths(tmp_path / "config", tmp_path / "data", tmp_path / "state")
    paths.config_dir.mkdir(parents=True, exist_ok=True)
    paths.data_dir.mkdir(parents=True, exist_ok=True)
    (paths.config_dir / "config.yaml").write_text(
        "mixed-port: 7890\nproxy-groups: []\nproxies: []\nrules: []\n",
        encoding="utf-8",
    )
    (paths.data_dir / "runtime.yaml").write_text(
        "mixed-port: 7890\nproxy-groups: []\nproxies: []\nrules: []\n",
        encoding="utf-8",
    )

    async def run_case():
        app = CProxyApp(paths)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(0.1)
            tabs = app.query_one("#main-tabs")
            tabs.active = "subscriptions"
            tabs.focus()
            await pilot.pause(0.1)
            await pilot.press("down")
            assert app.focused is app.query_one("#sub-url-input", Input)
            await pilot.press("escape")
            assert app.focused is not None
            assert app.focused.__class__.__name__ == "ContentTabs"

    asyncio.run(run_case())


def test_tui_app_up_down_move_between_form_controls(tmp_path):
    paths = AppPaths(tmp_path / "config", tmp_path / "data", tmp_path / "state")
    paths.config_dir.mkdir(parents=True, exist_ok=True)
    (paths.config_dir / "config.yaml").write_text(
        "mixed-port: 7890\nproxy-groups: []\nproxies: []\nrules: []\n",
        encoding="utf-8",
    )

    async def run_case():
        app = CProxyApp(paths)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(0.1)
            tabs = app.query_one("#main-tabs")
            tabs.active = "subscriptions"
            tabs.focus()
            await pilot.pause(0.1)
            await pilot.press("down")
            assert app.focused is app.query_one("#sub-url-input", Input)
            await pilot.press("down")
            assert app.focused is app.query_one("#sub-group-input", Input)
            await pilot.press("up")
            assert app.focused is app.query_one("#sub-url-input", Input)

    asyncio.run(run_case())


def test_tui_app_up_down_move_between_system_proxy_switches(tmp_path):
    paths = AppPaths(tmp_path / "config", tmp_path / "data", tmp_path / "state")
    paths.config_dir.mkdir(parents=True, exist_ok=True)
    (paths.config_dir / "config.yaml").write_text(
        "mixed-port: 7890\nproxy-groups: []\nproxies: []\nrules: []\n",
        encoding="utf-8",
    )

    async def run_case():
        app = CProxyApp(paths)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(0.1)
            tabs = app.query_one("#main-tabs")
            tabs.active = "system-proxy"
            tabs.focus()
            await pilot.pause(0.1)
            await pilot.press("down")
            assert app.focused is app.query_one("#switch-http", Switch)
            await pilot.press("down")
            assert app.focused is app.query_one("#switch-https", Switch)
            await pilot.press("up")
            assert app.focused is app.query_one("#switch-http", Switch)

    asyncio.run(run_case())


def test_tui_app_config_page_toolbar_and_editor_are_reachable(tmp_path):
    paths = AppPaths(tmp_path / "config", tmp_path / "data", tmp_path / "state")
    paths.config_dir.mkdir(parents=True, exist_ok=True)
    paths.data_dir.mkdir(parents=True, exist_ok=True)
    (paths.config_dir / "config.yaml").write_text(
        "mixed-port: 7890\nproxy-groups: []\nproxies: []\nrules: []\n",
        encoding="utf-8",
    )
    (paths.data_dir / "runtime.yaml").write_text(
        "mixed-port: 7890\nproxy-groups: []\nproxies: []\nrules: []\n",
        encoding="utf-8",
    )

    async def run_case():
        app = CProxyApp(paths)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(0.1)
            tabs = app.query_one("#main-tabs")
            tabs.active = "config"
            tabs.focus()
            await pilot.pause(0.1)
            await pilot.press("down")
            assert app.focused is app.query_one("#btn-config-save", Button)
            await pilot.press("right")
            assert app.focused is app.query_one("#btn-config-render", Button)
            await pilot.press("right")
            assert app.focused is app.query_one("#btn-config-restart", Button)
            await pilot.press("right")
            assert app.focused is app.query_one("#btn-config-reload", Button)
            await pilot.press("down")
            assert app.focused is app.query_one("#config-editor", TextArea)
            await pilot.press("escape")
            assert app.focused is not None
            assert app.focused.__class__.__name__ == "ContentTabs"

    asyncio.run(run_case())


def test_tui_app_logs_page_toolbar_and_viewer_are_reachable(tmp_path):
    paths = AppPaths(tmp_path / "config", tmp_path / "data", tmp_path / "state")
    paths.config_dir.mkdir(parents=True, exist_ok=True)
    paths.data_dir.mkdir(parents=True, exist_ok=True)
    (paths.config_dir / "config.yaml").write_text(
        "mixed-port: 7890\nproxy-groups: []\nproxies: []\nrules: []\n",
        encoding="utf-8",
    )

    async def run_case():
        app = CProxyApp(paths)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(0.1)
            tabs = app.query_one("#main-tabs")
            tabs.active = "logs"
            tabs.focus()
            await pilot.pause(0.1)
            await pilot.press("down")
            assert app.focused is app.query_one("#btn-log-clear", Button)
            await pilot.press("right")
            assert app.focused is app.query_one("#btn-log-refresh", Button)
            await pilot.press("right")
            assert app.focused is app.query_one("#chk-follow", Checkbox)
            await pilot.press("down")
            assert app.focused is app.query_one("#log-viewer", TextArea)
            await pilot.press("escape")
            assert app.focused is not None
            assert app.focused.__class__.__name__ == "ContentTabs"

    asyncio.run(run_case())


def test_tui_app_new_enterprise_tabs_are_keyboard_reachable(tmp_path):
    paths = AppPaths(tmp_path / "config", tmp_path / "data", tmp_path / "state")
    paths.config_dir.mkdir(parents=True, exist_ok=True)
    (paths.config_dir / "config.yaml").write_text(
        "mixed-port: 7890\nproxy-groups: []\nproxies: []\nrules: []\n",
        encoding="utf-8",
    )

    async def run_case(tab_id: str, selector: str):
        app = CProxyApp(paths)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(0.1)
            tabs = app.query_one("#main-tabs")
            tabs.active = tab_id
            tabs.focus()
            await pilot.pause(0.1)
            await pilot.press("down")
            assert app.focused is app.query_one(selector, DataTable)

    asyncio.run(run_case("providers", "#providers-table"))
    asyncio.run(run_case("connections", "#connections-table"))


def test_tui_app_ga_layout_smoke_80_and_120_columns(tmp_path):
    paths = AppPaths(tmp_path / "config", tmp_path / "data", tmp_path / "state")
    paths.config_dir.mkdir(parents=True, exist_ok=True)
    paths.data_dir.mkdir(parents=True, exist_ok=True)
    (paths.config_dir / "config.yaml").write_text(
        "mixed-port: 7890\nproxy-groups: []\nproxies: []\nrules: []\n",
        encoding="utf-8",
    )
    (paths.data_dir / "runtime.yaml").write_text(
        "mixed-port: 7890\nproxy-groups: []\nproxies: []\nrules: []\n",
        encoding="utf-8",
    )

    async def run_case(size):
        app = CProxyApp(paths)
        async with app.run_test(size=size) as pilot:
            await pilot.pause(0.1)
            tabs = app.query_one("#main-tabs")
            for tab_id in ("dashboard", "proxies", "providers", "connections", "logs"):
                tabs.active = tab_id
                await pilot.pause(0.05)
                assert tabs.active == tab_id

    asyncio.run(run_case((80, 24)))
    asyncio.run(run_case((120, 32)))


def test_tui_app_subscription_buttons_are_reachable_from_inputs(tmp_path):
    paths = AppPaths(tmp_path / "config", tmp_path / "data", tmp_path / "state")
    paths.config_dir.mkdir(parents=True, exist_ok=True)
    (paths.config_dir / "config.yaml").write_text(
        "mixed-port: 7890\nproxy-groups: []\nproxies: []\nrules: []\n",
        encoding="utf-8",
    )

    async def run_case():
        app = CProxyApp(paths)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(0.1)
            tabs = app.query_one("#main-tabs")
            tabs.active = "subscriptions"
            tabs.focus()
            await pilot.pause(0.1)
            await pilot.press("down")
            assert app.focused is app.query_one("#sub-url-input", Input)
            await pilot.press("down")
            assert app.focused is app.query_one("#sub-group-input", Input)
            await pilot.press("down")
            assert app.focused is app.query_one("#sub-attach-input", Input)
            await pilot.press("down")
            assert app.focused is app.query_one("#btn-sub-preview", Button)
            await pilot.press("right")
            assert app.focused is app.query_one("#btn-sub-apply", Button)
            await pilot.press("right")
            assert app.focused is app.query_one("#btn-sub-update", Button)

    asyncio.run(run_case())


def test_tui_app_ai_table_boundary_reaches_toolbar(tmp_path):
    paths = AppPaths(tmp_path / "config", tmp_path / "data", tmp_path / "state")
    paths.config_dir.mkdir(parents=True, exist_ok=True)
    (paths.config_dir / "config.yaml").write_text(
        "mixed-port: 7890\nproxy-groups: []\nproxies: []\nrules: []\n",
        encoding="utf-8",
    )

    async def run_case():
        app = CProxyApp(paths)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(0.1)
            tabs = app.query_one("#main-tabs")
            tabs.active = "ai-route"
            tabs.focus()
            await pilot.pause(0.1)
            await pilot.press("down")
            table = app.query_one("#ai-probe-table", DataTable)
            assert app.focused is table
            await pilot.press("down")
            assert app.focused is app.query_one("#btn-ai-refresh", Button)
            await pilot.press("right")
            assert app.focused is app.query_one("#btn-ai-probe", Button)
            await pilot.press("right")
            assert app.focused is app.query_one("#btn-ai-switch", Button)

    asyncio.run(run_case())


def test_tui_app_escape_steps_back_through_nodes_page(tmp_path):
    paths = AppPaths(tmp_path / "config", tmp_path / "data", tmp_path / "state")
    paths.config_dir.mkdir(parents=True, exist_ok=True)
    paths.data_dir.mkdir(parents=True, exist_ok=True)
    (paths.config_dir / "config.yaml").write_text(
        "mixed-port: 7890\nproxy-groups: []\nproxies: []\nrules: []\n",
        encoding="utf-8",
    )
    (paths.data_dir / "runtime.yaml").write_text(
        "mixed-port: 7890\nproxy-groups: []\nproxies: []\nrules: []\n",
        encoding="utf-8",
    )

    async def run_case():
        app = CProxyApp(paths)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(0.1)
            tabs = app.query_one("#main-tabs")
            tabs.active = "proxies"
            tabs.focus()
            await pilot.pause(0.1)
            await pilot.press("down")
            assert app.focused is app.query_one("#groups-table", DataTable)
            await pilot.press("right")
            assert app.focused is app.query_one("#nodes-table", DataTable)
            await pilot.press("escape")
            assert app.focused is app.query_one("#groups-table", DataTable)
            await pilot.press("escape")
            assert app.focused is not None
            assert app.focused.__class__.__name__ == "ContentTabs"

    asyncio.run(run_case())
