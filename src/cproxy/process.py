from __future__ import annotations

from .backend.models import StatusSnapshot
from .backend.process import ProcessBackend, ProcessOwnershipError
from .config import AppPaths


def is_running(paths: AppPaths) -> bool:
    return ProcessBackend(paths).is_running()


def start_process(paths: AppPaths) -> int:
    return ProcessBackend(paths).start()


def stop_process(paths: AppPaths) -> bool:
    return ProcessBackend(paths).stop()


def restart_process(paths: AppPaths) -> int:
    return ProcessBackend(paths).restart()


def get_status(paths: AppPaths) -> StatusSnapshot:
    return ProcessBackend(paths).status()
