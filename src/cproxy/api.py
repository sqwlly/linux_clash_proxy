from __future__ import annotations

from typing import Any

from .backend.api import APIBackend, APIUnavailableError
from .config import AppPaths


def controller_url(paths: AppPaths) -> str:
    return APIBackend(paths).controller_url()


def api_secret(paths: AppPaths) -> str:
    return APIBackend(paths).api_secret()


def api_request(paths: AppPaths, method: str, path: str, payload: dict | None = None) -> Any:
    return APIBackend(paths).request(method, path, payload)


def proxies_data(paths: AppPaths) -> dict[str, Any]:
    groups = APIBackend(paths).get_groups()
    return {
        name: {
            "type": group.type,
            "now": group.current,
            "alive": group.alive,
            "all": group.candidates,
            "history": [{"delay": group.delay}] if group.delay is not None else [],
        }
        for name, group in groups.items()
    }


def update_proxy(paths: AppPaths, group_name: str, target_name: str) -> None:
    APIBackend(paths).switch_group(group_name, target_name)


def delay_test(paths: AppPaths, target_name: str, url: str, timeout: int) -> dict[str, Any]:
    return APIBackend(paths).delay_test(target_name, url, timeout)
