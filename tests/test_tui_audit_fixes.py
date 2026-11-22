from __future__ import annotations

import asyncio
import json
import os
import stat
import threading
from pathlib import Path
from types import SimpleNamespace

from textual.app import App, ComposeResult
from textual.widgets import Button, DataTable, Label, Switch, TabbedContent, TabPane

from cproxy.backend.models import (
    AIProbeReport,
    ConnectionEntry,
    DelayCheckResult,
    GroupCheckReport,
    ProviderEntry,
    ProxyGroup,
)
from cproxy.backend.runtime import AIExit, AIRoute
from cproxy.config import AppPaths
from cproxy.tui.ai_status import chain_markup, delay_text, exit_line
from cproxy.tui.app import CProxyApp
from cproxy.tui.screens import ai_route as ai_route_module
from cproxy.tui.screens import config_editor as config_editor_module
from cproxy.tui.screens import connections as connections_module
from cproxy.tui.screens import dashboard as dashboard_module
from cproxy.tui.screens import providers as providers_module
from cproxy.tui.screens import proxies as proxies_module
from cproxy.tui.screens import subscriptions as subscriptions_module
from cproxy.tui.screens.ai_route import AIRouteScreen
from cproxy.tui.screens.config_editor import ConfigEditorScreen
from cproxy.tui.screens.connections import ConnectionsScreen
from cproxy.tui.screens.dashboard import DashboardScreen
from cproxy.tui.screens.logs import LogsScreen
from cproxy.tui.screens.providers import ProvidersScreen
from cproxy.tui.screens.proxies import ProxiesScreen
from cproxy.tui.screens.subscriptions import SubscriptionsScreen, redact_subscription_url
from cproxy.tui.screens.system_proxy import SystemProxyScreen
from cproxy.tui.widgets import NavigationTextArea as TextArea


class _HostApp(App):
    def __init__(self, widget):
        super().__init__()
        self._widget = widget

    def compose(self) -> ComposeResult:
        yield self._widget


def _isolated_paths(tmp_path: Path, extra_yaml: str = "") -> AppPaths:
    paths = AppPaths(tmp_path / "config", tmp_path / "data", tmp_path / "state")
    paths.config_dir.mkdir(parents=True, exist_ok=True)
    paths.data_dir.mkdir(parents=True, exist_ok=True)
    paths.state_dir.mkdir(parents=True, exist_ok=True)
    base = "mixed-port: 7890\nexternal-controller: 127.0.0.1:9\nproxy-groups: []\nproxies: []\nrules: []\n"
    (paths.config_dir / "config.yaml").write_text(base + extra_yaml, encoding="utf-8")
    return paths


async def _wait_for(predicate, pilot, timeout: float = 3.0) -> None:
    for _ in range(int(timeout / 0.05)):
        if predicate():
            return
        await pilot.pause(0.05)
    raise AssertionError("等待超时")


def test_virtual_region_bucket_delay_test_resolves_real_group_and_handles_system_exit(monkeypatch, tmp_path):
    """下钻到虚拟地区桶（如“美国”）测速时应解析为真实父组，且 SystemExit 不会杀掉 TUI。"""
    tested_groups: list[str] = []
    should_exit = {"flag": False}

    group = ProxyGroup(
        name="CyberGuard",
        type="select",
        current="🇺🇸 US-01",
        candidates=["🇺🇸 US-01", "🇯🇵 JP-01", "🇸🇬 SG-01"],
    )

    class FakeQueryService:
        def __init__(self, paths):
            self.paths = paths

        def load_context(self, require_api=False):
            return SimpleNamespace(groups={"CyberGuard": group}, api_available=True)

        def match_rule_group(self):
            return "CyberGuard"

    class FakeDiagnostics:
        def __init__(self, paths):
            self.paths = paths

        def test_group(self, group_name, *, on_progress=None):
            tested_groups.append(group_name)
            if should_exit["flag"]:
                raise SystemExit("错误: 未找到代理组 [/bad")
            if on_progress:
                on_progress(1, 2, "🇺🇸 US-01", 88)
                on_progress(2, 2, "🇯🇵 JP-01", 55)
            return GroupCheckReport(
                group_name=group_name,
                results=[
                    DelayCheckResult(name="🇺🇸 US-01", ok=True, delay=88),
                    DelayCheckResult(name="🇯🇵 JP-01", ok=True, delay=55),
                ],
            )

    monkeypatch.setattr(proxies_module, "QueryService", FakeQueryService)
    monkeypatch.setattr(proxies_module, "DiagnosticsService", FakeDiagnostics)
    paths = _isolated_paths(tmp_path)

    async def run_case():
        app = _HostApp(ProxiesScreen(paths))
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(0.15)
            screen = app.query_one(ProxiesScreen)
            nodes_table = screen.query_one("#nodes-table", DataTable)

            # 第一层包含虚拟地区桶（如“美国”），下钻进入该虚拟桶
            us_idx = next(
                idx for idx, row in enumerate(nodes_table.ordered_rows) if str(row.key.value) == "美国"
            )
            nodes_table.move_cursor(row=us_idx, animate=False)
            screen.action_select_node()
            await pilot.pause(0.05)
            assert screen._drilled is not None
            assert screen._drilled.key == "美国"

            # 测速应使用真实 Mihomo 组名 CyberGuard，且不把非美国节点塞进当前虚拟桶视图
            screen.action_test_delay()
            await _wait_for(lambda: len(tested_groups) == 1, pilot)
            await pilot.pause(0.1)
            assert tested_groups == ["CyberGuard"]
            assert nodes_table.row_count == 1
            assert "88ms" in str(nodes_table.get_row("🇺🇸 US-01")[1])

            # 即使底层抛出 SystemExit，也不能杀掉 App，并渲染转义后的错误提示
            should_exit["flag"] = True
            screen.action_test_delay()
            await _wait_for(lambda: len(tested_groups) == 2, pilot)
            await pilot.pause(0.1)
            status_text = str(screen.query_one("#proxy-action-status", Label).render())
            assert "测速失败" in status_text
            assert "[/bad" in status_text

    asyncio.run(run_case())


def test_proxies_empty_table_select_node_and_fallback_and_restart_safety(monkeypatch, tmp_path):
    """空表触发 action_select_node 不崩 IndexError；fallback 组允许手动切；重启需二次确认并写审计。"""
    switches: list[tuple[str, str]] = []
    restarts: list[int] = []

    fallback_group = ProxyGroup(
        name="AI-MANUAL",
        type="fallback",
        current="[US] Node A",
        candidates=["[US] Node A", "[/bad] Node B"],
    )

    class FakeQueryService:
        def __init__(self, paths):
            self.paths = paths

        def load_context(self, require_api=False):
            return SimpleNamespace(groups={"AI-MANUAL": fallback_group}, api_available=True)

        def switch_group(self, group_name, target_name):
            switches.append((group_name, target_name))
            return fallback_group

    monkeypatch.setattr(proxies_module, "QueryService", FakeQueryService)
    monkeypatch.setattr(proxies_module, "render_runtime", lambda p: p.data_dir / "runtime.yaml")
    monkeypatch.setattr(proxies_module, "restart_process", lambda p: (restarts.append(1) or 4321))
    paths = _isolated_paths(tmp_path)

    async def run_case():
        app = _HostApp(ProxiesScreen(paths))
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(0.15)
            screen = app.query_one(ProxiesScreen)
            nodes_table = screen.query_one("#nodes-table", DataTable)

            # 清空表格后触发 action_select_node 不能抛出 IndexError
            nodes_table.clear()
            screen.action_select_node()
            assert "尚未选择节点" in str(screen.query_one("#proxy-action-status", Label).render())

            # 回退到直接候选列表（choice is None）时，fallback 组必须允许手动切换且含方括号节点名不触发 MarkupError
            screen._current_entry = None
            screen._current_group = fallback_group
            screen._update_nodes_table()
            assert "[US] Node A" in str(screen.query_one("#current-node", Label).render())
            nodes_table.move_cursor(row=1, animate=False)
            screen.action_select_node()
            await _wait_for(lambda: len(switches) == 1, pilot)
            await pilot.pause(0.1)
            assert switches == [("AI-MANUAL", "[/bad] Node B")]

            # 重启代理需要二次确认并写审计日志
            screen.action_restart_proxy()
            assert restarts == []
            assert "请再次点击" in str(screen.query_one("#proxy-action-status", Label).render())

            screen.action_restart_proxy()
            await _wait_for(lambda: not screen._restart_running and len(restarts) == 1, pilot)
            await pilot.pause(0.1)
            assert restarts == [1]
            audit_file = paths.state_dir / "cproxy-audit.jsonl"
            assert audit_file.exists()
            records = [json.loads(line) for line in audit_file.read_text(encoding="utf-8").splitlines()]
            assert any(rec["action"] == "proxy_restart" and rec["result"] == "ok" for rec in records)

    asyncio.run(run_case())


def test_proxies_toolbar_arrow_navigation_in_cproxy_app(tmp_path):
    """移除 ProxiesScreen left/right priority=True 后，工具栏按钮可用左右方向键切换焦点。"""
    paths = _isolated_paths(tmp_path)

    async def run_case():
        app = CProxyApp(paths)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(0.15)
            tabs = app.query_one("#main-tabs", TabbedContent)
            tabs.active = "proxies"
            await pilot.pause(0.1)
            btn_switch = app.query_one("#btn-switch-node", Button)
            btn_switch.focus()
            await pilot.pause(0.05)
            assert app.focused is btn_switch
            await pilot.press("right")
            assert app.focused is app.query_one("#btn-test-delay", Button)
            await pilot.press("right")
            assert app.focused is app.query_one("#btn-refresh-proxies", Button)
            await pilot.press("left")
            assert app.focused is app.query_one("#btn-test-delay", Button)

            # 确认 ProxiesScreen 与 SystemProxyScreen 的状态栏 ID 已消歧
            assert len(list(app.query("#proxy-action-status"))) == 1
            assert len(list(app.query("#system-proxy-action-status"))) == 1

    asyncio.run(run_case())


def test_proxies_finish_test_delay_does_not_overwrite_current_node_when_switched_away(tmp_path):
    """测速完成时若用户已切到别的组，不得覆盖当前组的 #current-node 标签。"""
    paths = _isolated_paths(tmp_path)
    g1 = ProxyGroup(name="GroupA", type="select", current="NodeA", candidates=["NodeA"])
    g2 = ProxyGroup(name="GroupB", type="select", current="NodeB", candidates=["NodeB"])

    async def run_case():
        app = _HostApp(ProxiesScreen(paths))
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(0.1)
            screen = app.query_one(ProxiesScreen)
            screen._groups = [g1, g2]
            screen._current_group = g2
            screen._update_nodes_table()
            assert "NodeB" in str(screen.query_one("#current-node", Label).render())

            report = GroupCheckReport(group_name="GroupA", results=[DelayCheckResult("NodeA", True, 50)])
            screen._finish_test_delay("GroupA", "NodeA", report, None)
            assert "NodeB" in str(screen.query_one("#current-node", Label).render())
            assert "NodeA" not in str(screen.query_one("#current-node", Label).render())

    asyncio.run(run_case())


def test_config_editor_markup_error_redaction_and_concurrent_edit_during_save(monkeypatch, tmp_path):
    """含 `[/bad` 的非法 YAML 保存不会引发 MarkupError 且对敏感字段脱敏；保存期间并发编辑保留已修改状态。"""
    paths = _isolated_paths(tmp_path)
    config_path = paths.config_dir / "config.yaml"
    original = "mixed-port: 7890\n"
    config_path.write_text(original, encoding="utf-8")

    async def run_case_bad_yaml():
        app = _HostApp(ConfigEditorScreen(paths))
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(0.15)
            screen = app.query_one(ConfigEditorScreen)
            editor = app.query_one("#config-editor", TextArea)
            editor.load_text("secret: supersecret123\nmixed-port: [/bad\n")
            await pilot.pause(0.05)
            screen.action_save_config()
            await _wait_for(lambda: not screen._op_running, pilot)
            await pilot.pause(0.05)
            log_text = str(app.query_one("#config-log", Label).render())
            assert "保存失败" in log_text
            assert "supersecret123" not in log_text
            assert config_path.read_text(encoding="utf-8") == original

            # 验证重启二次确认文案与按钮标签“重启”一致
            editor.load_text(original)
            screen._modified = False
            screen.action_restart_config()
            assert "请再次点击“重启”确认" in str(app.query_one("#config-log", Label).render())

    asyncio.run(run_case_bad_yaml())

    # 测试保存期间用户继续编辑：保存完成后 _modified 仍必须为 True
    entered_snapshot = threading.Event()
    release_snapshot = threading.Event()
    real_snapshot = config_editor_module.snapshot_file

    def blocking_snapshot(*args, **kwargs):
        entered_snapshot.set()
        assert release_snapshot.wait(3)
        return real_snapshot(*args, **kwargs)

    monkeypatch.setattr(config_editor_module, "snapshot_file", blocking_snapshot)

    async def run_case_concurrent():
        app = _HostApp(ConfigEditorScreen(paths))
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(0.15)
            screen = app.query_one(ConfigEditorScreen)
            editor = app.query_one("#config-editor", TextArea)
            editor.load_text("mixed-port: 7891\n")
            await pilot.pause(0.05)
            screen.action_save_config()
            await _wait_for(entered_snapshot.is_set, pilot)

            # 在保存线程尚未结束时继续输入新内容
            editor.load_text("mixed-port: 7892\n")
            await pilot.pause(0.05)
            release_snapshot.set()
            await _wait_for(lambda: not screen._op_running, pilot)
            await pilot.pause(0.05)

            assert screen._modified is True
            assert "(已修改)" in str(app.query_one("#config-modified-label", Label).render())

    asyncio.run(run_case_concurrent())


def test_dashboard_handles_null_connections_and_clears_stale_fields(monkeypatch, tmp_path, ai_route_groups):
    """Dashboard 处理 `connections: null` 不清零流量；状态异常和 AI 组消失时清理陈旧字段。"""
    state = {"status_fail": False, "groups": ai_route_groups}

    class FakeAPI:
        def request(self, method, path):
            return {"uploadTotal": 2048, "downloadTotal": 4096, "connections": None}

    class FakeQueryService:
        def __init__(self, paths):
            self.api = FakeAPI()

        def get_ai_status_groups(self):
            return state["groups"]

    def fake_get_status(paths):
        if state["status_fail"]:
            raise RuntimeError("status error [/bad")
        return SimpleNamespace(running=True, port=7890, controller="[::1]:9090", pid=12345)

    monkeypatch.setattr(dashboard_module, "QueryService", FakeQueryService)
    monkeypatch.setattr(dashboard_module, "get_status", fake_get_status)
    paths = _isolated_paths(tmp_path)

    async def run_case():
        app = _HostApp(DashboardScreen(paths))
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(0.25)
            screen = app.query_one(DashboardScreen)
            assert str(screen.query_one("#dash-connections", Label).render()) == "0"
            assert "6.0 KB" in str(screen.query_one("#dash-total", Label).render())
            assert "[::1]:9090" in str(screen.query_one("#dash-controller", Label).render())
            assert "兜底" in str(screen.query_one("#dash-ai-mode", Label).render())

            # 当状态报错且 AI 组消失时，陈旧的端口/控制接口/PID 与 AI 路由字段都应被清成占位符
            state["status_fail"] = True
            state["groups"] = {}
            screen.refresh_data()
            await pilot.pause(0.25)
            assert "[/bad" in str(screen.query_one("#dash-status", Label).render())
            assert str(screen.query_one("#dash-port", Label).render()) == "─"
            assert str(screen.query_one("#dash-controller", Label).render()) == "─"
            assert str(screen.query_one("#dash-pid", Label).render()) == "─"
            assert str(screen.query_one("#dash-ai-mode", Label).render()) == "─"
            assert str(screen.query_one("#dash-ai-active", Label).render()) == "─"

    asyncio.run(run_case())


def test_system_proxy_malformed_markers_symlink_permissions_broken_yaml_and_switches(tmp_path, monkeypatch):
    """SystemProxyScreen：损坏标记中止写入、不吞非换行字符、保留符号链接与权限，
    破损 YAML 不影响 unset/移除，尊重开关与大写环境变量。"""
    paths = _isolated_paths(tmp_path)
    # 故意写坏 config.yaml
    (paths.config_dir / "config.yaml").write_text("mixed-port: [/bad\n", encoding="utf-8")

    monkeypatch.delenv("http_proxy", raising=False)
    monkeypatch.delenv("https_proxy", raising=False)
    monkeypatch.delenv("all_proxy", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)
    monkeypatch.setenv("HTTP_PROXY", "http://[upper]:7890")
    monkeypatch.setenv("HTTPS_PROXY", "")
    monkeypatch.setenv("ALL_PROXY", "")
    monkeypatch.setenv("NO_PROXY", "localhost,[::1]")

    async def run_case():
        app = _HostApp(SystemProxyScreen(paths))
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(0.1)
            screen = app.query_one(SystemProxyScreen)
            status = app.query_one("#system-proxy-action-status", Label)

            # 1) 大写环境变量识别 + 方括号转义
            assert screen.query_one("#switch-http", Switch).value is True
            assert screen.query_one("#switch-https", Switch).value is False
            assert "[upper]" in str(screen.query_one("#http-proxy-label", Label).render())

            # 2) 损坏 YAML 时点击 unset 不崩
            await pilot.click("#btn-clear-all")
            assert "unset http_proxy" in str(status.render())

            # 3) 损坏 YAML 时点击 export 报错而不崩（且不触发 MarkupError）
            await pilot.click("#btn-set-all")
            assert "读取配置失败" in str(status.render())

            # 4) 修复 YAML 后，仅开启 HTTP 开关时 export 只生成 http_proxy + no_proxy
            (paths.config_dir / "config.yaml").write_text("mixed-port: 7895\n", encoding="utf-8")
            screen.query_one("#switch-http", Switch).value = True
            screen.query_one("#switch-https", Switch).value = False
            screen.query_one("#switch-all", Switch).value = False
            screen.query_one("#btn-set-all", Button).press()
            await pilot.pause(0.05)
            rendered = str(status.render())
            assert "export http_proxy=http://127.0.0.1:7895" in rendered
            assert "export https_proxy=" not in rendered
            assert "export all_proxy=" not in rendered

            # 5) 缺少结束标记时中止写入，保持原文件完好
            broken_rc = tmp_path / ".broken_rc"
            broken_content = "export KEEP=1\n# >>> cproxy proxy >>>\nexport OLD=1\n"
            broken_rc.write_text(broken_content, encoding="utf-8")
            screen._write_shell_config(broken_rc, "127.0.0.1:7895", status)
            screen._write_shell_config(broken_rc, "127.0.0.1:7895", status)
            assert broken_rc.read_text(encoding="utf-8") == broken_content
            assert "标记不完整" in str(status.render())

            # 6) 结束标记后紧跟非换行字符时，更新块不得吃掉后续字符
            tight_rc = tmp_path / ".tight_rc"
            tight_rc.write_text(
                "# >>> cproxy proxy >>>\nexport http_proxy=old\n# <<< cproxy proxy <<<export AFTER=1\n",
                encoding="utf-8",
            )
            screen._write_shell_config(tight_rc, "127.0.0.1:7895", status)
            screen._write_shell_config(tight_rc, "127.0.0.1:7895", status)
            assert "export AFTER=1\n" in tight_rc.read_text(encoding="utf-8")

            # 7) 符号链接 rc 文件保留链接本身与目标文件原始权限（0o644）
            dotfiles_dir = tmp_path / "dotfiles"
            dotfiles_dir.mkdir()
            target_rc = dotfiles_dir / "zshrc"
            target_rc.write_text("export DOT=1\n", encoding="utf-8")
            os.chmod(target_rc, 0o644)
            symlink_rc = tmp_path / ".zshrc_link"
            symlink_rc.symlink_to(target_rc)

            screen._write_shell_config(symlink_rc, "127.0.0.1:7895", status)
            screen._write_shell_config(symlink_rc, "127.0.0.1:7895", status)
            assert symlink_rc.is_symlink()
            assert symlink_rc.resolve() == target_rc.resolve()
            assert stat.S_IMODE(target_rc.stat().st_mode) == 0o644
            assert "export http_proxy=http://127.0.0.1:7895" in target_rc.read_text(encoding="utf-8")

    asyncio.run(run_case())


def test_ai_status_markup_escaping_and_bool_delay_guard():
    """ai_status 转义含方括号的组/节点名，并拒绝把 bool 当作 int 延迟。"""
    assert delay_text(True) == "未测得"
    assert delay_text(False) == "未测得"
    assert delay_text(120) == "120ms"

    exit_item = AIExit(group="[US] Pool", node="[/bad] Node 01", delay=120, alive=True)
    rendered_exit = exit_line(exit_item)
    assert r"\[US] Pool" in rendered_exit
    assert r"\[/bad] Node 01" in rendered_exit

    route = AIRoute(
        present=True,
        auto_mode=True,
        pool="[POOL]",
        active=exit_item,
        standby=None,
        gemini=AIExit(group="AI-GEMINI", node="[/gem] Node", delay=90, alive=True),
    )
    rendered_chain = chain_markup(route)
    assert r"\[POOL]" in rendered_chain
    assert r"\[/bad] Node 01" in rendered_chain
    assert r"\[/gem] Node" in rendered_chain


def test_ai_route_system_exit_restores_mode_and_empty_probe_status(monkeypatch, tmp_path):
    """AIRouteScreen 切换遇 SystemExit 时恢复模式标签且不崩；空探测列表不显示 0/0 正常。"""

    class FakeQueryService:
        def __init__(self, paths):
            self.paths = paths

        def get_ai_status_groups(self):
            raise SystemExit("错误: AI 组异常 [/bad")

    class FakeDiagnostics:
        def __init__(self, paths):
            self.paths = paths

        def run_ai_probe(self, *, on_result=None):
            return AIProbeReport(results=[])

    monkeypatch.setattr(ai_route_module, "QueryService", FakeQueryService)
    monkeypatch.setattr(ai_route_module, "DiagnosticsService", FakeDiagnostics)
    paths = _isolated_paths(tmp_path)

    async def run_case():
        app = _HostApp(AIRouteScreen(paths))
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(0.15)
            screen = app.query_one(AIRouteScreen)

            screen.action_switch_us_sg()
            await pilot.pause(0.2)
            mode_text = str(screen.query_one("#ai-route-mode", Label).render())
            assert "切换中" not in mode_text
            assert "切换失败" in mode_text
            assert "[/bad" in mode_text

            screen.action_probe_ai()
            await _wait_for(lambda: not screen._probe_running, pilot)
            await pilot.pause(0.05)
            probe_status = str(screen.query_one("#ai-probe-status", Label).render())
            assert "0/0 正常" not in probe_status
            assert "无探测目标" in probe_status

    asyncio.run(run_case())


def test_subscriptions_redaction_and_apply_two_click_confirmation(monkeypatch, tmp_path):
    """redact_subscription_url 去除 userinfo 并遮蔽路径 token；应用订阅需二次确认且错误信息脱敏。"""
    assert redact_subscription_url("https://user:pass@example.test:8443/s/abcdef1234567890?token=xyz") == (
        "https://example.test:8443/s/...?..."
    )
    assert redact_subscription_url("https://example.test/link/550e8400-e29b-41d4-a716-446655440000") == (
        "https://example.test/link/..."
    )
    assert redact_subscription_url("https://example.test/api/v1/client/subscribe?token=secret") == (
        "https://example.test/api/v1/client/subscribe?..."
    )

    calls: list[str] = []

    class FakeRefreshService:
        def __init__(self, paths):
            self.paths = paths

        def refresh(self, *, subscription_url, groups):
            calls.append(subscription_url)
            raise RuntimeError("failed for https://sub.test/api?token=topsecret123")

    monkeypatch.setattr(subscriptions_module, "RefreshService", FakeRefreshService)
    paths = _isolated_paths(tmp_path)

    async def run_case():
        app = _HostApp(SubscriptionsScreen(paths))
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(0.15)
            screen = app.query_one(SubscriptionsScreen)
            app.query_one("#sub-url-input").value = "https://user:pw@sub.test/s/abcdef1234567890?token=topsecret123"
            output = app.query_one("#sub-output", TextArea)

            # 首次点击仅提示确认，不发起订阅写入
            screen._import_subscription(dry_run=False)
            assert calls == []
            assert "请再次点击“应用”确认" in output.text
            assert "topsecret123" not in output.text
            assert "user:pw" not in output.text

            # 第二次点击执行，且错误输出已脱敏
            screen._import_subscription(dry_run=False)
            await _wait_for(lambda: not screen._subscription_running and len(calls) == 1, pilot)
            await pilot.pause(0.05)
            assert "topsecret123" not in output.text
            assert "错误:" in output.text

    asyncio.run(run_case())


def test_connections_reset_confirm_on_error_and_providers_busy_guard(monkeypatch, tmp_path):
    """ConnectionsScreen 失败后重置 _confirm_close_all；ProvidersScreen 防并发重复更新；特殊方括号不触发 MarkupError。"""
    close_calls = 0

    class FakeConnService:
        def __init__(self, paths):
            self.paths = paths

        def list_connections(self):
            return [ConnectionEntry("c-1", "[::1]:443 [/bad", "proc[0]", "MATCH", ["[US] Group"], 10, 20)]

        def close_all_connections(self):
            nonlocal close_calls
            close_calls += 1
            raise RuntimeError("boom [/bad")

    monkeypatch.setattr(connections_module, "QueryService", FakeConnService)
    paths = _isolated_paths(tmp_path)

    async def run_conn_case():
        app = _HostApp(ConnectionsScreen(paths))
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(0.15)
            screen = app.query_one(ConnectionsScreen)
            screen.action_close_all()
            assert screen._confirm_close_all is True
            screen.action_close_all()
            assert screen._confirm_close_all is False
            await _wait_for(lambda: close_calls == 1, pilot)
            await pilot.pause(0.1)
            assert "[/bad" in str(screen.query_one("#connections-action-status", Label).render())
            assert screen._confirm_close_all is False

    asyncio.run(run_conn_case())

    started = threading.Event()
    release = threading.Event()
    provider_updates: list[str] = []

    class FakeProvService:
        def __init__(self, paths):
            self.paths = paths

        def list_proxy_providers(self):
            return [ProviderEntry("[corp][/bad", "HTTP", "HTTP", 3, "2026-11-21")]

        def update_proxy_provider(self, name):
            provider_updates.append(name)
            started.set()
            assert release.wait(3)

    monkeypatch.setattr(providers_module, "QueryService", FakeProvService)

    async def run_prov_case():
        app = _HostApp(ProvidersScreen(paths))
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(0.15)
            screen = app.query_one(ProvidersScreen)
            table = app.query_one("#providers-table", DataTable)
            table.move_cursor(row=0, animate=False)

            screen.action_update_provider()
            await _wait_for(started.is_set, pilot)
            assert screen._updating is True
            assert screen.query_one("#btn-update-provider", Button).disabled is True
            # 忙碌期间再次触发应被忽略
            screen.action_update_provider()
            release.set()
            await _wait_for(lambda: not screen._updating, pilot)
            await pilot.pause(0.1)
            assert provider_updates == ["[corp][/bad"]
            assert screen.query_one("#btn-update-provider", Button).disabled is False

    asyncio.run(run_prov_case())


def test_logs_screen_bounded_read_hide_pause_and_theme_css(tmp_path):
    """LogsScreen 有界读取尾部日志、隐藏时暂停跟随；CProxyApp 浅色主题无硬编码暗色且含 Toast.-warning。"""
    paths = _isolated_paths(tmp_path)
    log_path = paths.state_dir / "cproxy.log"
    # 写入大于 256 KB 的日志文件，确认有界读取只载入尾部并正常显示最后一行
    chunk = ("x" * 200 + "\n") * 1500
    log_path.write_text(chunk + "TAIL_END_LINE\n", encoding="utf-8")

    class _TabbedLogsApp(App):
        def compose(self) -> ComposeResult:
            with TabbedContent(initial="logs", id="main-tabs"):
                with TabPane("日志", id="logs"):
                    yield LogsScreen(paths)
                with TabPane("其他", id="other"):
                    yield Label("other")

    async def run_case():
        app = _TabbedLogsApp()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(0.15)
            screen = app.query_one(LogsScreen)
            screen.refresh_data()
            await _wait_for(lambda: "TAIL_END_LINE" in app.query_one("#log-viewer", TextArea).text, pilot)
            assert len(screen._log_lines) <= screen.INITIAL_LINE_LIMIT

            tabs = app.query_one("#main-tabs", TabbedContent)
            tabs.active = "other"
            await pilot.pause(0.1)
            assert screen._paused_by_hide is True

            tabs.active = "logs"
            await pilot.pause(0.1)
            assert screen._paused_by_hide is False

    asyncio.run(run_case())

    tcss = Path("src/cproxy/tui/styles.tcss").read_text(encoding="utf-8")
    assert "Toast.-warning" in tcss
    for dark_hex in ("#e8edf3", "#0d1218", "#1b2633", ".page-title"):
        assert dark_hex not in tcss
