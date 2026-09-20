from __future__ import annotations

import os
import shutil
from datetime import datetime, timezone
from pathlib import Path

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widget import Widget
from textual.widgets import Button, Label, Switch

from ...audit import write_audit_event
from ...config import AppPaths, read_config


class SystemProxyScreen(Widget):
    BINDINGS = [
        Binding("t", "toggle_proxy", "切换"),
    ]

    def __init__(self, paths: AppPaths, **kwargs):
        super().__init__(**kwargs)
        self.paths = paths
        self._pending_action: str | None = None

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
                yield Label("─", id="proxy-action-status", classes="action-status")

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

    def _refresh_status(self) -> None:
        http_proxy = os.environ.get("http_proxy", "")
        https_proxy = os.environ.get("https_proxy", "")
        all_proxy = os.environ.get("all_proxy", "")
        no_proxy = os.environ.get("no_proxy", "")

        self.query_one("#switch-http", Switch).value = bool(http_proxy)
        self.query_one("#switch-https", Switch).value = bool(https_proxy)
        self.query_one("#switch-all", Switch).value = bool(all_proxy)

        self.query_one("#http-proxy-label", Label).update(
            http_proxy or "[#8b98aa](未设置)[/]"
        )
        self.query_one("#https-proxy-label", Label).update(
            https_proxy or "[#8b98aa](未设置)[/]"
        )
        self.query_one("#all-proxy-label", Label).update(
            all_proxy or "[#8b98aa](未设置)[/]"
        )

        env_text = (
            f"http={self._clip_env(http_proxy)}\n"
            f"https={self._clip_env(https_proxy)}\n"
            f"all={self._clip_env(all_proxy)}\n"
            f"no={self._clip_env(no_proxy, 36)}"
        )
        self.query_one("#env-status", Label).update(env_text)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        status_label = self.query_one("#proxy-action-status", Label)
        addr = self._get_proxy_addr()

        if event.button.id == "btn-set-all":
            proxy_url = f"http://{addr}"
            status_label.update(
                "\n".join(
                    [
                        f"export http_proxy={proxy_url}",
                        f"export https_proxy={proxy_url}",
                        f"export all_proxy={proxy_url}",
                        'export no_proxy="localhost,127.0.0.1,::1"',
                        "[#8b98aa]请复制到父 shell 执行；TUI 无法修改父进程环境。[/]",
                    ]
                )
            )
            self.notify("已生成临时代理命令", severity="information")

        elif event.button.id == "btn-clear-all":
            status_label.update("unset http_proxy https_proxy all_proxy HTTP_PROXY HTTPS_PROXY ALL_PROXY")
            self.notify("已生成清理命令", severity="information")

        elif event.button.id == "btn-write-bashrc":
            self._write_shell_config(Path.home() / ".bashrc", addr, status_label)

        elif event.button.id == "btn-write-zshrc":
            self._write_shell_config(Path.home() / ".zshrc", addr, status_label)

        elif event.button.id == "btn-remove-bashrc":
            self._remove_shell_config(Path.home() / ".bashrc", status_label)

        elif event.button.id == "btn-remove-zshrc":
            self._remove_shell_config(Path.home() / ".zshrc", status_label)

    def _write_shell_config(self, shell_rc: Path, addr: str, status_label: Label) -> None:
        proxy_url = f"http://{addr}"
        marker = "# >>> cproxy proxy >>>"
        marker_end = "# <<< cproxy proxy <<<"
        block = (
            f"\n{marker}\n"
            f"export http_proxy={proxy_url}\n"
            f"export https_proxy={proxy_url}\n"
            f"export all_proxy={proxy_url}\n"
            f'export no_proxy="localhost,127.0.0.1,::1"\n'
            f"{marker_end}\n"
        )

        action = f"write:{shell_rc}"
        if self._pending_action != action:
            self._pending_action = action
            status_label.update(f"[#f6c177]将修改 {shell_rc}；请再次点击同一按钮确认[/]")
            return

        try:
            self._pending_action = None
            existing = shell_rc.read_text(encoding="utf-8") if shell_rc.exists() else ""

            if marker in existing:
                start = existing.index(marker)
                end = existing.index(marker_end) + len(marker_end) + 1
                existing = existing[:start] + block.strip() + "\n" + existing[end:]
            else:
                existing = existing.rstrip() + "\n" + block

            backup = self._backup_shell_config(shell_rc)
            self._atomic_write(shell_rc, existing)
            write_audit_event(
                self.paths,
                action="write_shell_proxy",
                target=str(shell_rc),
                result="ok",
                detail={"backup": str(backup) if backup else ""},
            )
            backup_text = f"；备份 {backup}" if backup else ""
            status_label.update(f"[#a3e635]已写入 {shell_rc}{backup_text}[/]；执行 source {shell_rc} 生效")
            self.notify(f"已写入 {shell_rc.name}", severity="information")

        except Exception as e:
            status_label.update(f"[#fb7185]写入失败: {e}[/]")
            self.notify(f"写入失败: {e}", severity="error")

    def _remove_shell_config(self, shell_rc: Path, status_label: Label) -> None:
        action = f"remove:{shell_rc}"
        if self._pending_action != action:
            self._pending_action = action
            status_label.update(f"[#f6c177]将移除 {shell_rc} 中的 cproxy 管理块；请再次点击确认[/]")
            return
        self._pending_action = None
        marker = "# >>> cproxy proxy >>>"
        marker_end = "# <<< cproxy proxy <<<"
        try:
            existing = shell_rc.read_text(encoding="utf-8") if shell_rc.exists() else ""
            if marker not in existing or marker_end not in existing:
                status_label.update(f"[#8b98aa]{shell_rc} 中没有 cproxy 管理块[/]")
                return
            start = existing.index(marker)
            end = existing.index(marker_end, start) + len(marker_end)
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
            status_label.update(f"[#a3e635]已移除 cproxy 管理块；备份 {backup}[/]")
            self.notify(f"已清理 {shell_rc.name}", severity="information")
        except Exception as e:
            status_label.update(f"[#fb7185]移除失败: {e}[/]")
            self.notify(f"移除失败: {e}", severity="error")

    def _backup_shell_config(self, shell_rc: Path) -> Path | None:
        if not shell_rc.exists():
            return None
        backup_dir = self.paths.state_dir / "shell-backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        backup = backup_dir / f"{shell_rc.name.lstrip('.')}-{stamp}.bak"
        shutil.copy2(shell_rc, backup)
        os.chmod(backup, 0o600)
        return backup

    @staticmethod
    def _atomic_write(path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = path.with_name(f".{path.name}.cproxy-tmp-{os.getpid()}")
        try:
            temp_path.write_text(content, encoding="utf-8")
            os.chmod(temp_path, 0o600)
            os.replace(temp_path, path)
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
