from __future__ import annotations

import io
import json
import tarfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from .audit import audit_log_file
from .config import AppPaths, config_file, log_file, runtime_file
from .process import get_status
from .redaction import redact_text, redact_value


def build_support_bundle(paths: AppPaths, output_path: Path | None = None) -> Path:
    paths.state_dir.mkdir(parents=True, exist_ok=True)
    created_at = datetime.now(timezone.utc)
    bundle_path = output_path or paths.state_dir / f"cproxy-support-{created_at.strftime('%Y%m%dT%H%M%SZ')}.tar.gz"
    bundle_path.parent.mkdir(parents=True, exist_ok=True)

    with tarfile.open(bundle_path, "w:gz") as archive:
        _add_json(
            archive,
            "manifest.json",
            {
                "created_at": created_at.isoformat(),
                "redaction": "keys matching secret/token/password/credential/authorization and URL queries are redacted",
            },
        )
        _add_json(archive, "status.json", get_status(paths).__dict__)
        _add_yaml_file(archive, "config.redacted.yaml", config_file(paths))
        _add_yaml_file(archive, "runtime.redacted.yaml", runtime_file(paths))
        _add_text_file(archive, "audit.redacted.jsonl", audit_log_file(paths), max_lines=1000)
        _add_text_file(archive, "log.tail.redacted.txt", log_file(paths), max_lines=500)

    return bundle_path


def _add_json(archive: tarfile.TarFile, name: str, payload: dict[str, Any]) -> None:
    text = json.dumps(redact_value(payload), ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    _add_bytes(archive, name, text.encode("utf-8"))


def _add_yaml_file(archive: tarfile.TarFile, name: str, path: Path) -> None:
    if not path.exists():
        _add_bytes(archive, name, b"")
        return
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    text = yaml.safe_dump(redact_value(data), allow_unicode=True, sort_keys=False)
    _add_bytes(archive, name, text.encode("utf-8"))


def _add_text_file(archive: tarfile.TarFile, name: str, path: Path, max_lines: int) -> None:
    if not path.exists():
        _add_bytes(archive, name, b"")
        return
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    text = "\n".join(_redact_log_line(line, parse_json=name.endswith(".jsonl")) for line in lines[-max_lines:])
    text += "\n" if lines else ""
    _add_bytes(archive, name, text.encode("utf-8"))


def _redact_log_line(line: str, parse_json: bool) -> str:
    if parse_json:
        try:
            return json.dumps(redact_value(json.loads(line)), ensure_ascii=False, sort_keys=True)
        except json.JSONDecodeError:
            pass
    return redact_text(line)


def _add_bytes(archive: tarfile.TarFile, name: str, payload: bytes) -> None:
    info = tarfile.TarInfo(name)
    info.size = len(payload)
    archive.addfile(info, io.BytesIO(payload))
