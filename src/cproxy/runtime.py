from __future__ import annotations

from pathlib import Path

from .backend.runtime import (
    AI_AUTO_GROUP,
    AI_MANUAL_GROUP,
    AI_REGION_SG,
    AI_REGION_US,
    AI_SG_GROUP,
    AI_US_GROUP,
    TEST_URL,
    RuntimeBackend,
)
from .config import AppPaths


def render_runtime(paths: AppPaths) -> Path:
    return RuntimeBackend(paths).render_runtime()
