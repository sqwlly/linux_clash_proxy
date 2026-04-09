from __future__ import annotations

from typing import Any

from .backend.runtime import RuntimeBackend
from .config import AppPaths


def runtime_groups(paths: AppPaths) -> dict[str, dict[str, Any]]:
    groups = RuntimeBackend(paths).get_groups()
    return {
        name: {
            "type": group.type,
            "all": group.candidates,
            "now": group.current,
        }
        for name, group in groups.items()
    }
