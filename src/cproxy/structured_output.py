from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1


def _json_default(value: object) -> object:
    if isinstance(value, Path):
        return str(value)
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def emit_json(
    command: str,
    data: Any,
    *,
    ok: bool = True,
    warnings: list[str] | None = None,
    recommended_actions: list[str] | None = None,
) -> None:
    """输出稳定的顶层信封，便于脚本在字段扩展时继续兼容。"""
    payload = {
        "schema_version": SCHEMA_VERSION,
        "command": command,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "ok": ok,
        "warnings": warnings or [],
        "recommended_actions": recommended_actions or [],
        "data": data,
    }
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True, default=_json_default))
