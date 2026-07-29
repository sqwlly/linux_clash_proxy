from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import subprocess
import time
from pathlib import Path

from ..config import AppPaths, config_file, log_file, pid_file, process_meta_file, read_config, runtime_file
from .models import ProcessOwner, StatusSnapshot

SYSTEMD_SERVICE_NAME = "clash-proxy.service"
LOCK_STALE_SECONDS = 30


class ProcessOwnershipError(RuntimeError):
    pass


class ForeignInstanceError(RuntimeError):
    pass


class ProcessBackend:
    def __init__(self, paths: AppPaths):
        self.paths = paths

    def _program_path(self) -> str:
        config = read_config(self.paths)
        return str(config.get("program-path", "mihomo"))

    def _lock_path(self) -> Path:
        return self.paths.state_dir / ".lock"

    def _acquire_lock(self) -> bool:
        lock = self._lock_path()
        if lock.exists():
            try:
                age = time.time() - lock.stat().st_mtime
            except OSError:
                age = LOCK_STALE_SECONDS + 1
            if age < LOCK_STALE_SECONDS:
                return False
            lock.unlink(missing_ok=True)
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.touch()
        return True

    def _release_lock(self) -> None:
        self._lock_path().unlink(missing_ok=True)

    @staticmethod
    def _systemd_managed() -> bool:
        if os.environ.get("CPROXY_NO_SYSTEMD"):
            return False
        systemctl = shutil.which("systemctl")
        if not systemctl:
            return False
        try:
            result = subprocess.run(
                [systemctl, "is-enabled", SYSTEMD_SERVICE_NAME],
                capture_output=True, timeout=5,
            )
            return result.returncode == 0
        except (subprocess.TimeoutExpired, OSError):
            return False

    @staticmethod
    def _production_service_active() -> bool:
        if os.environ.get("CPROXY_NO_SYSTEMD"):
            return False
        systemctl = shutil.which("systemctl")
        if not systemctl:
            return False
        try:
            result = subprocess.run(
                [systemctl, "is-active", SYSTEMD_SERVICE_NAME],
                capture_output=True, timeout=5,
            )
            return result.returncode == 0
        except (subprocess.TimeoutExpired, OSError):
            return False

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
        if running:
            config = read_config(self.paths)
            port_value = config.get("mixed-port", 7890)
            address = self._parse_listen_address(port_value)
            if address and not self._port_accepts_connection(address):
                return False
        return running

    @staticmethod
    def _parse_listen_address(value: object) -> tuple[str, int] | None:
        text = str(value or "").strip()
        if not text:
            return None
        if text.isdigit():
            port = int(text)
            if not 0 < port < 65536:
                return None
            return "127.0.0.1", port
        host, sep, port_text = text.rpartition(":")
        if not sep:
            return None
        host = host.strip().strip("[]") or "127.0.0.1"
        if host in ("0.0.0.0", "::"):
            host = "127.0.0.1"
        try:
            port = int(port_text)
        except ValueError:
            return None
        if not 0 < port < 65536:
            return None
        return host, port

    @staticmethod
    def _port_accepts_connection(address: tuple[str, int]) -> bool:
        try:
            with socket.create_connection(address, timeout=0.3):
                return True
        except OSError:
            return False

    def _ensure_no_foreign_instance(self) -> None:
        config = read_config(self.paths)
        listen_values: list[tuple[str, object]] = []
        for key in ("external-controller", "external-controller-tls", "mixed-port", "port"):
            if config.get(key):
                listen_values.append((key, config[key]))
        for label, value in listen_values:
            address = self._parse_listen_address(value)
            if address and self._port_accepts_connection(address):
                raise ForeignInstanceError(
                    f"错误: {label} 地址 {value} 已有服务在监听，可能是非 cproxy 管理的 mihomo 实例\n"
                    "提示: 若是旧版系统服务在运行，先执行 systemctl stop clash-proxy 再启动"
                )

    def start(self) -> int:
        self.paths.state_dir.mkdir(parents=True, exist_ok=True)
        self.paths.data_dir.mkdir(parents=True, exist_ok=True)

        if self.is_running():
            pid = self._read_pid()
            if pid is not None:
                return pid

        if self._production_service_active():
            raise ForeignInstanceError(
                f"错误: 系统服务 {SYSTEMD_SERVICE_NAME} 正在运行，再启动 cproxy 会抢占相同端口\n"
                f"提示: 使用 systemctl restart {SYSTEMD_SERVICE_NAME} 管理生产代理，"
                "或先 systemctl stop clash-proxy 再启动 cproxy"
            )

        runtime = runtime_file(self.paths)
        if not runtime.exists():
            raise FileNotFoundError(f"runtime config not found: {runtime}")

        if not self._acquire_lock():
            raise RuntimeError("错误: 另一个启动进程正在进行中，请稍候...")

        try:
            self._ensure_no_foreign_instance()

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
        finally:
            self._release_lock()

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
        if self._systemd_managed():
            subprocess.run(["systemctl", "restart", SYSTEMD_SERVICE_NAME], check=True, timeout=30)
            time.sleep(1)
            pid = self._read_pid()
            return pid if pid else 0
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
            controller=str(config.get("external-controller-tls") or config.get("external-controller", "127.0.0.1:9090")),
            port=str(config.get("mixed-port", 7890)),
            runtime_ready=runtime_file(self.paths).exists(),
            running=running,
            pid=pid,
        )
