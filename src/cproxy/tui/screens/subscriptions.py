from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widget import Widget
from textual.widgets import Button, Label

from ...config import AppPaths, config_file, read_config, runtime_file
from ..widgets import NavigationInput as Input, NavigationTextArea as TextArea


def build_import_subscription_command(
    clash_proxy: str,
    url: str,
    dry_run: bool,
    group: str = "",
    attach_to: str = "",
    config_path: Path | None = None,
    update_script: str = "",
) -> list[str]:
    cmd = [clash_proxy, "import-subscription", url, "--dry-run" if dry_run else "--apply"]
    if config_path is not None:
        cmd.extend(["--config-file", str(config_path)])
    if update_script:
        cmd.extend(["--update-script", update_script])
    if group:
        cmd.extend(["--group", group])
    if attach_to:
        cmd.extend(["--attach-to", attach_to])
    return cmd


def build_import_update_env(paths: AppPaths, refresh_script: Path, proxy_sh: str) -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "CONFIG_FILE": str(config_file(paths)),
            "RUNTIME_CONFIG": str(runtime_file(paths)),
            "REFRESH_SCRIPT": str(refresh_script),
            "PROXY_SH": proxy_sh,
            "CPROXY_CONFIG_DIR": str(paths.config_dir),
            "CPROXY_DATA_DIR": str(paths.data_dir),
            "CPROXY_STATE_DIR": str(paths.state_dir),
        }
    )
    return env


def write_user_refresh_script() -> Path:
    handle = tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        prefix="cproxy-tui-refresh-",
        suffix=".sh",
        delete=False,
    )
    try:
        handle.write(
            """#!/bin/sh
set -eu
python3 - <<'PY'
import os
from pathlib import Path

from cproxy.config import AppPaths
from cproxy.runtime import render_runtime

paths = AppPaths(
    config_dir=Path(os.environ["CPROXY_CONFIG_DIR"]),
    data_dir=Path(os.environ["CPROXY_DATA_DIR"]),
    state_dir=Path(os.environ["CPROXY_STATE_DIR"]),
)
runtime_path = render_runtime(paths)
print(f"runtime rendered: {runtime_path}")
PY
"""
        )
        return Path(handle.name)
    finally:
        handle.close()
        os.chmod(handle.name, 0o700)


class SubscriptionsScreen(Widget):
    def __init__(self, paths: AppPaths, **kwargs):
        super().__init__(**kwargs)
        self.paths = paths

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label("Subscriptions", classes="page-title")

            with Vertical(classes="panel form-panel"):
                yield Label("Import", classes="panel-title")
                yield Input(
                    placeholder="Subscription URL (Clash/VLESS/Base64)...",
                    id="sub-url-input",
                    classes="subscription-input",
                )
                yield Input(
                    placeholder="Group name (optional, e.g. CyberGuard)",
                    id="sub-group-input",
                    classes="subscription-input",
                )
                yield Input(
                    placeholder="Attach to selector (optional, e.g. AI-MANUAL)",
                    id="sub-attach-input",
                    classes="subscription-input",
                )
                with Horizontal(classes="toolbar"):
                    yield Button("Preview", id="btn-sub-preview", classes="action-button muted-button")
                    yield Button("Apply", id="btn-sub-apply", classes="action-button success-button")
                    yield Button("Validate", id="btn-sub-update", classes="action-button primary-button")

            with Horizontal(classes="workbench-row"):
                with Vertical(classes="panel output-panel split-main"):
                    yield Label("Output", classes="panel-title")
                    yield TextArea(id="sub-output", read_only=True, classes="output-area")

                with Vertical(classes="panel split-sidebar summary-panel"):
                    yield Label("Current Config", classes="panel-title")
                    yield Label("─", id="sub-current-info", classes="current-info")

    def on_mount(self) -> None:
        self._load_current_info()
        output = self.query_one("#sub-output", TextArea)
        try:
            output.theme = "monokai"
        except Exception:
            pass

    def _load_current_info(self) -> None:
        try:
            config = read_config(self.paths)
            config_path = config_file(self.paths)

            proxies = config.get("proxies", [])
            groups = config.get("proxy-groups", [])

            info_text = "\n".join(
                [
                    f"[#8b98aa]Path[/]\n{config_path}",
                    f"[#8b98aa]Proxies[/] {len(proxies)}",
                    f"[#8b98aa]Groups[/] {len(groups)}",
                    f"[#8b98aa]Port[/] {config.get('mixed-port', '─')}",
                    f"[#8b98aa]Mode[/] {config.get('mode', '─')}",
                ]
            )
            self.query_one("#sub-current-info", Label).update(info_text)
        except Exception as e:
            self.query_one("#sub-current-info", Label).update(f"[#fb7185]Error: {e}[/]")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-sub-preview":
            self._import_subscription(dry_run=True)
        elif event.button.id == "btn-sub-apply":
            self._import_subscription(dry_run=False)
        elif event.button.id == "btn-sub-update":
            self._update_config()

    def _import_subscription(self, dry_run: bool) -> None:
        url_input = self.query_one("#sub-url-input", Input)
        group_input = self.query_one("#sub-group-input", Input)
        attach_input = self.query_one("#sub-attach-input", Input)
        output = self.query_one("#sub-output", TextArea)

        url = url_input.value.strip()
        if not url:
            output.load_text("Please enter a subscription URL")
            return

        clash_proxy = shutil.which("clash-proxy")
        if not clash_proxy:
            output.load_text("Error: clash-proxy command not found")
            return

        update_script = shutil.which("clash-proxy-update")
        if not update_script:
            output.load_text("Error: clash-proxy-update command not found")
            return

        group = group_input.value.strip()
        attach_to = attach_input.value.strip()
        config_path = config_file(self.paths)
        cmd = build_import_subscription_command(
            clash_proxy,
            url,
            dry_run,
            group,
            attach_to,
            config_path=config_path,
            update_script=update_script,
        )

        output.load_text(f"Running: {' '.join(cmd)}\n\nPlease wait...")

        refresh_script: Path | None = None
        env = None
        try:
            if not dry_run:
                refresh_script = write_user_refresh_script()
                env = build_import_update_env(self.paths, refresh_script, clash_proxy)
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30, env=env)
            output_text = ""
            if result.stdout:
                output_text += result.stdout
            if result.stderr:
                output_text += "\n" + result.stderr
            if result.returncode != 0:
                output_text += f"\nExit code: {result.returncode}"
            output.load_text(output_text or "No output")

            if result.returncode == 0 and not dry_run:
                self._load_current_info()
                self.notify("Subscription imported", severity="information")

        except subprocess.TimeoutExpired:
            output.load_text("Error: Command timeout (30s)")
        except Exception as e:
            output.load_text(f"Error: {e}")
        finally:
            if refresh_script is not None:
                refresh_script.unlink(missing_ok=True)

    def _update_config(self) -> None:
        output = self.query_one("#sub-output", TextArea)
        config_path = config_file(self.paths)

        if not config_path.exists():
            output.load_text(f"Config not found: {config_path}")
            return

        update_script = shutil.which("clash-proxy-update")
        if not update_script:
            output.load_text("Error: clash-proxy-update command not found")
            return

        cmd = [update_script, "--dry-run", str(config_path)]
        output.load_text(f"Running: {' '.join(cmd)}\n\nPlease wait...")

        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            output_text = ""
            if result.stdout:
                output_text += result.stdout
            if result.stderr:
                output_text += "\n" + result.stderr
            output.load_text(output_text or "Validation passed")
        except Exception as e:
            output.load_text(f"Error: {e}")
