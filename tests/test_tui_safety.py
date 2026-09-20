from __future__ import annotations

import asyncio
import os

import yaml
from textual.app import App, ComposeResult
from textual.widgets import Label

from cproxy.config import AppPaths
from cproxy.tui.screens.config_editor import ConfigEditorScreen
from cproxy.tui.screens.system_proxy import SystemProxyScreen
from cproxy.tui.widgets import NavigationTextArea as TextArea


class _ConfigApp(App):
    def __init__(self, paths: AppPaths):
        super().__init__()
        self.paths = paths

    def compose(self) -> ComposeResult:
        yield ConfigEditorScreen(self.paths)


class _SystemProxyApp(App):
    def __init__(self, paths: AppPaths):
        super().__init__()
        self.paths = paths

    def compose(self) -> ComposeResult:
        yield SystemProxyScreen(self.paths)


def _paths(tmp_path) -> AppPaths:
    return AppPaths(tmp_path / "config", tmp_path / "data", tmp_path / "state")


def test_config_editor_starts_clean_and_invalid_yaml_does_not_overwrite(tmp_path):
    paths = _paths(tmp_path)
    paths.config_dir.mkdir(parents=True)
    config_path = paths.config_dir / "config.yaml"
    original = "mixed-port: 7890\nproxy-groups: []\n"
    config_path.write_text(original, encoding="utf-8")

    async def run_case():
        app = _ConfigApp(paths)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(0.1)
            screen = app.query_one(ConfigEditorScreen)
            assert screen._modified is False
            editor = app.query_one("#config-editor", TextArea)
            editor.load_text("mixed-port: [\n")
            await pilot.pause(0.05)
            screen.action_save_config()
            assert config_path.read_text(encoding="utf-8") == original
            assert "保存失败，原文件未变更" in str(app.query_one("#config-log", Label).render())

    asyncio.run(run_case())


def test_config_editor_valid_save_is_atomic_and_snapshotted(tmp_path):
    paths = _paths(tmp_path)
    paths.config_dir.mkdir(parents=True)
    config_path = paths.config_dir / "config.yaml"
    config_path.write_text("mixed-port: 7890\n", encoding="utf-8")

    async def run_case():
        app = _ConfigApp(paths)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(0.1)
            screen = app.query_one(ConfigEditorScreen)
            editor = app.query_one("#config-editor", TextArea)
            editor.load_text("mixed-port: 7891\nmode: rule\n")
            await pilot.pause(0.05)
            screen.action_save_config()
            assert yaml.safe_load(config_path.read_text(encoding="utf-8"))["mixed-port"] == 7891
            assert config_path.stat().st_mode & 0o777 == 0o600
            snapshots = list((paths.state_dir / "snapshots").glob("config-*.yaml"))
            assert len(snapshots) == 1
            assert snapshots[0].read_text(encoding="utf-8") == "mixed-port: 7890\n"

    asyncio.run(run_case())


def test_system_proxy_shell_changes_require_confirmation_and_are_reversible(tmp_path):
    paths = _paths(tmp_path)
    shell_rc = tmp_path / ".zshrc"
    shell_rc.write_text("export KEEP=1\n", encoding="utf-8")

    async def run_case():
        app = _SystemProxyApp(paths)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(0.1)
            screen = app.query_one(SystemProxyScreen)
            status = app.query_one("#proxy-action-status", Label)

            screen._write_shell_config(shell_rc, "127.0.0.1:7890", status)
            assert shell_rc.read_text(encoding="utf-8") == "export KEEP=1\n"
            screen._write_shell_config(shell_rc, "127.0.0.1:7890", status)
            assert "# >>> cproxy proxy >>>" in shell_rc.read_text(encoding="utf-8")
            assert list((paths.state_dir / "shell-backups").glob("zshrc-*.bak"))

            screen._remove_shell_config(shell_rc, status)
            assert "# >>> cproxy proxy >>>" in shell_rc.read_text(encoding="utf-8")
            screen._remove_shell_config(shell_rc, status)
            assert shell_rc.read_text(encoding="utf-8") == "export KEEP=1\n"

    asyncio.run(run_case())


def test_system_proxy_export_button_does_not_mutate_parent_environment(tmp_path, monkeypatch):
    paths = _paths(tmp_path)
    for name in ("http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.delenv(name, raising=False)
    before = dict(os.environ)

    async def run_case():
        app = _SystemProxyApp(paths)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(0.1)
            await pilot.click("#btn-set-all")
            assert "TUI 无法修改父进程环境" in str(app.query_one("#proxy-action-status", Label).render())

    asyncio.run(run_case())
    assert dict(os.environ) == before
