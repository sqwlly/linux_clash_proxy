"""AI 出口看门狗：语义判定、连续失败切换、冷却期与状态持久化。"""
from __future__ import annotations

import sys
import time
from pathlib import Path
from urllib.error import HTTPError

ROOT_DIR = Path(__file__).resolve().parents[1]
SRC_DIR = ROOT_DIR / "src"
sys.path.insert(0, str(SRC_DIR))

from cproxy.backend.models import ProxyGroup
from cproxy.config import AppPaths
from cproxy.services import ai_watch as ai_watch_mod
from cproxy.services.ai_watch import AIWatchService
from cproxy.services.diagnostics import PROBE_DEAD, PROBE_GEO_BLOCKED, PROBE_OK, _probe_target


# --- 语义分类 ---

class _FakeResponse:
    def __init__(self, status: int):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def getcode(self):
        return self.status


class _FakeHTTPError(HTTPError):
    def __init__(self, code: int, body: str):
        self.code = code
        self._body = body

    def read(self, amt=None):
        data = self._body.encode()
        return data if amt is None else data[:amt]


class _FakeOpener:
    def __init__(self, behavior: str):
        self._behavior = behavior

    def open(self, request, timeout=None):
        if self._behavior == "ok":
            return _FakeResponse(200)
        if self._behavior == "geo":
            raise _FakeHTTPError(400, "User location is not supported for the API use.")
        if self._behavior == "dead":
            raise _FakeHTTPError(500, "internal server error")
        raise AssertionError(f"unknown behavior {self._behavior}")


def test_probe_target_classifies(monkeypatch):
    from cproxy.services import diagnostics as diag

    monkeypatch.setattr(diag, "DEFAULT_AI_PROBE_RETRIES", 0)
    assert _probe_target(_FakeOpener("ok"), "http://x", 5)[0] == PROBE_OK
    assert _probe_target(_FakeOpener("geo"), "http://x", 5)[0] == PROBE_GEO_BLOCKED
    assert _probe_target(_FakeOpener("dead"), "http://x", 5)[0] == PROBE_DEAD


# --- 看门狗 ---

def _paths(tmp_path: Path) -> AppPaths:
    return AppPaths(config_dir=tmp_path / "cfg", data_dir=tmp_path / "data", state_dir=tmp_path / "state")


def _group(name: str, candidates: list[str], current: str) -> ProxyGroup:
    return ProxyGroup(name=name, type="fallback", current=current, candidates=list(candidates))


def _groups() -> dict[str, ProxyGroup]:
    return {
        "AI-MANUAL": _group("AI-MANUAL", ["US-01", "JP-01", "AI-AUTO"], "US-01"),
        "AI-GEMINI": _group("AI-GEMINI", ["KR-01", "GB-01"], "KR-01"),
        "AI-AUTO": _group("AI-AUTO", ["US-01", "JP-01"], "US-01"),
    }


class _FakeQuery:
    def __init__(self, groups: dict[str, ProxyGroup]):
        self.api = type("_API", (), {"get_groups": lambda self: groups})()
        self.switches: list[tuple[str, str]] = []

    def switch_group(self, group: str, target: str):
        self.switches.append((group, target))


def test_watch_switches_after_two_consecutive_failures(tmp_path, monkeypatch):
    paths = _paths(tmp_path)
    groups = _groups()
    svc = AIWatchService(paths)
    fake_q = _FakeQuery(groups)
    svc.query = fake_q
    monkeypatch.setattr(ai_watch_mod, "ai_target_verdict", lambda p, url: PROBE_GEO_BLOCKED)

    report1 = svc.watch()
    assert not any(r.switched for r in report1.results), "第一轮失败不切换（连续失败阈值 2）"

    report2 = svc.watch()
    manual = next(r for r in report2.results if r.group == "AI-MANUAL")
    gemini = next(r for r in report2.results if r.group == "AI-GEMINI")
    assert manual.switched and manual.target == "JP-01"
    assert gemini.switched and gemini.target == "GB-01"
    assert ("AI-MANUAL", "JP-01") in fake_q.switches
    assert ("AI-GEMINI", "GB-01") in fake_q.switches


def test_watch_ok_resets_failure_count(tmp_path, monkeypatch):
    paths = _paths(tmp_path)
    groups = _groups()
    monkeypatch.setattr(ai_watch_mod, "ai_target_verdict", lambda p, url: PROBE_GEO_BLOCKED)

    svc = AIWatchService(paths)
    svc.query = _FakeQuery(groups)
    svc.watch()  # fail_count=1

    # 第二轮恢复正常：不应切换，且计数清零
    monkeypatch.setattr(ai_watch_mod, "ai_target_verdict", lambda p, url: PROBE_OK)
    fake_q2 = _FakeQuery(groups)
    svc2 = AIWatchService(paths)
    svc2.query = fake_q2
    report = svc2.watch()
    assert not any(r.switched for r in report.results)
    assert fake_q2.switches == []

    # 第三轮又失败：计数应从 0 重新开始，仍不切换（不足 2 次连续）
    monkeypatch.setattr(ai_watch_mod, "ai_target_verdict", lambda p, url: PROBE_GEO_BLOCKED)
    svc3 = AIWatchService(paths)
    svc3.query = _FakeQuery(groups)
    assert not any(r.switched for r in svc3.watch().results)


def test_pick_next_skips_cooldown_and_nested_group(tmp_path):
    svc = AIWatchService(_paths(tmp_path))
    groups = _groups()
    group = groups["AI-MANUAL"]
    now = time.time()
    cooldown = {"JP-01": now}  # JP-01 在冷却期
    # candidates: US-01(current)、JP-01(冷却)、AI-AUTO(嵌套组) → 无可切
    assert svc._pick_next(groups, group, "US-01", cooldown, now) is None
    # 无冷却时 JP-01 可选
    assert svc._pick_next(groups, group, "US-01", {}, now) == "JP-01"


def test_state_persists_across_instances(tmp_path, monkeypatch):
    paths = _paths(tmp_path)
    groups = _groups()
    monkeypatch.setattr(ai_watch_mod, "ai_target_verdict", lambda p, url: PROBE_DEAD)

    svc1 = AIWatchService(paths)
    svc1.query = _FakeQuery(groups)
    svc1.watch()  # fail_count=1，写状态

    fake_q2 = _FakeQuery(groups)
    svc2 = AIWatchService(paths)
    svc2.query = fake_q2
    report = svc2.watch()  # 读到 fail_count=1 → 变 2 → 切换
    assert any(r.switched for r in report.results)
    assert fake_q2.switches
