from __future__ import annotations

import threading

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widget import Widget
from textual.widgets import Button, Checkbox, Label

from ...config import AppPaths, log_file
from ..widgets import NavigationTextArea as TextArea


class LogsScreen(Widget):
    BINDINGS = [
        Binding("c", "clear_logs", "Clear"),
        Binding("f", "toggle_follow", "Follow"),
        Binding("r", "refresh_logs", "Refresh"),
    ]

    def __init__(self, paths: AppPaths, **kwargs):
        super().__init__(**kwargs)
        self.paths = paths
        self._following = True
        self._stop_event = threading.Event()
        self._tail_thread: threading.Thread | None = None
        self._last_pos = 0

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label("Logs", classes="page-title")
            with Vertical(classes="panel output-panel"):
                yield Label("Log Viewer", classes="panel-title")
                with Horizontal():
                    yield Label("─", id="log-file-label", classes="path-label")
                    yield Label("[#a3e635]● Following[/]", id="log-status-label", classes="status-strip")
                with Horizontal(classes="toolbar"):
                    yield Button("Clear", id="btn-log-clear", classes="action-button danger-button")
                    yield Button("Refresh", id="btn-log-refresh", classes="action-button primary-button")
                    yield Checkbox("Auto-follow", value=True, id="chk-follow")
                yield TextArea(id="log-viewer", read_only=True, classes="log-viewer")

    def on_mount(self) -> None:
        log_path = log_file(self.paths)
        self.query_one("#log-file-label", Label).update(f"[#8b98aa]{log_path}[/]")

        viewer = self.query_one("#log-viewer", TextArea)
        try:
            viewer.theme = "monokai"
        except Exception:
            pass

        self._load_logs()
        self._start_tail()

    def on_unmount(self) -> None:
        self._stop_event.set()
        if self._tail_thread and self._tail_thread.is_alive():
            self._tail_thread.join(timeout=0.1)

    def _load_logs(self) -> None:
        log_path = log_file(self.paths)
        viewer = self.query_one("#log-viewer", TextArea)

        if not log_path.exists():
            viewer.load_text(f"Log file not found: {log_path}")
            return

        try:
            content = log_path.read_text(encoding="utf-8", errors="replace")
            lines = content.splitlines()
            max_lines = 500
            if len(lines) > max_lines:
                lines = lines[-max_lines:]

            viewer.load_text("\n".join(lines))
            self._last_pos = log_path.stat().st_size

            if self._following:
                viewer.move_cursor((len(lines), 0))

        except Exception as e:
            viewer.load_text(f"Error reading logs: {e}")

    def _start_tail(self) -> None:
        def tail_loop():
            log_path = log_file(self.paths)
            while not self._stop_event.wait(1):
                if not self._following:
                    continue
                if not log_path.exists():
                    continue

                try:
                    current_size = log_path.stat().st_size
                    if current_size > self._last_pos:
                        with log_path.open("r", encoding="utf-8", errors="replace") as f:
                            f.seek(self._last_pos)
                            new_content = f.read()
                            self._last_pos = current_size

                        if new_content.strip():
                            self.app.call_from_thread(self._append_log, new_content)
                    elif current_size < self._last_pos:
                        self._last_pos = 0
                        self.app.call_from_thread(self._load_logs)

                except Exception:
                    pass

        self._tail_thread = threading.Thread(target=tail_loop, daemon=True)
        self._tail_thread.start()

    def _append_log(self, content: str) -> None:
        viewer = self.query_one("#log-viewer", TextArea)
        current = viewer.text
        new_text = current + "\n" + content.rstrip() if current else content.rstrip()

        lines = new_text.splitlines()
        max_lines = 1000
        if len(lines) > max_lines:
            lines = lines[-max_lines:]
            new_text = "\n".join(lines)

        viewer.load_text(new_text)

        if self._following:
            viewer.move_cursor((len(lines), 0))

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-log-clear":
            self.query_one("#log-viewer", TextArea).load_text("")
        elif event.button.id == "btn-log-refresh":
            self._load_logs()

    def on_checkbox_changed(self, event: Checkbox.Changed) -> None:
        if event.checkbox.id == "chk-follow":
            self._following = event.value
            status = "[#a3e635]● Following[/]" if self._following else "[#8b98aa]○ Paused[/]"
            self.query_one("#log-status-label", Label).update(status)

    def action_clear_logs(self) -> None:
        self.query_one("#log-viewer", TextArea).load_text("")

    def action_toggle_follow(self) -> None:
        self._following = not self._following
        self.query_one("#chk-follow", Checkbox).value = self._following
        status = "[#a3e635]● Following[/]" if self._following else "[#8b98aa]○ Paused[/]"
        self.query_one("#log-status-label", Label).update(status)

    def action_refresh_logs(self) -> None:
        self._load_logs()
