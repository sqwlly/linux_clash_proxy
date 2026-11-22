from __future__ import annotations

import json
import os
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import AppPaths
from .redaction import redact_value


def audit_log_file(paths: AppPaths) -> Path:
    return paths.state_dir / "cproxy-audit.jsonl"


def write_audit_event(
    paths: AppPaths,
    action: str,
    target: str,
    result: str,
    detail: dict[str, Any] | None = None,
) -> None:
    paths.state_dir.mkdir(parents=True, exist_ok=True)
    event = redact_value(
        {
            "ts": datetime.now(timezone.utc).isoformat(),
            "action": action,
            "target": target,
            "result": result,
            "detail": detail or {},
        }
    )
    line = json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n"
    log_path = audit_log_file(paths)
    if os.name != "nt":
        fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "a", encoding="utf-8") as fh:
                fd = -1
                fh.write(line)
        finally:
            if fd >= 0:
                os.close(fd)
    else:
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(line)

    if _journald_enabled(paths):
        _write_journald_event(event)


def _journald_enabled(paths: AppPaths) -> bool:
    try:
        from .config import read_config

        return bool(read_config(paths).get("audit-journald"))
    except Exception:
        return False


def _write_journald_event(event: dict[str, Any]) -> None:
    systemd_cat = shutil.which("systemd-cat")
    if not systemd_cat:
        return
    subprocess.run(
        [systemd_cat, "--identifier=cproxy-audit", "--priority=info"],
        input=json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n",
        text=True,
        check=False,
        timeout=2,
    )
