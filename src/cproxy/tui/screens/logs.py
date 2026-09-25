from __future__ import annotations

import threading

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.document._edit import Edit
from textual.widget import Widget
from textual.widgets import Button, Checkbox, Label

from ...config import AppPaths, log_file
from ..widgets import NavigationTextArea as TextArea


class LogsScreen(Widget):
    INITIAL_LINE_LIMIT = 500
    FOLLOW_LINE_LIMIT = 1000

    BINDINGS = [
        Binding("c", "clear_logs", "清空视图"),
        Binding("f", "toggle_follow", "跟随"),
        Binding("r", "refresh_logs", "刷新"),
    ]

    def __init__(self, paths: AppPaths, **kwargs):
        super().__init__(**kwargs)
        self.paths = paths
        self._following = True
        self._stop_event = threading.Event()
        self._tail_thread: threading.Thread | None = None
        self._last_pos = 0
        self._log_lines: list[str] = []

    def compose(self) -> ComposeResult:
        with Vertical():
            with Vertical(classes="panel output-panel"):
                with Horizontal(classes="panel-header"):
                    yield Label("─", id="log-file-label", classes="path-label")
                    yield Label("[#a3e635]● 跟随[/]", id="log-status-label", classes="status-strip")
                with Horizontal(classes="toolbar"):
                    yield Button("清空", id="btn-log-clear", classes="action-button danger-button")
                    yield Button("刷新", id="btn-log-refresh", classes="action-button muted-button")
                    yield Checkbox("跟随", value=True, id="chk-follow")
                yield TextArea(id="log-viewer", read_only=True, classes="log-viewer")

    def on_mount(self) -> None:
        log_path = log_file(self.paths)
        self.query_one("#log-file-label", Label).update(f"[#8b98aa]{log_path}[/]")

        viewer = self.query_one("#log-viewer", TextArea)
        try:
            viewer.theme = "monokai"
        except Exception:
            pass

        if not list(self.app.query("#main-tabs")):
            self.refresh_data()

    def on_unmount(self) -> None:
        self._stop_event.set()
        if self._tail_thread and self._tail_thread.is_alive():
            self._tail_thread.join(timeout=0.1)

    def _load_logs(self) -> None:
        log_path = log_file(self.paths)
        viewer = self.query_one("#log-viewer", TextArea)

        if not log_path.exists():
            self._log_lines = []
            viewer.load_text(f"日志文件不存在: {log_path}")
            return

        try:
            content = log_path.read_text(encoding="utf-8", errors="replace")
            lines = content.splitlines()
            if len(lines) > self.INITIAL_LINE_LIMIT:
                lines = lines[-self.INITIAL_LINE_LIMIT:]

            self._log_lines = lines
            viewer.load_text("\n".join(lines))
            self._last_pos = log_path.stat().st_size

            if self._following:
                viewer.move_cursor((len(lines), 0))

        except Exception as e:
            self._log_lines = []
            viewer.load_text(f"读取日志失败: {e}")

    def _start_tail(self) -> None:
        if self._tail_thread and self._tail_thread.is_alive():
            return

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

    def refresh_data(self) -> None:
        self._load_logs()
        self._start_tail()

    def _append_log(self, content: str) -> None:
        viewer = self.query_one("#log-viewer", TextArea)
        new_lines = content.rstrip().splitlines()
        if not new_lines:
            return
        if len(new_lines) > self.FOLLOW_LINE_LIMIT:
            new_lines = new_lines[-self.FOLLOW_LINE_LIMIT:]

        # 文档内容正常时等于 "\n".join(self._log_lines)（无结尾换行）；占位文本
        # （文件不存在/读取失败）与清空后的空状态靠整体重建回到该不变式
        overflow = len(self._log_lines) + len(new_lines) - self.FOLLOW_LINE_LIMIT
        if not self._log_lines or overflow >= len(self._log_lines):
            self._log_lines = new_lines
            viewer.load_text("\n".join(self._log_lines))
        else:
            if overflow > 0:
                viewer.edit(Edit("", (0, 0), (overflow, 0), maintain_selection_offset=True))
                del self._log_lines[:overflow]
            insert_at = (len(self._log_lines) - 1, len(self._log_lines[-1]))
            text = "\n" + "\n".join(new_lines)
            viewer.edit(Edit(text, insert_at, insert_at, maintain_selection_offset=False))
            self._log_lines.extend(new_lines)

        if self._following:
            viewer.move_cursor((len(self._log_lines), 0))

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-log-clear":
            self._clear_log_viewer()
        elif event.button.id == "btn-log-refresh":
            self._load_logs()

    def on_checkbox_changed(self, event: Checkbox.Changed) -> None:
        if event.checkbox.id == "chk-follow":
            self._following = event.value
            status = "[#a3e635]● 跟随[/]" if self._following else "[#8b98aa]○ 暂停[/]"
            self.query_one("#log-status-label", Label).update(status)

    def action_clear_logs(self) -> None:
        self._clear_log_viewer()

    def _clear_log_viewer(self) -> None:
        self._log_lines = []
        self.query_one("#log-viewer", TextArea).load_text("")

    def action_toggle_follow(self) -> None:
        self._following = not self._following
        self.query_one("#chk-follow", Checkbox).value = self._following
        status = "[#a3e635]● 跟随[/]" if self._following else "[#8b98aa]○ 暂停[/]"
        self.query_one("#log-status-label", Label).update(status)

    def action_refresh_logs(self) -> None:
        self.refresh_data()
