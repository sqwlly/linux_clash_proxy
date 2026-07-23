from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
import yaml

ROOT_DIR = Path(__file__).resolve().parents[1]
SRC_DIR = ROOT_DIR / "src"
sys.path.insert(0, str(SRC_DIR))

from cproxy.config import default_paths
from cproxy.services.probe_history import (
    load_history_penalties,
    load_history_rows,
    probe_history_file,
    record_probe_history,
)


# ---------------------------------------------------------------------------
# probe_history: record + load
# ---------------------------------------------------------------------------

def test_record_and_load_rows(tmp_path: Path):
    paths = default_paths(tmp_path)
    history = probe_history_file(paths)
    assert not history.exists()

    record_probe_history(
        history,
        profile="codex", strategy_name="conservative", group="AI-MANUAL",
        url="https://chatgpt.com", rounds=3, timeout_ms=8000,
        current="node-a", current_stable=True, current_reason="ok",
        best="node-b", stable=True, reason="ok",
        switch_requested=True, switched=True, skip_reason="",
        nodes=[
            {"name": "node-a", "success": 3, "total": 3, "failures": 0,
             "avg_ms": 100, "max_ms": 120, "min_ms": 80, "history_penalty_ms": 0, "score": 85},
            {"name": "node-b", "success": 3, "total": 3, "failures": 0,
             "avg_ms": 80, "max_ms": 100, "min_ms": 60, "history_penalty_ms": 0, "score": 90},
        ],
    )
    assert history.is_file()

    rows = load_history_rows(history, 5)
    assert len(rows) == 1
    assert rows[0]["profile"] == "codex"
    assert rows[0]["best"] == "node-b"
    assert rows[0]["switched"] is True
    assert len(rows[0]["nodes"]) == 2


def test_load_rows_limit(tmp_path: Path):
    paths = default_paths(tmp_path)
    history = probe_history_file(paths)
    for i in range(10):
        record_probe_history(
            history, profile="codex", strategy_name="conservative", group="G",
            url="https://x.com", rounds=1, timeout_ms=1000,
            current=None, current_stable=False, current_reason="",
            best=None, stable=False, reason="", switch_requested=False,
            switched=False, skip_reason="", nodes=[],
        )
    assert len(load_history_rows(history, 3)) == 3
    assert len(load_history_rows(history, 100)) == 10


def test_load_rows_missing_file(tmp_path: Path):
    assert load_history_rows(tmp_path / "nonexistent.jsonl", 5) == []


def test_load_rows_invalid_limit(tmp_path: Path):
    with pytest.raises(ValueError, match="limit"):
        load_history_rows(tmp_path / "x.jsonl", 0)


# ---------------------------------------------------------------------------
# probe_history: penalties
# ---------------------------------------------------------------------------

def _write_history(path: Path, entries: list[dict]):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for entry in entries:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


def test_penalties_from_failures(tmp_path: Path):
    history = tmp_path / "history.jsonl"
    _write_history(history, [
        {"group": "G", "profile": "codex", "url": "https://x.com", "rounds": 3,
         "nodes": [
             {"name": "good", "success": 3, "failures": 0},
             {"name": "bad", "success": 1, "failures": 2},
         ]},
    ])
    penalties = load_history_penalties(history, "G", "codex", "https://x.com", 3)
    assert penalties.get("good", 0) == 0
    # bad: 2 failures * 75 + 2 missed * 25 = 200
    assert penalties["bad"] == 200


def test_penalties_cap(tmp_path: Path):
    history = tmp_path / "history.jsonl"
    entries = [
        {"group": "G", "profile": "codex", "url": "https://x.com", "rounds": 3,
         "nodes": [{"name": "n", "success": 0, "failures": 3}]}
        for _ in range(30)
    ]
    _write_history(history, entries)
    penalties = load_history_penalties(history, "G", "codex", "https://x.com", 3)
    assert penalties["n"] == 300  # capped at HISTORY_PENALTY_CAP_MS


def test_penalties_filter_by_group_profile_url(tmp_path: Path):
    history = tmp_path / "history.jsonl"
    _write_history(history, [
        {"group": "G1", "profile": "codex", "url": "https://x.com", "rounds": 3,
         "nodes": [{"name": "n", "success": 0, "failures": 3}]},
        {"group": "G2", "profile": "codex", "url": "https://x.com", "rounds": 3,
         "nodes": [{"name": "n", "success": 0, "failures": 3}]},
    ])
    assert load_history_penalties(history, "G1", "codex", "https://x.com", 3).get("n", 0) > 0
    assert load_history_penalties(history, "G2", "chatgpt", "https://x.com", 3) == {}
    assert load_history_penalties(history, "G1", "codex", "https://other.com", 3) == {}


def test_penalties_missing_file(tmp_path: Path):
    assert load_history_penalties(tmp_path / "nope.jsonl", "G", "codex", "https://x.com", 3) == {}


# ---------------------------------------------------------------------------
# runtime: Japan group, SSRDOG rules, atomic write
# ---------------------------------------------------------------------------

def _render_config(tmp_path: Path, config_yaml: str) -> dict:
    paths = default_paths(tmp_path)
    config_dir = paths.config_dir
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "config.yaml").write_text(config_yaml, encoding="utf-8")

    from cproxy.backend.runtime import RuntimeBackend
    RuntimeBackend(paths).render_runtime()

    runtime_path = paths.data_dir / "runtime.yaml"
    with runtime_path.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh)


BASE_CONFIG = """\
mixed-port: 7890
external-controller: 127.0.0.1:9090
proxies:
  - name: US-01
  - name: SG-01
  - name: JP-01
proxy-groups:
  - name: Auto
    type: fallback
    proxies: [US-01, SG-01]
  - name: 🇺🇸 United States
    type: select
    proxies: [US-01]
  - name: 🇸🇬 Singapore
    type: select
    proxies: [SG-01]
  - name: 🇯🇵 Japan
    type: select
    proxies: [JP-01]
rules:
  - DOMAIN-KEYWORD,chatgpt,SSRDOG
  - DOMAIN-SUFFIX,openai.com,SSRDOG
  - MATCH,Auto
"""


def test_render_japan_group_in_manual(tmp_path: Path):
    data = _render_config(tmp_path, BASE_CONFIG)
    groups = {g["name"]: g for g in data["proxy-groups"]}
    manual = groups["AI-MANUAL"]
    assert "🇯🇵 Japan" in manual["proxies"]
    assert manual["proxies"].index("🇯🇵 Japan") > manual["proxies"].index("AI-SG")


def test_render_no_japan_when_absent(tmp_path: Path):
    config = BASE_CONFIG.replace(
        "  - name: 🇯🇵 Japan\n    type: select\n    proxies: [JP-01]\n", ""
    ).replace("  - name: JP-01\n", "")
    data = _render_config(tmp_path, config)
    groups = {g["name"]: g for g in data["proxy-groups"]}
    assert "🇯🇵 Japan" not in groups
    assert "🇯🇵 Japan" not in groups["AI-MANUAL"]["proxies"]


def test_render_japan_auto_created_from_proxies(tmp_path: Path):
    config = """\
mixed-port: 7890
external-controller: 127.0.0.1:9090
proxies:
  - name: 🇺🇸 US 01
  - name: 🇸🇬 SG 01
  - name: 🇯🇵 JP 01
  - name: 🇯🇵 JP 02
proxy-groups:
  - name: Auto
    type: fallback
    proxies: [🇺🇸 US 01, 🇸🇬 SG 01]
rules:
  - MATCH,Auto
"""
    data = _render_config(tmp_path, config)
    groups = {g["name"]: g for g in data["proxy-groups"]}
    jp = groups["🇯🇵 Japan"]
    assert jp["proxies"] == ["🇯🇵 JP 01", "🇯🇵 JP 02"]
    assert "🇯🇵 Japan" in groups["AI-MANUAL"]["proxies"]


def test_render_ssrdog_rules_removed(tmp_path: Path):
    data = _render_config(tmp_path, BASE_CONFIG)
    ssrdog = [r for r in data["rules"] if "SSRDOG" in str(r)]
    assert ssrdog == []
    ai_rules = [r for r in data["rules"] if "AI-MANUAL" in str(r)]
    assert len(ai_rules) == 10


def test_render_atomic_write(tmp_path: Path):
    paths = default_paths(tmp_path)
    paths.config_dir.mkdir(parents=True, exist_ok=True)
    (paths.config_dir / "config.yaml").write_text(BASE_CONFIG, encoding="utf-8")

    from cproxy.backend.runtime import RuntimeBackend
    runtime_path = paths.data_dir / "runtime.yaml"
    RuntimeBackend(paths).render_runtime()
    assert runtime_path.is_file()
    assert not runtime_path.with_suffix(".tmp").exists()


def test_render_idempotent_no_snapshot(tmp_path: Path):
    paths = default_paths(tmp_path)
    paths.config_dir.mkdir(parents=True, exist_ok=True)
    (paths.config_dir / "config.yaml").write_text(BASE_CONFIG, encoding="utf-8")

    from cproxy.backend.runtime import RuntimeBackend
    from cproxy.snapshots import list_snapshots
    backend = RuntimeBackend(paths)
    backend.render_runtime()
    count1 = len(list_snapshots(paths, "runtime"))
    backend.render_runtime()
    count2 = len(list_snapshots(paths, "runtime"))
    assert count1 == count2


# ---------------------------------------------------------------------------
# process: lock file
# ---------------------------------------------------------------------------

def test_lock_file_prevents_concurrent_start(tmp_path: Path):
    paths = default_paths(tmp_path)
    paths.state_dir.mkdir(parents=True, exist_ok=True)
    paths.data_dir.mkdir(parents=True, exist_ok=True)

    from cproxy.backend.process import ProcessBackend
    backend = ProcessBackend(paths)

    lock = backend._lock_path()
    lock.touch()

    assert backend._acquire_lock() is False

    import os as _os
    old_time = time.time() - 60
    _os.utime(lock, (old_time, old_time))
    assert backend._acquire_lock() is True
    backend._release_lock()
    assert not lock.exists()


# ---------------------------------------------------------------------------
# CLI: new commands parse correctly
# ---------------------------------------------------------------------------

def test_cli_shadow_history_empty(tmp_path: Path, monkeypatch, capsys):
    from cproxy.cli import run
    monkeypatch.setattr("cproxy.cli.default_paths", lambda: default_paths(tmp_path))
    rc = run(["shadow-history"])
    assert rc == 0
    assert "-" in capsys.readouterr().out


def test_cli_ai_use_parses(tmp_path: Path, monkeypatch):
    from cproxy.cli import run
    from cproxy.backend.models import ProxyGroup
    from cproxy.backend.api import APIUnavailableError

    d = tmp_path / ".config" / "cproxy"
    d.mkdir(parents=True, exist_ok=True)
    (d / "config.yaml").write_text("external-controller: 127.0.0.1:9090\n", encoding="utf-8")
    monkeypatch.setattr("cproxy.cli.default_paths", lambda: default_paths(tmp_path))

    groups = {"G": ProxyGroup(name="G", type="Selector", current="n", candidates=["n"])}
    monkeypatch.setattr("cproxy.services.probe.APIBackend.get_groups", lambda self: dict(groups))
    monkeypatch.setattr("cproxy.services.probe.APIBackend.delay_test",
                        lambda self, t, u, to, *, request_timeout=None: {"delay": 100})

    rc = run(["ai-use", "codex", "--group", "G", "--raw"])
    assert rc == 0
