from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widget import Widget
from textual.widgets import Button, Label

from ...config import AppPaths, config_file, read_config, runtime_file
from ..widgets import NavigationDataTable as DataTable, NavigationInput as Input, NavigationTextArea as TextArea


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


def redact_subscription_url(url: str) -> str:
    parsed = urlsplit(url)
    if not parsed.scheme or not parsed.netloc:
        return url[:80] + ("..." if len(url) > 80 else "")
    path = parsed.path
    if len(path) > 24:
        path = path[:12] + "..." + path[-8:]
    query = "..." if parsed.query else ""
    return urlunsplit((parsed.scheme, parsed.netloc, path, query, ""))


def subscription_group_rows(config: dict) -> list[tuple[str, str, str, str]]:
    groups = [group for group in config.get("proxy-groups", []) if isinstance(group, dict)]
    parents_by_child: dict[str, list[str]] = {}
    for group in groups:
        parent_name = str(group.get("name", ""))
        for child in group.get("proxies", []) or []:
            parents_by_child.setdefault(str(child), []).append(parent_name)

    rows = []
    for group in groups:
        name = str(group.get("name", ""))
        if not name:
            continue
        group_type = str(group.get("type", ""))
        proxies = [str(item) for item in group.get("proxies", []) or []]
        parents = [parent for parent in parents_by_child.get(name, []) if parent != name]
        rows.append((name, group_type, str(len(proxies)), ", ".join(parents) or "─"))
    return rows


def format_subscription_result(
    stdout: str,
    stderr: str,
    returncode: int,
    dry_run: bool,
    group: str,
    attach_to: str,
) -> str:
    mode = "Preview" if dry_run else "Apply"
    status = "OK" if returncode == 0 else f"Failed ({returncode})"
    summary_line = next(
        (
            line.strip()
            for line in stdout.splitlines()
            if line.strip().startswith(("订阅下载完成:", "订阅挂载完成:"))
        ),
        "",
    )
    lines = [f"{mode}: {status}"]
    if group:
        lines.append(f"Group: {group}")
    if attach_to:
        lines.append(f"Attach to: {attach_to}")
    if summary_line:
        lines.append(f"Summary: {summary_line}")
    if stdout.strip():
        lines.extend(["", "stdout:", stdout.strip()])
    if stderr.strip():
        lines.extend(["", "stderr:", stderr.strip()])
    return "\n".join(lines)


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
        self._subscription_running = False

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
                with Horizontal(classes="input-row"):
                    yield Input(
                        placeholder="Group name (optional)",
                        id="sub-group-input",
                        classes="subscription-input",
                    )
                    yield Input(
                        placeholder="Attach to selector (optional)",
                        id="sub-attach-input",
                        classes="subscription-input",
                    )
                    yield Button("Preview", id="btn-sub-preview", classes="action-button muted-button")
                    yield Button("Apply", id="btn-sub-apply", classes="action-button success-button")
                    yield Button("Validate", id="btn-sub-update", classes="action-button primary-button")

            with Horizontal(classes="workbench-row"):
                with Vertical(classes="panel output-panel split-main"):
                    yield Label("Output", classes="panel-title")
                    yield TextArea(id="sub-output", read_only=True, classes="output-area")

                with Vertical(classes="panel split-sidebar summary-panel"):
                    yield Label("Subscription Groups", classes="panel-title")
                    yield Label("─", id="sub-current-info", classes="current-info")
                    yield DataTable(id="sub-groups-table")

    def on_mount(self) -> None:
        output = self.query_one("#sub-output", TextArea)
        try:
            output.theme = "monokai"
        except Exception:
            pass
        groups_table = self.query_one("#sub-groups-table", DataTable)
        groups_table.add_columns("Group", "Type", "Nodes", "Attached")
        groups_table.cursor_type = "row"
        groups_table.show_header = True
        if not list(self.app.query("#main-tabs")):
            self._load_current_info()

    def refresh_data(self) -> None:
        self._load_current_info()

    def _load_current_info(self) -> None:
        try:
            config = read_config(self.paths)
            config_path = config_file(self.paths)

            proxies = config.get("proxies", [])
            groups = config.get("proxy-groups", [])
            group_rows = subscription_group_rows(config)

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
            groups_table = self.query_one("#sub-groups-table", DataTable)
            groups_table.clear()
            for row in group_rows:
                groups_table.add_row(*row, key=row[0])
            if not group_rows:
                groups_table.add_row("No groups", "─", "0", "─")
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
        if self._subscription_running:
            return
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

        output.load_text(
            "\n".join(
                [
                    "Previewing subscription..." if dry_run else "Applying subscription...",
                    f"URL: {redact_subscription_url(url)}",
                    f"Group: {group or '(from subscription)'}",
                    f"Attach to: {attach_to or '(not attached)'}",
                    "",
                    "Please wait...",
                ]
            )
        )

        refresh_script: Path | None = None
        env = None
        try:
            if not dry_run:
                refresh_script = write_user_refresh_script()
                env = build_import_update_env(self.paths, refresh_script, clash_proxy)
            self._set_subscription_busy(True)
            threading.Thread(
                target=self._import_subscription_worker,
                args=(cmd, env, dry_run, group, attach_to, refresh_script),
                daemon=True,
            ).start()
        except Exception as e:
            output.load_text(f"Error: {e}")
            self._set_subscription_busy(False)

    def _import_subscription_worker(
        self,
        cmd: list[str],
        env: dict[str, str] | None,
        dry_run: bool,
        group: str,
        attach_to: str,
        refresh_script: Path | None,
    ) -> None:
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30, env=env)
            output_text = format_subscription_result(
                result.stdout,
                result.stderr,
                result.returncode,
                dry_run=dry_run,
                group=group,
                attach_to=attach_to,
            )
            self._call_from_subscription_thread(
                self._finish_import_subscription,
                output_text,
                result.returncode == 0 and not dry_run,
            )
        except subprocess.TimeoutExpired:
            self._call_from_subscription_thread(self._fail_subscription_command, "Error: Command timeout (30s)")
        except Exception as e:
            self._call_from_subscription_thread(self._fail_subscription_command, f"Error: {e}")
        finally:
            if refresh_script is not None:
                refresh_script.unlink(missing_ok=True)

    def _finish_import_subscription(self, output_text: str, imported: bool) -> None:
        if not self.is_mounted:
            self._subscription_running = False
            return
        self.query_one("#sub-output", TextArea).load_text(output_text)
        self._set_subscription_busy(False)
        if imported:
            self._load_current_info()
            self.notify("Subscription imported", severity="information")

    def _fail_subscription_command(self, output_text: str) -> None:
        if not self.is_mounted:
            self._subscription_running = False
            return
        self.query_one("#sub-output", TextArea).load_text(output_text)
        self._set_subscription_busy(False)

    def _update_config(self) -> None:
        output = self.query_one("#sub-output", TextArea)
        config_path = config_file(self.paths)

        if self._subscription_running:
            return

        if not config_path.exists():
            output.load_text(f"Config not found: {config_path}")
            return

        update_script = shutil.which("clash-proxy-update")
        if not update_script:
            output.load_text("Error: clash-proxy-update command not found")
            return

        cmd = [update_script, "--dry-run", str(config_path)]
        output.load_text(f"Running: {' '.join(cmd)}\n\nPlease wait...")

        self._set_subscription_busy(True)
        threading.Thread(target=self._validate_config_worker, args=(cmd,), daemon=True).start()

    def _validate_config_worker(self, cmd: list[str]) -> None:
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            output_text = ""
            if result.stdout:
                output_text += result.stdout
            if result.stderr:
                output_text += "\n" + result.stderr
            self._call_from_subscription_thread(self._finish_validate_config, output_text or "Validation passed")
        except Exception as e:
            self._call_from_subscription_thread(self._fail_subscription_command, f"Error: {e}")

    def _finish_validate_config(self, output_text: str) -> None:
        if not self.is_mounted:
            self._subscription_running = False
            return
        self.query_one("#sub-output", TextArea).load_text(output_text)
        self._set_subscription_busy(False)

    def _call_from_subscription_thread(self, callback, *args) -> None:
        try:
            self.app.call_from_thread(callback, *args)
        except Exception:
            self._subscription_running = False

    def _set_subscription_busy(self, busy: bool) -> None:
        self._subscription_running = busy
        for selector in ("#btn-sub-preview", "#btn-sub-apply", "#btn-sub-update"):
            self.query_one(selector, Button).disabled = busy
