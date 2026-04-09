from __future__ import annotations

import os
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from .config import AppPaths, config_file, log_file, pid_file, read_config, runtime_file


@dataclass(frozen=True)
class StatusSnapshot:
    source_config: str
    runtime_config: str
    controller: str
    port: str
    runtime_ready: bool
    running: bool
    pid: int | None


def _program_path(paths: AppPaths) -> str:
    config = read_config(paths)
    return str(config.get("program-path", "mihomo"))


def _read_pid(paths: AppPaths) -> int | None:
    path = pid_file(paths)
    if not path.exists():
        return None
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except ValueError:
        return None


def _is_pid_running(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def is_running(paths: AppPaths) -> bool:
    pid = _read_pid(paths)
    running = _is_pid_running(pid)
    if not running and pid_file(paths).exists():
        pid_file(paths).unlink(missing_ok=True)
    return running


def start_process(paths: AppPaths) -> int:
    paths.state_dir.mkdir(parents=True, exist_ok=True)
    paths.data_dir.mkdir(parents=True, exist_ok=True)

    if is_running(paths):
        pid = _read_pid(paths)
        if pid is not None:
            return pid

    runtime = runtime_file(paths)
    if not runtime.exists():
        raise FileNotFoundError(f"runtime config not found: {runtime}")

    with log_file(paths).open("a", encoding="utf-8") as log_handle:
        process = subprocess.Popen(
            [_program_path(paths), "-f", str(runtime), "-d", str(paths.data_dir)],
            stdout=log_handle,
            stderr=log_handle,
            start_new_session=True,
        )

    pid_file(paths).write_text(f"{process.pid}\n", encoding="utf-8")
    time.sleep(0.1)
    if not _is_pid_running(process.pid):
        raise RuntimeError("process exited immediately")
    return process.pid


def stop_process(paths: AppPaths) -> bool:
    pid = _read_pid(paths)
    if not _is_pid_running(pid):
        pid_file(paths).unlink(missing_ok=True)
        return False

    assert pid is not None
    os.kill(pid, signal.SIGTERM)
    for _ in range(20):
        if not _is_pid_running(pid):
            pid_file(paths).unlink(missing_ok=True)
            return True
        time.sleep(0.1)
    os.kill(pid, signal.SIGKILL)
    pid_file(paths).unlink(missing_ok=True)
    return True


def restart_process(paths: AppPaths) -> int:
    stop_process(paths)
    return start_process(paths)


def get_status(paths: AppPaths) -> StatusSnapshot:
    config = read_config(paths)
    pid = _read_pid(paths)
    running = _is_pid_running(pid)
    if not running and pid_file(paths).exists():
        pid_file(paths).unlink(missing_ok=True)
        pid = None
    return StatusSnapshot(
        source_config=str(config_file(paths)),
        runtime_config=str(runtime_file(paths)),
        controller=str(config.get("external-controller", "127.0.0.1:9090")),
        port=str(config.get("mixed-port", 7890)),
        runtime_ready=runtime_file(paths).exists(),
        running=running,
        pid=pid,
    )
