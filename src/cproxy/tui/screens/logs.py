from __future__ import annotations

import os
import threading

from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widget import Widget
from textual.widgets import Button, Checkbox, Label

from ...config import AppPaths, log_file
from ..widgets import NavigationTextArea as TextArea
from ..widgets import escape_markup, safe_call_from_thread


class LogsScreen(Widget):
    INITIAL_LINE_LIMIT = 500
    FOLLOW_LINE_LIMIT = 1000
    INITIAL_READ_MAX_BYTES = 256 * 1024
    _PARTIAL_FLUSH_LIMIT = 64 * 1024

    BINDINGS = [
        Binding("c", "clear_logs", "清空视图"),
        Binding("f", "toggle_follow", "跟随"),
        Binding("r", "refresh_logs", "刷新"),
    ]

    def __init__(self, paths: AppPaths, **kwargs):
        super().__init__(**kwargs)
        self.paths = paths
        self._following = True
        self._paused_by_hide = False
        self._stop_event = threading.Event()
        self._tail_thread: threading.Thread | None = None
        self._pos_lock = threading.Lock()
        self._last_pos = 0
        self._last_ino: int | None = None
        self._partial_bytes = b""
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
        self.query_one("#log-file-label", Label).update(f"[#8b98aa]{escape_markup(log_path)}[/]")

        viewer = self.query_one("#log-viewer", TextArea)
        try:
            viewer.theme = "monokai"
        except Exception:
            pass

        if not list(self.app.query("#main-tabs")):
            self.refresh_data()

    def on_hide(self) -> None:
        self._paused_by_hide = True

    def on_show(self) -> None:
        if not self._paused_by_hide:
            return
        self._paused_by_hide = False
        if self._following:
            self.refresh_data()

    def on_unmount(self) -> None:
        self._stop_event.set()
        if self._tail_thread and self._tail_thread.is_alive():
            self._tail_thread.join(timeout=0.1)

    def _load_logs(self) -> None:
        self._load_logs_worker()

    @work(thread=True, exclusive=True, group="logs-load")
    def _load_logs_worker(self) -> None:
        log_path = log_file(self.paths)
        if not log_path.exists():
            with self._pos_lock:
                self._last_pos = 0
                self._last_ino = None
                self._partial_bytes = b""
            safe_call_from_thread(self, self._apply_loaded_logs, [], f"日志文件不存在: {log_path}")
            return

        try:
            with log_path.open("rb") as fh:
                st = os.fstat(fh.fileno())
                truncated_head = st.st_size > self.INITIAL_READ_MAX_BYTES
                if truncated_head:
                    fh.seek(st.st_size - self.INITIAL_READ_MAX_BYTES)
                raw = fh.read()
                end_pos = fh.tell()
                inode = st.st_ino

            content = raw.decode("utf-8", errors="replace")
            if truncated_head and "\n" in content:
                content = content.split("\n", 1)[1]
            lines = content.splitlines()
            if len(lines) > self.INITIAL_LINE_LIMIT:
                lines = lines[-self.INITIAL_LINE_LIMIT :]

            with self._pos_lock:
                self._last_pos = end_pos
                self._last_ino = inode
                self._partial_bytes = b""

            safe_call_from_thread(self, self._apply_loaded_logs, lines, None)
        except Exception as exc:
            safe_call_from_thread(self, self._apply_loaded_logs, [], f"读取日志失败: {exc}")

    def _apply_loaded_logs(self, lines: list[str], placeholder: str | None) -> None:
        if not self.is_mounted:
            return
        viewer = self.query_one("#log-viewer", TextArea)
        if placeholder is not None:
            self._log_lines = []
            viewer.load_text(placeholder)
            return

        self._log_lines = lines
        viewer.load_text("\n".join(lines))
        if self._following:
            viewer.move_cursor((len(lines), 0))

    def _start_tail(self) -> None:
        if self._tail_thread and self._tail_thread.is_alive():
            return

        def tail_loop() -> None:
            log_path = log_file(self.paths)
            while not self._stop_event.wait(1):
                if not self._following or self._paused_by_hide or not self.is_mounted:
                    continue

                try:
                    with log_path.open("rb") as fh:
                        st = os.fstat(fh.fileno())
                        with self._pos_lock:
                            last_pos = self._last_pos
                            last_ino = self._last_ino

                        if (last_ino is not None and st.st_ino != last_ino) or st.st_size < last_pos:
                            with self._pos_lock:
                                self._last_pos = 0
                                self._last_ino = st.st_ino
                                self._partial_bytes = b""
                            safe_call_from_thread(self, self._load_logs)
                            continue

                        if st.st_size <= last_pos:
                            continue

                        fh.seek(last_pos)
                        chunk = fh.read()
                        new_pos = fh.tell()

                        emit_bytes = b""
                        with self._pos_lock:
                            buf = self._partial_bytes + chunk
                            if b"\n" in buf:
                                complete, remainder = buf.rsplit(b"\n", 1)
                                self._partial_bytes = remainder
                                emit_bytes = complete + b"\n"
                            elif len(buf) > self._PARTIAL_FLUSH_LIMIT:
                                self._partial_bytes = b""
                                emit_bytes = buf
                            else:
                                self._partial_bytes = buf
                            self._last_pos = new_pos
                            self._last_ino = st.st_ino

                    if emit_bytes:
                        new_content = emit_bytes.decode("utf-8", errors="replace")
                        if new_content.strip():
                            safe_call_from_thread(self, self._append_log, new_content)
                except FileNotFoundError:
                    continue
                except Exception:
                    pass

        self._tail_thread = threading.Thread(target=tail_loop, daemon=True)
        self._tail_thread.start()

    def refresh_data(self) -> None:
        self._load_logs()
        self._start_tail()

    def _append_log(self, content: str) -> None:
        if not self.is_mounted:
            return
        viewer = self.query_one("#log-viewer", TextArea)
        new_lines = content.rstrip().splitlines()
        if not new_lines:
            return
        if len(new_lines) > self.FOLLOW_LINE_LIMIT:
            new_lines = new_lines[-self.FOLLOW_LINE_LIMIT :]

        # 文档内容正常时等于 "\n".join(self._log_lines)（无结尾换行）；占位文本
        # （文件不存在/读取失败）与清空后的空状态靠整体重建回到该不变式
        overflow = len(self._log_lines) + len(new_lines) - self.FOLLOW_LINE_LIMIT
        if not self._log_lines or overflow >= len(self._log_lines):
            self._log_lines = new_lines
            viewer.load_text("\n".join(self._log_lines))
        else:
            if overflow > 0:
                viewer.replace("", (0, 0), (overflow, 0), maintain_selection_offset=True)
                del self._log_lines[:overflow]
            insert_at = (len(self._log_lines) - 1, len(self._log_lines[-1]))
            text = "\n" + "\n".join(new_lines)
            viewer.replace(text, insert_at, insert_at, maintain_selection_offset=False)
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
