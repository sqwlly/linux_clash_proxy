from __future__ import annotations

import os
import shutil
import stat
import threading
from datetime import datetime, timezone
from pathlib import Path

from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widget import Widget
from textual.widgets import Button, Label, Switch

from ...audit import write_audit_event
from ...config import AppPaths, read_config
from ..widgets import coerce_worker_error, escape_markup, safe_call_from_thread

_MARKER_START = "# >>> cproxy proxy >>>"
_MARKER_END = "# <<< cproxy proxy <<<"


class SystemProxyScreen(Widget):
    BINDINGS = [
        Binding("t", "toggle_proxy", "切换"),
    ]

    def __init__(self, paths: AppPaths, **kwargs):
        super().__init__(**kwargs)
        self.paths = paths
        self._pending_action: str | None = None
        self._op_running = False

    def compose(self) -> ComposeResult:
        with Vertical():
            with Horizontal(classes="compact-row"):
                with Vertical(classes="panel form-panel split-main"):
                    yield Label("临时命令范围", classes="panel-title")
                    with Horizontal(classes="field-row"):
                        yield Label("HTTP", classes="label-key")
                        yield Switch(id="switch-http", value=False)
                        yield Label("─", id="http-proxy-label", classes="metric-value")
                    with Horizontal(classes="field-row"):
                        yield Label("HTTPS", classes="label-key")
                        yield Switch(id="switch-https", value=False)
                        yield Label("─", id="https-proxy-label", classes="metric-value")
                    with Horizontal(classes="field-row"):
                        yield Label("ALL", classes="label-key")
                        yield Switch(id="switch-all", value=False)
                        yield Label("─", id="all-proxy-label", classes="metric-value")

                with Vertical(classes="panel summary-panel split-sidebar"):
                    yield Label("当前进程环境", classes="panel-title")
                    yield Label("─", id="env-status", classes="current-info")

            with Vertical(classes="panel output-panel"):
                yield Label("生成命令与持久配置", classes="panel-title")
                with Horizontal(classes="toolbar"):
                    yield Button("export", id="btn-set-all", classes="action-button success-button")
                    yield Button("unset", id="btn-clear-all", classes="action-button muted-button")
                    yield Button("写入 bash", id="btn-write-bashrc", classes="action-button primary-button")
                    yield Button("写入 zsh", id="btn-write-zshrc", classes="action-button primary-button")
                    yield Button("移除 bash", id="btn-remove-bashrc", classes="action-button danger-button")
                    yield Button("移除 zsh", id="btn-remove-zshrc", classes="action-button danger-button")
                yield Label("─", id="system-proxy-action-status", classes="action-status")

    def on_mount(self) -> None:
        if not list(self.app.query("#main-tabs")):
            self._refresh_status()

    def refresh_data(self) -> None:
        self._refresh_status()

    def _get_proxy_addr(self) -> str:
        config = read_config(self.paths)
        port = config.get("mixed-port", 7890)
        return f"127.0.0.1:{port}"

    def _clip_env(self, value: str, limit: int = 42) -> str:
        text = value or "(未设置)"
        if len(text) <= limit:
            return text
        return text[: limit - 1] + "…"

    def _switch_flags(self) -> tuple[bool, bool, bool]:
        return (
            bool(self.query_one("#switch-http", Switch).value),
            bool(self.query_one("#switch-https", Switch).value),
            bool(self.query_one("#switch-all", Switch).value),
        )

    @staticmethod
    def _build_export_lines(addr: str, flags: tuple[bool, bool, bool]) -> list[str]:
        proxy_url = f"http://{addr}"
        http_on, https_on, all_on = flags
        lines: list[str] = []
        if http_on:
            lines.append(f"export http_proxy={proxy_url}")
        if https_on:
            lines.append(f"export https_proxy={proxy_url}")
        if all_on:
            lines.append(f"export all_proxy={proxy_url}")
        if not lines:
            lines = [
                f"export http_proxy={proxy_url}",
                f"export https_proxy={proxy_url}",
                f"export all_proxy={proxy_url}",
            ]
        lines.append('export no_proxy="localhost,127.0.0.1,::1"')
        return lines

    def _refresh_status(self) -> None:
        http_proxy = os.environ.get("http_proxy") or os.environ.get("HTTP_PROXY") or ""
        https_proxy = os.environ.get("https_proxy") or os.environ.get("HTTPS_PROXY") or ""
        all_proxy = os.environ.get("all_proxy") or os.environ.get("ALL_PROXY") or ""
        no_proxy = os.environ.get("no_proxy") or os.environ.get("NO_PROXY") or ""

        self.query_one("#switch-http", Switch).value = bool(http_proxy)
        self.query_one("#switch-https", Switch).value = bool(https_proxy)
        self.query_one("#switch-all", Switch).value = bool(all_proxy)

        self.query_one("#http-proxy-label", Label).update(
            escape_markup(http_proxy) if http_proxy else "[#8b98aa](未设置)[/]"
        )
        self.query_one("#https-proxy-label", Label).update(
            escape_markup(https_proxy) if https_proxy else "[#8b98aa](未设置)[/]"
        )
        self.query_one("#all-proxy-label", Label).update(
            escape_markup(all_proxy) if all_proxy else "[#8b98aa](未设置)[/]"
        )

        env_text = (
            f"http={escape_markup(self._clip_env(http_proxy))}\n"
            f"https={escape_markup(self._clip_env(https_proxy))}\n"
            f"all={escape_markup(self._clip_env(all_proxy))}\n"
            f"no={escape_markup(self._clip_env(no_proxy, 36))}"
        )
        self.query_one("#env-status", Label).update(env_text)

    def _set_rc_buttons_disabled(self, disabled: bool) -> None:
        for selector in (
            "#btn-write-bashrc",
            "#btn-write-zshrc",
            "#btn-remove-bashrc",
            "#btn-remove-zshrc",
        ):
            self.query_one(selector, Button).disabled = disabled

    def on_button_pressed(self, event: Button.Pressed) -> None:
        status_label = self.query_one("#system-proxy-action-status", Label)

        if event.button.id == "btn-set-all":
            self._pending_action = None
            try:
                addr = self._get_proxy_addr()
            except Exception as exc:
                status_label.update(f"[#fb7185]读取配置失败: {escape_markup(exc)}[/]")
                self.notify(f"读取配置失败: {exc}", severity="error", markup=False)
                return
            export_lines = self._build_export_lines(addr, self._switch_flags())
            rendered_lines = [escape_markup(line) for line in export_lines]
            rendered_lines.append("[#8b98aa]请复制到父 shell 执行；TUI 无法修改父进程环境。[/]")
            status_label.update("\n".join(rendered_lines))
            self.notify("已生成临时代理命令", severity="information", markup=False)

        elif event.button.id == "btn-clear-all":
            self._pending_action = None
            status_label.update("unset http_proxy https_proxy all_proxy HTTP_PROXY HTTPS_PROXY ALL_PROXY")
            self.notify("已生成清理命令", severity="information", markup=False)

        elif event.button.id == "btn-write-bashrc":
            self._request_write_shell_config(Path.home() / ".bashrc", status_label)

        elif event.button.id == "btn-write-zshrc":
            self._request_write_shell_config(Path.home() / ".zshrc", status_label)

        elif event.button.id == "btn-remove-bashrc":
            self._request_remove_shell_config(Path.home() / ".bashrc", status_label)

        elif event.button.id == "btn-remove-zshrc":
            self._request_remove_shell_config(Path.home() / ".zshrc", status_label)

    def _request_write_shell_config(self, shell_rc: Path, status_label: Label) -> None:
        if self._op_running:
            return
        action = f"write:{shell_rc}"
        if self._pending_action != action:
            self._pending_action = action
            status_label.update(f"[#f6c177]将修改 {escape_markup(shell_rc)}；请再次点击同一按钮确认[/]")
            return
        self._pending_action = None
        self._op_running = True
        self._set_rc_buttons_disabled(True)
        status_label.update(f"[#f6c177]正在写入 {escape_markup(shell_rc)}…[/]")
        self._write_shell_config_worker(shell_rc, self._switch_flags())

    @work(thread=True, exclusive=True, group="system-proxy-ops")
    def _write_shell_config_worker(self, shell_rc: Path, flags: tuple[bool, bool, bool]) -> None:
        try:
            addr = self._get_proxy_addr()
            export_lines = self._build_export_lines(addr, flags)
            backup = self._apply_write_shell_config(shell_rc, export_lines)
            if not safe_call_from_thread(self, self._finish_write_shell_config, shell_rc, backup, None):
                self._op_running = False
        except (Exception, SystemExit) as exc:
            if not safe_call_from_thread(self, self._finish_write_shell_config, shell_rc, None, coerce_worker_error(exc)):
                self._op_running = False

    def _finish_write_shell_config(self, shell_rc: Path, backup: Path | None, error: Exception | None) -> None:
        self._op_running = False
        if not self.is_mounted:
            return
        self._set_rc_buttons_disabled(False)
        status_label = self.query_one("#system-proxy-action-status", Label)
        if error is None:
            backup_text = f"；备份 {escape_markup(backup)}" if backup else ""
            rc_escaped = escape_markup(shell_rc)
            status_label.update(f"[#a3e635]已写入 {rc_escaped}{backup_text}[/]；执行 source {rc_escaped} 生效")
            self.notify(f"已写入 {shell_rc.name}", severity="information", markup=False)
        else:
            status_label.update(f"[#fb7185]写入失败: {escape_markup(error)}[/]")
            self.notify(f"写入失败: {error}", severity="error", markup=False)

    def _request_remove_shell_config(self, shell_rc: Path, status_label: Label) -> None:
        if self._op_running:
            return
        action = f"remove:{shell_rc}"
        if self._pending_action != action:
            self._pending_action = action
            status_label.update(f"[#f6c177]将移除 {escape_markup(shell_rc)} 中的 cproxy 管理块；请再次点击确认[/]")
            return
        self._pending_action = None
        self._op_running = True
        self._set_rc_buttons_disabled(True)
        status_label.update(f"[#f6c177]正在清理 {escape_markup(shell_rc)}…[/]")
        self._remove_shell_config_worker(shell_rc)

    @work(thread=True, exclusive=True, group="system-proxy-ops")
    def _remove_shell_config_worker(self, shell_rc: Path) -> None:
        try:
            removed, backup = self._apply_remove_shell_config(shell_rc)
            if not safe_call_from_thread(self, self._finish_remove_shell_config, shell_rc, removed, backup, None):
                self._op_running = False
        except (Exception, SystemExit) as exc:
            if not safe_call_from_thread(
                self, self._finish_remove_shell_config, shell_rc, False, None, coerce_worker_error(exc)
            ):
                self._op_running = False

    def _finish_remove_shell_config(
        self, shell_rc: Path, removed: bool, backup: Path | None, error: Exception | None
    ) -> None:
        self._op_running = False
        if not self.is_mounted:
            return
        self._set_rc_buttons_disabled(False)
        status_label = self.query_one("#system-proxy-action-status", Label)
        if error is not None:
            status_label.update(f"[#fb7185]移除失败: {escape_markup(error)}[/]")
            self.notify(f"移除失败: {error}", severity="error", markup=False)
        elif not removed:
            status_label.update(f"[#8b98aa]{escape_markup(shell_rc)} 中没有 cproxy 管理块[/]")
        else:
            status_label.update(f"[#a3e635]已移除 cproxy 管理块；备份 {escape_markup(backup)}[/]")
            self.notify(f"已清理 {shell_rc.name}", severity="information", markup=False)

    def _write_shell_config(self, shell_rc: Path, addr: str, status_label: Label) -> None:
        """同步写入入口（供单元测试直接验证 2-click 确认与写入语义）。"""
        action = f"write:{shell_rc}"
        if self._pending_action != action:
            self._pending_action = action
            status_label.update(f"[#f6c177]将修改 {escape_markup(shell_rc)}；请再次点击同一按钮确认[/]")
            return

        self._pending_action = None
        try:
            export_lines = self._build_export_lines(addr, self._switch_flags())
            backup = self._apply_write_shell_config(shell_rc, export_lines)
            backup_text = f"；备份 {escape_markup(backup)}" if backup else ""
            rc_escaped = escape_markup(shell_rc)
            status_label.update(f"[#a3e635]已写入 {rc_escaped}{backup_text}[/]；执行 source {rc_escaped} 生效")
            self.notify(f"已写入 {shell_rc.name}", severity="information", markup=False)
        except Exception as exc:
            status_label.update(f"[#fb7185]写入失败: {escape_markup(exc)}[/]")
            self.notify(f"写入失败: {exc}", severity="error", markup=False)

    def _remove_shell_config(self, shell_rc: Path, status_label: Label) -> None:
        """同步移除入口（供单元测试直接验证 2-click 确认与移除语义）。"""
        action = f"remove:{shell_rc}"
        if self._pending_action != action:
            self._pending_action = action
            status_label.update(f"[#f6c177]将移除 {escape_markup(shell_rc)} 中的 cproxy 管理块；请再次点击确认[/]")
            return
        self._pending_action = None
        try:
            removed, backup = self._apply_remove_shell_config(shell_rc)
            if not removed:
                status_label.update(f"[#8b98aa]{escape_markup(shell_rc)} 中没有 cproxy 管理块[/]")
                return
            status_label.update(f"[#a3e635]已移除 cproxy 管理块；备份 {escape_markup(backup)}[/]")
            self.notify(f"已清理 {shell_rc.name}", severity="information", markup=False)
        except Exception as exc:
            status_label.update(f"[#fb7185]移除失败: {escape_markup(exc)}[/]")
            self.notify(f"移除失败: {exc}", severity="error", markup=False)

    @staticmethod
    def _consume_marker_end(content: str, end_idx: int) -> int:
        end = end_idx + len(_MARKER_END)
        if content[end : end + 2] == "\r\n":
            return end + 2
        if content[end : end + 1] == "\n":
            return end + 1
        return end

    def _apply_write_shell_config(self, shell_rc: Path, export_lines: list[str]) -> Path | None:
        block_body = "\n".join([_MARKER_START, *export_lines, _MARKER_END]) + "\n"
        existing = shell_rc.read_text(encoding="utf-8") if shell_rc.exists() else ""

        if _MARKER_START in existing:
            start = existing.index(_MARKER_START)
            end_idx = existing.find(_MARKER_END, start)
            if end_idx == -1:
                raise ValueError(f"cproxy 管理块标记不完整（缺少结束标记 {_MARKER_END}），已中止写入以避免损坏文件")
            end = self._consume_marker_end(existing, end_idx)
            updated = existing[:start] + block_body + existing[end:]
        elif existing:
            updated = existing.rstrip() + "\n\n" + block_body
        else:
            updated = block_body

        backup = self._backup_shell_config(shell_rc)
        self._atomic_write(shell_rc, updated)
        write_audit_event(
            self.paths,
            action="write_shell_proxy",
            target=str(shell_rc),
            result="ok",
            detail={"backup": str(backup) if backup else ""},
        )
        return backup

    def _apply_remove_shell_config(self, shell_rc: Path) -> tuple[bool, Path | None]:
        existing = shell_rc.read_text(encoding="utf-8") if shell_rc.exists() else ""
        if _MARKER_START not in existing:
            return False, None
        start = existing.index(_MARKER_START)
        end_idx = existing.find(_MARKER_END, start)
        if end_idx == -1:
            raise ValueError(f"cproxy 管理块标记不完整（缺少结束标记 {_MARKER_END}），已中止移除以避免损坏文件")
        end = self._consume_marker_end(existing, end_idx)
        updated = (existing[:start].rstrip() + "\n" + existing[end:].lstrip("\n")).lstrip("\n")
        backup = self._backup_shell_config(shell_rc)
        self._atomic_write(shell_rc, updated)
        write_audit_event(
            self.paths,
            action="remove_shell_proxy",
            target=str(shell_rc),
            result="ok",
            detail={"backup": str(backup) if backup else ""},
        )
        return True, backup

    def _backup_shell_config(self, shell_rc: Path) -> Path | None:
        if not shell_rc.exists():
            return None
        backup_dir = self.paths.state_dir / "shell-backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        backup = backup_dir / f"{shell_rc.name.lstrip('.')}-{stamp}.bak"
        shutil.copy2(shell_rc.resolve(), backup)
        os.chmod(backup, 0o600)
        return backup

    @staticmethod
    def _atomic_write(path: Path, content: str) -> None:
        target = path.resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        existing_mode: int | None = None
        try:
            existing_mode = stat.S_IMODE(target.stat().st_mode)
        except FileNotFoundError:
            existing_mode = None
        temp_path = target.with_name(f".{target.name}.cproxy-tmp-{os.getpid()}-{threading.get_ident()}")
        try:
            temp_path.write_text(content, encoding="utf-8")
            if existing_mode is not None:
                os.chmod(temp_path, existing_mode)
            os.replace(temp_path, target)
        finally:
            temp_path.unlink(missing_ok=True)

    def action_toggle_proxy(self) -> None:
        switches = [
            self.query_one("#switch-http", Switch),
            self.query_one("#switch-https", Switch),
            self.query_one("#switch-all", Switch),
        ]
        new_value = not all(item.value for item in switches)
        for item in switches:
            item.value = new_value
