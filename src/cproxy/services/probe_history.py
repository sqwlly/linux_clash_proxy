from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from collections import deque
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from ..config import AppPaths

HISTORY_LOOKBACK = 20
MAX_HISTORY_LINES = 200
HISTORY_PENALTY_CAP_MS = 300
HISTORY_FAILURE_PENALTY_MS = 75
HISTORY_MISSED_ROUND_PENALTY_MS = 25


def probe_history_file(paths: AppPaths) -> Path:
    return paths.state_dir / "probe_history.jsonl"


def _int_value(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def load_history_penalties(
    history_path: Path,
    group: str,
    profile: str,
    url: str,
    default_rounds: int,
) -> dict[str, int]:
    if not history_path.is_file():
        return {}

    recent_lines: deque[str] = deque(maxlen=MAX_HISTORY_LINES)
    try:
        with history_path.open(encoding="utf-8") as fh:
            for line in fh:
                recent_lines.append(line)
    except OSError:
        return {}

    raw_penalties: dict[str, int] = {}
    matched = 0
    for line in reversed(recent_lines):
        if matched >= HISTORY_LOOKBACK:
            break
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        if payload.get("group") != group or payload.get("profile") != profile or payload.get("url") != url:
            continue

        nodes = payload.get("nodes")
        if not isinstance(nodes, list):
            continue
        matched += 1
        rounds = max(1, _int_value(payload.get("rounds"), default_rounds))
        for node in nodes:
            if not isinstance(node, dict) or not node.get("name"):
                continue
            success = _int_value(node.get("success"))
            failures = _int_value(node.get("failures"))
            if failures <= 0:
                continue
            missed_rounds = max(0, rounds - success)
            run_penalty = min(
                HISTORY_PENALTY_CAP_MS,
                failures * HISTORY_FAILURE_PENALTY_MS + missed_rounds * HISTORY_MISSED_ROUND_PENALTY_MS,
            )
            name = str(node["name"])
            raw_penalties[name] = raw_penalties.get(name, 0) + run_penalty
    if not matched:
        return {}
    return {
        name: min(HISTORY_PENALTY_CAP_MS, round(total / matched))
        for name, total in raw_penalties.items()
        if round(total / matched) > 0
    }


def load_history_rows(history_path: Path, limit: int) -> list[dict]:
    if limit < 1:
        raise ValueError("--limit 必须大于等于 1")

    rows: list[dict] = []
    try:
        with history_path.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(item, dict):
                    rows.append(item)
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise RuntimeError(f"读取历史失败: {exc}") from exc
    return rows[-limit:]


@contextmanager
def _history_lock(lock_path: Path, *, timeout: float = 5.0) -> Iterator[None]:
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    if sys.platform != "win32":
        import fcntl

        with lock_path.open("a+b") as lock_fh:
            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                try:
                    fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)
                except OSError:
                    pass
    else:
        deadline = time.monotonic() + timeout
        acquired_fd: int | None = None
        while True:
            try:
                acquired_fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_RDWR)
                break
            except FileExistsError:
                try:
                    if (time.time() - lock_path.stat().st_mtime) > 30.0:
                        lock_path.unlink(missing_ok=True)
                        continue
                except OSError:
                    pass
                if time.monotonic() >= deadline:
                    break
                time.sleep(0.02)
            except OSError:
                break
        try:
            yield
        finally:
            if acquired_fd is not None:
                try:
                    os.close(acquired_fd)
                except OSError:
                    pass
                try:
                    lock_path.unlink(missing_ok=True)
                except OSError:
                    pass


def _atomic_write_history(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as fh:
            if sys.platform != "win32":
                os.fchmod(fh.fileno(), 0o600)
            fh.write(text.encode("utf-8"))
            fh.flush()
        os.replace(tmp_path, path)
    except BaseException:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def record_probe_history(
    history_path: Path,
    *,
    profile: str,
    strategy_name: str,
    group: str,
    url: str,
    rounds: int,
    timeout_ms: int,
    current: str | None,
    current_stable: bool,
    current_reason: str,
    best: str | None,
    stable: bool,
    reason: str,
    switch_requested: bool,
    switched: bool,
    skip_reason: str,
    nodes: list[dict],
) -> None:
    history_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "ts": int(time.time()),
        "profile": profile,
        "strategy": strategy_name,
        "group": group,
        "url": url,
        "rounds": rounds,
        "timeout_ms": timeout_ms,
        "current": current,
        "current_stable": current_stable,
        "current_reason": current_reason,
        "best": best,
        "stable": stable,
        "reason": reason,
        "switch_requested": switch_requested,
        "switched": switched,
        "skip_reason": skip_reason,
        "nodes": nodes,
    }
    entry_line = json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n"
    lock_path = history_path.with_name(f"{history_path.name}.lock")
    with _history_lock(lock_path):
        existing_lines: list[str] = []
        if history_path.is_file():
            try:
                with history_path.open(encoding="utf-8") as fh:
                    existing_lines = [line if line.endswith("\n") else line + "\n" for line in fh if line.strip()]
            except OSError:
                existing_lines = []

        trimmed = (existing_lines + [entry_line])[-MAX_HISTORY_LINES:]
        _atomic_write_history(history_path, "".join(trimmed))
