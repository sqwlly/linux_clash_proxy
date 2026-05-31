from __future__ import annotations

import os
from pathlib import Path

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widget import Widget
from textual.widgets import Button, Label, Switch

from ...config import AppPaths, read_config


class SystemProxyScreen(Widget):
    BINDINGS = [
        Binding("t", "toggle_proxy", "Toggle"),
    ]

    def __init__(self, paths: AppPaths, **kwargs):
        super().__init__(**kwargs)
        self.paths = paths

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label("System Proxy", classes="page-title")

            with Horizontal(classes="workbench-row compact-row"):
                with Vertical(classes="panel form-panel split-main"):
                    yield Label("Session", classes="panel-title")
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
                    yield Label("Environment", classes="panel-title")
                    yield Label("─", id="env-status", classes="current-info")

            with Vertical(classes="panel output-panel"):
                yield Label("Persist", classes="panel-title")
                with Horizontal(classes="toolbar"):
                    yield Button("Set Session", id="btn-set-all", classes="action-button success-button")
                    yield Button("Clear Session", id="btn-clear-all", classes="action-button danger-button")
                    yield Button("Write bashrc", id="btn-write-bashrc", classes="action-button primary-button")
                    yield Button("Write zshrc", id="btn-write-zshrc", classes="action-button primary-button")
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

    def _refresh_status(self) -> None:
        http_proxy = os.environ.get("http_proxy", "")
        https_proxy = os.environ.get("https_proxy", "")
        all_proxy = os.environ.get("all_proxy", "")

        self.query_one("#switch-http", Switch).value = bool(http_proxy)
        self.query_one("#switch-https", Switch).value = bool(https_proxy)
        self.query_one("#switch-all", Switch).value = bool(all_proxy)

        self.query_one("#http-proxy-label", Label).update(http_proxy or "[#8b98aa](not set)[/]")
        self.query_one("#https-proxy-label", Label).update(https_proxy or "[#8b98aa](not set)[/]")
        self.query_one("#all-proxy-label", Label).update(all_proxy or "[#8b98aa](not set)[/]")

        env_text = (
            f"http_proxy={http_proxy or '(not set)'}\n"
            f"https_proxy={https_proxy or '(not set)'}\n"
            f"all_proxy={all_proxy or '(not set)'}\n"
            f"no_proxy={os.environ.get('no_proxy', '(not set)')}"
        )
        self.query_one("#env-status", Label).update(env_text)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        status_label = self.query_one("#proxy-action-status", Label)
        addr = self._get_proxy_addr()

        if event.button.id == "btn-set-all":
            proxy_url = f"http://{addr}"
            os.environ["http_proxy"] = proxy_url
            os.environ["https_proxy"] = proxy_url
            os.environ["all_proxy"] = proxy_url
            status_label.update(f"[#a3e635]Set (session only): {proxy_url}[/]")
            self._refresh_status()
            self.notify(f"Proxy set: {addr}", severity="information")

        elif event.button.id == "btn-clear-all":
            for key in ("http_proxy", "https_proxy", "all_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
                os.environ.pop(key, None)
            status_label.update("[#a3e635]Cleared (session only)[/]")
            self._refresh_status()
            self.notify("Proxy cleared", severity="information")

        elif event.button.id == "btn-write-bashrc":
            self._write_shell_config(Path.home() / ".bashrc", addr, status_label)

        elif event.button.id == "btn-write-zshrc":
            self._write_shell_config(Path.home() / ".zshrc", addr, status_label)

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

        try:
            existing = shell_rc.read_text(encoding="utf-8") if shell_rc.exists() else ""

            if marker in existing:
                start = existing.index(marker)
                end = existing.index(marker_end) + len(marker_end) + 1
                existing = existing[:start] + block.strip() + "\n" + existing[end:]
            else:
                existing = existing.rstrip() + "\n" + block

            shell_rc.write_text(existing, encoding="utf-8")
            status_label.update(f"[#a3e635]Written to {shell_rc}[/] (run 'source {shell_rc}' to apply)")
            self.notify(f"Written to {shell_rc.name}", severity="information")

        except Exception as e:
            status_label.update(f"[#fb7185]Write failed: {e}[/]")
            self.notify(f"Write failed: {e}", severity="error")

    def action_toggle_proxy(self) -> None:
        addr = self._get_proxy_addr()
        if os.environ.get("all_proxy"):
            for key in ("http_proxy", "https_proxy", "all_proxy"):
                os.environ.pop(key, None)
        else:
            proxy_url = f"http://{addr}"
            os.environ["http_proxy"] = proxy_url
            os.environ["https_proxy"] = proxy_url
            os.environ["all_proxy"] = proxy_url
        self._refresh_status()
