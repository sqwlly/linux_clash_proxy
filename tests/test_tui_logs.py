import threading
import time

from cproxy.config import AppPaths
from cproxy.tui.screens.logs import LogsScreen


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
