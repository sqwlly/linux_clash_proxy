from __future__ import annotations

from pathlib import Path

from .backend.runtime import (
    RuntimeBackend,
)
from .config import AppPaths


def render_runtime(paths: AppPaths) -> Path:
    return RuntimeBackend(paths).render_runtime()
