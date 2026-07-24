from __future__ import annotations

import json
import time
from collections import deque
from pathlib import Path
from typing import Any

from ..config import AppPaths

HISTORY_LOOKBACK = 20
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

    recent_lines: deque[str] = deque(maxlen=HISTORY_LOOKBACK * 4)
    try:
        with history_path.open(encoding="utf-8") as fh:
            for line in fh:
                recent_lines.append(line)
    except OSError:
        return {}

    penalties: dict[str, int] = {}
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
            missed_rounds = max(0, rounds - success)
            penalty = failures * HISTORY_FAILURE_PENALTY_MS
            penalty += missed_rounds * HISTORY_MISSED_ROUND_PENALTY_MS
            name = str(node["name"])
            penalties[name] = min(HISTORY_PENALTY_CAP_MS, penalties.get(name, 0) + penalty)
    return penalties


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
    with history_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
