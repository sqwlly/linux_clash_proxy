from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace

from textual.app import App, ComposeResult
from textual.widgets import Button, TextArea

from cproxy.config import AppPaths
from cproxy.tui.screens import subscriptions as subscriptions_module
from cproxy.tui.screens.subscriptions import (
    SubscriptionsScreen,
    redact_subscription_url,
    subscription_group_rows,
)


class _ScreenApp(App):
    def __init__(self, screen):
        super().__init__()
        self._body = screen

    def compose(self) -> ComposeResult:
        yield self._body


async def _wait_for(predicate, pilot, timeout: float = 5.0) -> None:
    for _ in range(int(timeout / 0.05)):
        if predicate():
            return
        await pilot.pause(0.05)
    raise AssertionError("等待超时")


def test_subscription_group_rows_show_attach_relationships():
    rows = subscription_group_rows(
        {
            "proxy-groups": [
                {"name": "AI-MANUAL", "type": "select", "proxies": ["AI-AUTO", "CyberGuard"]},
                {"name": "CyberGuard", "type": "select", "proxies": ["CyberGuard-Auto", "Node A", "DIRECT"]},
                {"name": "CyberGuard-Auto", "type": "fallback", "proxies": ["Node A", "Node B"]},
            ]
        }
    )

    assert ("AI-MANUAL", "select", "2", "─") in rows
    assert ("CyberGuard", "select", "3", "AI-MANUAL") in rows
    assert ("CyberGuard-Auto", "fallback", "2", "CyberGuard") in rows


def test_redact_subscription_url_hides_query_token():
    assert redact_subscription_url("https://example.test/api/v1/client/subscribe?token=secret") == (
        "https://example.test/api/v1/client/subscribe?..."
    )


def test_preview_subscription_runs_in_worker_with_staged_output(monkeypatch, tmp_path):
    started = threading.Event()
    release = threading.Event()

    def fake_preview(paths, url, timeout=0):
        started.set()
        assert release.wait(5), "worker 未被释放"
        return SimpleNamespace(source="yaml", bytes=1234, proxy_count=5, group_count=2)

    monkeypatch.setattr(subscriptions_module, "preview_subscription", fake_preview)
    paths = AppPaths(tmp_path / "config", tmp_path / "data", tmp_path / "state")

    async def run_case():
        app = _ScreenApp(SubscriptionsScreen(paths))
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(0.1)
            screen = app.query_one(SubscriptionsScreen)
            app.query_one("#sub-url-input").value = "https://example.test/sub?token=secret"

            screen._import_subscription(dry_run=True)
            await _wait_for(lambda: screen._subscription_running, pilot)
            await _wait_for(started.is_set, pilot)

            output = app.query_one("#sub-output", TextArea)
            assert "正在预览订阅…" in output.text
            assert "阶段: 下载并解析订阅…" in output.text
            for selector in ("#btn-sub-preview", "#btn-sub-apply", "#btn-sub-update"):
                assert app.query_one(selector, Button).disabled is True

            release.set()
            await _wait_for(lambda: not screen._subscription_running, pilot)
            await pilot.pause(0.1)

            assert "预览: 正常" in output.text
            assert "节点: 5" in output.text
            assert "阶段:" not in output.text
            for selector in ("#btn-sub-preview", "#btn-sub-apply", "#btn-sub-update"):
                assert app.query_one(selector, Button).disabled is False

    asyncio.run(run_case())


def test_validate_config_shows_stages_and_result(monkeypatch, tmp_path):
    entered_render = threading.Event()
    release = threading.Event()

    class FakeRuntimeBackend:
        def __init__(self, paths):
            self.paths = paths

        def render_runtime(self):
            entered_render.set()
            assert release.wait(5), "render_runtime 未被释放"
            return "fake-runtime.yaml"

    monkeypatch.setattr(subscriptions_module, "RuntimeBackend", FakeRuntimeBackend)
    paths = AppPaths(tmp_path / "config", tmp_path / "data", tmp_path / "state")
    paths.config_dir.mkdir(parents=True)
    (paths.config_dir / "config.yaml").write_text("mixed-port: 7890\n", encoding="utf-8")

    async def run_case():
        app = _ScreenApp(SubscriptionsScreen(paths))
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(0.1)
            screen = app.query_one(SubscriptionsScreen)
            screen._update_config()
            assert screen._subscription_running is True

            output = app.query_one("#sub-output", TextArea)
            assert "正在隔离目录中验证配置" in output.text
            assert "阶段: 复制配置到隔离目录…" in output.text

            await _wait_for(entered_render.is_set, pilot)
            await _wait_for(lambda: "渲染隔离运行配置…" in output.text, pilot)

            release.set()
            await _wait_for(lambda: not screen._subscription_running, pilot)
            await pilot.pause(0.1)

            assert "验证通过" in output.text
            assert "fake-runtime.yaml" in output.text
            assert "阶段:" not in output.text

    asyncio.run(run_case())
