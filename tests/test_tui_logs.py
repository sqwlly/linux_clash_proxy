import asyncio
import threading
import time

from textual.app import App, ComposeResult

from cproxy.config import AppPaths
from cproxy.tui.screens.logs import LogsScreen
from cproxy.tui.widgets import NavigationTextArea as TextArea


class _LogsApp(App):
    def __init__(self, paths: AppPaths):
        super().__init__()
        self.paths = paths

    def compose(self) -> ComposeResult:
        yield LogsScreen(self.paths)


def test_logs_screen_unmount_does_not_wait_for_slow_tail_thread(tmp_path):
    paths = AppPaths(tmp_path / "config", tmp_path / "data", tmp_path / "state")
    screen = LogsScreen(paths)

    worker_started = threading.Event()
    release_worker = threading.Event()

    def slow_worker():
        worker_started.set()
        release_worker.wait(timeout=2)

    thread = threading.Thread(target=slow_worker, daemon=True)
    thread.start()
    worker_started.wait(timeout=1)
    screen._tail_thread = thread

    try:
        start = time.perf_counter()
        screen.on_unmount()
        elapsed = time.perf_counter() - start
    finally:
        release_worker.set()
        thread.join(timeout=1)

    assert elapsed < 0.5


def test_logs_screen_stop_event_wakes_tail_style_wait_immediately(tmp_path):
    paths = AppPaths(tmp_path / "config", tmp_path / "data", tmp_path / "state")
    screen = LogsScreen(paths)
    worker_exited = threading.Event()

    def wait_worker():
        while not screen._stop_event.wait(1):
            pass
        worker_exited.set()

    thread = threading.Thread(target=wait_worker)
    thread.start()
    screen._tail_thread = thread

    screen.on_unmount()

    assert worker_exited.wait(timeout=0.5)


def test_logs_screen_placeholder_is_replaced_when_log_file_appears(tmp_path):
    paths = AppPaths(tmp_path / "config", tmp_path / "data", tmp_path / "state")

    async def run_case():
        app = _LogsApp(paths)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(0.1)
            screen = app.query_one(LogsScreen)
            viewer = app.query_one("#log-viewer", TextArea)
            assert "日志文件不存在" in viewer.text
            assert screen._log_lines == []

            # 日志文件出现后，新增内容应整体替换占位文本，而不是无分隔地粘在它前面
            screen._append_log("line1\nline2\n")
            assert viewer.text == "line1\nline2"
            assert screen._log_lines == ["line1", "line2"]

            # 占位文本已被替换，后续追加走增量路径
            screen._append_log("line3\n")
            assert viewer.text == "line1\nline2\nline3"
            assert screen._log_lines == ["line1", "line2", "line3"]

    asyncio.run(run_case())
