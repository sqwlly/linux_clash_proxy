#!/usr/bin/env python3
from __future__ import annotations

import json
import os
from collections import deque


HISTORY_LOOKBACK = 20
HISTORY_PENALTY_CAP_MS = 300
HISTORY_FAILURE_PENALTY_MS = 75
HISTORY_MISSED_ROUND_PENALTY_MS = 25


def _int_value(value: object, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def load_history_penalties(
    history_file: str,
    group: str,
    profile: str,
    url: str,
    default_rounds: int,
) -> dict[str, int]:
    if not history_file or not os.path.isfile(history_file):
        return {}

    recent_lines: deque[str] = deque(maxlen=HISTORY_LOOKBACK * 4)
    try:
        with open(history_file, encoding="utf-8") as fh:
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
