"""AI-MANUAL 稳定性兜底：fallback 类型、排序合并与 rebalance 持久化。"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import yaml

ROOT_DIR = Path(__file__).resolve().parents[1]
SRC_DIR = ROOT_DIR / "src"
sys.path.insert(0, str(SRC_DIR))

from cproxy.backend.runtime import RuntimeBackend, _load_ai_manual_order, _order_manual_candidates
from cproxy.config import AppPaths, ai_manual_order_file, default_paths
from cproxy.services import rebalance as rebalance_mod
from cproxy.services.probe import ProbeSummary
from cproxy.services.rebalance import RebalanceService


# --- _order_manual_candidates ---

def test_order_prefer_first_then_order_then_static():
    leaves = ["US-01", "US-02", "JP-01", "SG-01"]
    ordered = _order_manual_candidates(leaves, ["SG-01"], ["JP-01", "US-01"])
    assert ordered == ["SG-01", "JP-01", "US-01", "US-02"]


def test_order_dedups_and_filters_unknown():
    leaves = ["US-01", "US-02"]
    ordered = _order_manual_candidates(leaves, ["US-01", "US-01"], ["ghost", "US-02"])
    assert ordered == ["US-01", "US-02"]


def test_order_none_falls_back_to_static():
    leaves = ["US-01", "JP-01"]
    assert _order_manual_candidates(leaves, None, None) == leaves


# --- _load_ai_manual_order ---

def _paths(tmp_path: Path) -> AppPaths:
    return AppPaths(config_dir=tmp_path / "cfg", data_dir=tmp_path / "data", state_dir=tmp_path / "state")


def test_load_order_missing(tmp_path):
    assert _load_ai_manual_order(_paths(tmp_path)) == []


def test_load_order_roundtrip(tmp_path):
    paths = _paths(tmp_path)
    order_file = ai_manual_order_file(paths)
    order_file.parent.mkdir(parents=True, exist_ok=True)
    order_file.write_text(json.dumps(["US-01", "JP-01"]), encoding="utf-8")
    assert _load_ai_manual_order(paths) == ["US-01", "JP-01"]


def test_load_order_corrupt(tmp_path):
    paths = _paths(tmp_path)
    order_file = ai_manual_order_file(paths)
    order_file.parent.mkdir(parents=True, exist_ok=True)
    order_file.write_text("{oops", encoding="utf-8")
    assert _load_ai_manual_order(paths) == []


# --- render：AI-MANUAL 应为 fallback 且按 prefer/order 排序 ---

BASE_CONFIG = """\
mixed-port: 7890
external-controller: 127.0.0.1:9090
proxies:
  - name: US-01
  - name: US-02
  - name: SG-01
  - name: JP-01
proxy-groups:
  - name: Auto
    type: fallback
    proxies: [US-01, SG-01]
rules:
  - MATCH,Auto
"""


def _render(tmp_path: Path, extra_config: str = "") -> dict:
    paths = default_paths(tmp_path)
    paths.config_dir.mkdir(parents=True, exist_ok=True)
    (paths.config_dir / "config.yaml").write_text(BASE_CONFIG + extra_config, encoding="utf-8")
    RuntimeBackend(paths).render_runtime()
    return yaml.safe_load((paths.data_dir / "runtime.yaml").read_text(encoding="utf-8"))


def _manual_group(data: dict) -> dict:
    return next(group for group in data["proxy-groups"] if group["name"] == "AI-MANUAL")


def test_render_manual_is_fallback(tmp_path):
    manual = _manual_group(_render(tmp_path))
    assert manual["type"] == "fallback"
    assert manual["url"]
    assert manual["interval"] == 300
    assert manual["proxies"][-1] == "AI-AUTO"
    assert manual["proxies"][0] == "US-01"


def test_render_manual_respects_prefer_and_order(tmp_path):
    paths = default_paths(tmp_path)
    order_file = ai_manual_order_file(paths)
    order_file.parent.mkdir(parents=True, exist_ok=True)
    order_file.write_text(json.dumps(["JP-01", "SG-01"]), encoding="utf-8")

    manual = _manual_group(_render(tmp_path, extra_config="\nai-manual-prefer: [SG-01]\n"))
    # prefer 置前，order 居中（跳过已 pin 的 SG-01），静态 remainder 垫尾
    assert manual["proxies"][:3] == ["SG-01", "JP-01", "US-01"]


# --- RebalanceService ---

def _summary(name: str, delays: tuple[int, ...], failures: int = 0) -> ProbeSummary:
    return ProbeSummary(name=name, delays=delays, failures=failures, history_penalty_ms=0)


class _FakeProbeService:
    def __init__(self, paths):
        self.paths = paths

    def probe(self, **kwargs):
        return SimpleNamespace(
            summaries=[
                _summary("dead", (), failures=5),
                _summary("slow", (500,)),
                _summary("stable", (100, 110)),
            ]
        )


class _FakeBackend:
    def render_runtime(self):
        return Path("/tmp/ignored-runtime.yaml")


class _FakeProcess:
    def __init__(self, running: bool):
        self._running = running

    def is_running(self):
        return self._running


def test_rebalance_writes_order_and_rerenders(tmp_path, monkeypatch):
    paths = _paths(tmp_path)
    monkeypatch.setattr(rebalance_mod, "ProbeService", _FakeProbeService)
    monkeypatch.setattr(rebalance_mod, "RuntimeBackend", lambda p: _FakeBackend())
    monkeypatch.setattr(rebalance_mod, "ProcessBackend", lambda p: _FakeProcess(False))

    report = RebalanceService(paths).rebalance()
    assert report.rerendered is True
    assert report.hot_reloaded is False
    assert json.loads(ai_manual_order_file(paths).read_text(encoding="utf-8")) == ["stable", "slow", "dead"]


def test_rebalance_dry_run_does_not_write(tmp_path, monkeypatch):
    paths = _paths(tmp_path)
    monkeypatch.setattr(rebalance_mod, "ProbeService", _FakeProbeService)

    report = RebalanceService(paths).rebalance(dry_run=True)
    assert report.rerendered is False
    assert report.reason == "dry-run"
    assert not ai_manual_order_file(paths).exists()
