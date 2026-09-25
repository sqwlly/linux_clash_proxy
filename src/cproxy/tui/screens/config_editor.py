from __future__ import annotations

import os
from pathlib import Path

import yaml
from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widget import Widget
from textual.widgets import Button, Label

from ...audit import write_audit_event
from ...config import AppPaths, config_file
from ...process import restart_process
from ...runtime import render_runtime
from ...snapshots import snapshot_file
from ..widgets import NavigationTextArea as TextArea


class ConfigEditorScreen(Widget):
    BINDINGS = [
        Binding("ctrl+s", "save_config", "保存", priority=True),
        Binding("f6", "render_config", "生成配置", priority=True),
    ]

    def __init__(self, paths: AppPaths, **kwargs):
        super().__init__(**kwargs)
        self.paths = paths
        self._modified = False
        self._loaded_text = ""
        self._pending_action: str | None = None
        self._op_running = False

    def compose(self) -> ComposeResult:
        with Vertical():
            with Vertical(classes="panel output-panel"):
                with Horizontal(classes="panel-header"):
                    yield Label("源配置", classes="panel-title")
                    yield Label("─", id="config-file-label", classes="path-label")
                    yield Label("", id="config-modified-label", classes="status-strip")
                with Horizontal(classes="toolbar"):
                    yield Button("保存", id="btn-config-save", classes="action-button success-button")
                    yield Button("生成", id="btn-config-render", classes="action-button primary-button")
                    yield Button("重启", id="btn-config-restart", classes="action-button danger-button")
                    yield Button("载入", id="btn-config-reload", classes="action-button muted-button")
                yield TextArea(id="config-editor", classes="config-editor")
                yield Label("─", id="config-log", classes="action-status")

    def on_mount(self) -> None:
        if not list(self.app.query("#main-tabs")):
            self._load_config()

    def refresh_data(self) -> None:
        if self._modified:
            self.query_one("#config-log", Label).update("[#f6c177]配置有未保存修改，已跳过自动刷新[/]")
            return
        self._load_config()

    def _load_config(self) -> None:
        config_path = config_file(self.paths)
        self.query_one("#config-file-label", Label).update(f"[#8b98aa]{config_path}[/]")
        self.query_one("#config-modified-label", Label).update("")

        editor = self.query_one("#config-editor", TextArea)
        try:
            editor.theme = "monokai"
        except Exception:
            pass

        if config_path.exists():
            content = config_path.read_text(encoding="utf-8")
        else:
            content = f"# 配置不存在: {config_path}\n# 请运行 cproxy init 初始化"

        self._loaded_text = content
        editor.load_text(content)
        self._modified = False
        self._pending_action = None
        self.query_one("#config-modified-label", Label).update("")

    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        self._modified = event.text_area.text != self._loaded_text
        label = "[#f6c177](已修改)[/]" if self._modified else ""
        self.query_one("#config-modified-label", Label).update(label)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-config-save":
            self.action_save_config()
        elif event.button.id == "btn-config-render":
            self.action_render_config()
        elif event.button.id == "btn-config-restart":
            self.action_restart_config()
        elif event.button.id == "btn-config-reload":
            self.action_reload_config()

    def _set_op_buttons_disabled(self, disabled: bool) -> None:
        for button_id in ("btn-config-save", "btn-config-render", "btn-config-restart", "btn-config-reload"):
            self.query_one(f"#{button_id}", Button).disabled = disabled

    def action_save_config(self) -> None:
        if self._op_running:
            return
        editor_text = self.query_one("#config-editor", TextArea).text
        self.query_one("#config-log", Label).update("[#f6c177]正在保存…[/]")
        self._op_running = True
        self._set_op_buttons_disabled(True)
        self._save_config(editor_text)

    @work(thread=True, exclusive=True, group="config-ops")
    def _save_config(self, text: str) -> None:
        error: Exception | None = None
        try:
            parsed = yaml.safe_load(text)
            if not isinstance(parsed, dict):
                raise ValueError("YAML 顶层必须是映射")
            config_path = config_file(self.paths)
            config_path.parent.mkdir(parents=True, exist_ok=True)
            snapshot_file(self.paths, config_path, "config")
            temp_path = config_path.with_name(f".{config_path.name}.tmp-{os.getpid()}")
            try:
                temp_path.write_text(text, encoding="utf-8")
                os.chmod(temp_path, 0o600)
                os.replace(temp_path, config_path)
            finally:
                temp_path.unlink(missing_ok=True)
        except Exception as exc:
            error = exc
        self.app.call_from_thread(self._finish_save_config, text, error)

    def _finish_save_config(self, text: str, error: Exception | None) -> None:
        self._op_running = False
        self._set_op_buttons_disabled(False)
        log_label = self.query_one("#config-log", Label)
        if error is None:
            self._loaded_text = text
            self._modified = False
            self._pending_action = None
            config_path = config_file(self.paths)
            self.query_one("#config-modified-label", Label).update("[#a3e635](已保存)[/]")
            log_label.update(f"[#a3e635]已校验并保存: {config_path}[/]")
            self.notify("配置已校验、快照并保存", severity="information")
        else:
            log_label.update(f"[#fb7185]保存失败，原文件未变更: {error}[/]")
            self.notify(f"保存失败: {error}", severity="error")

    def action_render_config(self) -> None:
        if self._op_running:
            return
        log_label = self.query_one("#config-log", Label)

        if self._modified:
            log_label.update("[#f6c177]请先保存配置，再生成运行配置[/]")
            return

        log_label.update("[#f6c177]正在生成配置…[/]")
        self._op_running = True
        self._set_op_buttons_disabled(True)
        self._render_config()

    @work(thread=True, exclusive=True, group="config-ops")
    def _render_config(self) -> None:
        error: Exception | None = None
        runtime_path: Path | None = None
        try:
            runtime_path = render_runtime(self.paths)
        except Exception as exc:
            error = exc
        self.app.call_from_thread(self._finish_render_config, runtime_path, error)

    def _finish_render_config(self, runtime_path: Path | None, error: Exception | None) -> None:
        self._op_running = False
        self._set_op_buttons_disabled(False)
        log_label = self.query_one("#config-log", Label)
        if error is None:
            log_label.update(f"[#a3e635]已生成: {runtime_path}[/]")
            self.notify("运行配置已生成", severity="information")
        else:
            log_label.update(f"[#fb7185]生成失败: {error}[/]")
            self.notify(f"生成失败: {error}", severity="error")

    def action_restart_config(self) -> None:
        if self._op_running:
            return
        log_label = self.query_one("#config-log", Label)

        if self._modified:
            log_label.update("[#f6c177]请先保存配置，再重启应用[/]")
            return
        if self._pending_action != "restart":
            self._pending_action = "restart"
            log_label.update("[#f6c177]重启会中断现有连接；请再次点击“重启并应用”确认[/]")
            return

        self._pending_action = None
        log_label.update("[#f6c177]正在生成配置并重启…[/]")
        self._op_running = True
        self._set_op_buttons_disabled(True)
        self._restart_config()

    @work(thread=True, exclusive=True, group="config-ops")
    def _restart_config(self) -> None:
        error: Exception | None = None
        runtime_path: Path | None = None
        pid: int | None = None
        try:
            runtime_path = render_runtime(self.paths)
            pid = restart_process(self.paths)
            write_audit_event(
                self.paths,
                action="config_restart",
                target=str(runtime_path),
                result="ok",
                detail={"pid": pid},
            )
        except Exception as exc:
            error = exc
        self.app.call_from_thread(self._finish_restart_config, runtime_path, pid, error)

    def _finish_restart_config(self, runtime_path: Path | None, pid: int | None, error: Exception | None) -> None:
        self._op_running = False
        self._set_op_buttons_disabled(False)
        log_label = self.query_one("#config-log", Label)
        if error is None:
            log_label.update(f"[#a3e635]已生成 {runtime_path}；代理已重启，PID {pid}[/]")
            self.notify("运行配置已生成并重启代理", severity="information")
        else:
            log_label.update(f"[#fb7185]重启失败: {error}[/]")
            self.notify(f"重启失败: {error}", severity="error")

    def action_reload_config(self) -> None:
        if self._op_running:
            return
        log_label = self.query_one("#config-log", Label)
        if self._modified and self._pending_action != "reload":
            self._pending_action = "reload"
            log_label.update("[#f6c177]重新载入会丢弃未保存内容；请再次点击确认[/]")
            return
        self._load_config()
        log_label.update("[#a3e635]已重新载入磁盘配置[/]")
