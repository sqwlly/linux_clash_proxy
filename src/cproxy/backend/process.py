from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from pathlib import Path

from ..config import AppPaths, config_file, log_file, pid_file, process_meta_file, read_config, runtime_file
from .models import ProcessOwner, StatusSnapshot


class ProcessOwnershipError(RuntimeError):
    pass


class ProcessBackend:
    def __init__(self, paths: AppPaths):
        self.paths = paths

    def _program_path(self) -> str:
        config = read_config(self.paths)
        return str(config.get("program-path", "mihomo"))

    def _read_pid(self) -> int | None:
        path = pid_file(self.paths)
        if not path.exists():
            return None
        try:
            return int(path.read_text(encoding="utf-8").strip())
        except ValueError:
            return None

    def _is_pid_running(self, pid: int | None) -> bool:
        if not pid:
            return False
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return True

    def _read_process_owner(self) -> ProcessOwner | None:
        path = process_meta_file(self.paths)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return ProcessOwner(pid=int(data["pid"]), program=str(data["program"]), runtime=str(data["runtime"]))
        except (ValueError, KeyError, json.JSONDecodeError):
            return None

    def _write_process_owner(self, owner: ProcessOwner) -> None:
        process_meta_file(self.paths).write_text(
            json.dumps({"pid": owner.pid, "program": owner.program, "runtime": owner.runtime}, ensure_ascii=True) + "\n",
            encoding="utf-8",
        )

    def _cleanup_process_state(self) -> None:
        pid_file(self.paths).unlink(missing_ok=True)
        process_meta_file(self.paths).unlink(missing_ok=True)

    def _is_owned_process(self, pid: int | None) -> bool:
        if not pid or not self._is_pid_running(pid):
            return False

        owner = self._read_process_owner()
        if not owner or owner.pid != pid:
            return False

        cmdline_path = Path(f"/proc/{pid}/cmdline")
        try:
            cmdline = cmdline_path.read_text(encoding="utf-8").replace("\x00", " ")
        except OSError:
            return False

        return Path(owner.program).name in cmdline and owner.runtime in cmdline

    def is_running(self) -> bool:
        pid = self._read_pid()
        running = self._is_owned_process(pid)
        if not running and pid_file(self.paths).exists() and not self._is_pid_running(pid):
            self._cleanup_process_state()
        return running

    def start(self) -> int:
        self.paths.state_dir.mkdir(parents=True, exist_ok=True)
        self.paths.data_dir.mkdir(parents=True, exist_ok=True)

        if self.is_running():
            pid = self._read_pid()
            if pid is not None:
                return pid

        runtime = runtime_file(self.paths)
        if not runtime.exists():
            raise FileNotFoundError(f"runtime config not found: {runtime}")

        with log_file(self.paths).open("a", encoding="utf-8") as log_handle:
            program = self._program_path()
            process = subprocess.Popen(
                [program, "-f", str(runtime), "-d", str(self.paths.data_dir)],
                stdout=log_handle,
                stderr=log_handle,
                start_new_session=True,
            )

        pid_file(self.paths).write_text(f"{process.pid}\n", encoding="utf-8")
        self._write_process_owner(ProcessOwner(pid=process.pid, program=program, runtime=str(runtime)))
        time.sleep(0.1)
        if not self._is_pid_running(process.pid):
            self._cleanup_process_state()
            raise RuntimeError("process exited immediately")
        return process.pid

    def stop(self) -> bool:
        pid = self._read_pid()
        if not self._is_pid_running(pid):
            self._cleanup_process_state()
            return False

        if not self._is_owned_process(pid):
            raise ProcessOwnershipError("错误: 当前 PID 文件指向的进程不属于 cproxy 管理的进程")

        assert pid is not None
        os.kill(pid, signal.SIGTERM)
        for _ in range(20):
            if not self._is_pid_running(pid):
                self._cleanup_process_state()
                return True
            time.sleep(0.1)
        os.kill(pid, signal.SIGKILL)
        self._cleanup_process_state()
        return True

    def restart(self) -> int:
        self.stop()
        return self.start()

    def status(self) -> StatusSnapshot:
        config = read_config(self.paths)
        pid = self._read_pid()
        running = self._is_owned_process(pid)
        if not running and pid_file(self.paths).exists() and not self._is_pid_running(pid):
            self._cleanup_process_state()
            pid = None
        if not running:
            pid = None
        return StatusSnapshot(
            source_config=str(config_file(self.paths)),
            runtime_config=str(runtime_file(self.paths)),
            controller=str(config.get("external-controller", "127.0.0.1:9090")),
            port=str(config.get("mixed-port", 7890)),
            runtime_ready=runtime_file(self.paths).exists(),
            running=running,
            pid=pid,
        )
