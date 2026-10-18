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


def test_render_manual_includes_kr_gb(tmp_path):
    config = """\
mixed-port: 7890
external-controller: 127.0.0.1:9090
proxies:
  - name: US-01
  - name: SG-01
  - name: JP-01
  - name: 🇰🇷韩国 01 | IEPL 1.7X | CM
  - name: 🇬🇧英国 01 | IEPL 1.7X | CM
proxy-groups:
  - name: Auto
    type: fallback
    proxies: [US-01, SG-01]
rules:
  - MATCH,Auto
"""
    paths = default_paths(tmp_path)
    paths.config_dir.mkdir(parents=True, exist_ok=True)
    (paths.config_dir / "config.yaml").write_text(config, encoding="utf-8")
    RuntimeBackend(paths).render_runtime()
    data = yaml.safe_load((paths.data_dir / "runtime.yaml").read_text(encoding="utf-8"))
    groups = {group["name"]: group for group in data["proxy-groups"]}

    manual = groups["AI-MANUAL"]
    assert "🇰🇷韩国 01 | IEPL 1.7X | CM" in manual["proxies"]
    assert "🇬🇧英国 01 | IEPL 1.7X | CM" in manual["proxies"]
    # 韩/英只进 codex 池，不污染 AI-US / AI-SG 自动池
    assert "🇰🇷韩国 01 | IEPL 1.7X | CM" not in groups["AI-US"]["proxies"]
    assert "🇬🇧英国 01 | IEPL 1.7X | CM" not in groups["AI-SG"]["proxies"]


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


def test_rebalance_marks_runtime_applied_and_switches_unstable_current(tmp_path, monkeypatch):
    from cproxy.backend.models import ProxyGroup
    from cproxy.services.probe import StabilityVerdict

    paths = _paths(tmp_path)
    applied: list[Path] = []
    reloaded: list[str] = []
    switched: list[tuple[str, str]] = []

    class _ProbeUnstableCurrent:
        def __init__(self, p):
            pass

        def probe(self, **kwargs):
            return SimpleNamespace(
                current="slow",
                current_verdict=StabilityVerdict(False, "失败 2 次"),
                verdict=StabilityVerdict(True, "5/5 稳定"),
                summaries=[
                    _summary("slow", (500,), failures=2),
                    _summary("stable", (100, 110)),
                ],
            )

    class _RunningProc:
        def is_running(self):
            return True

        def is_runtime_stale(self, path=None):
            return False

        def mark_runtime_applied(self, path=None):
            applied.append(Path(path))
            return True

    class _FakeAPI:
        def __init__(self, p):
            pass

        def reload_config(self, path: str):
            reloaded.append(path)

        def get_groups(self):
            return {
                "AI-MANUAL": ProxyGroup(
                    name="AI-MANUAL", type="fallback", current="slow", candidates=["slow", "stable", "AI-AUTO"]
                )
            }

    class _FakeQuery:
        def __init__(self, p):
            self.api = _FakeAPI(p)

        def switch_group(self, group: str, target: str):
            switched.append((group, target))

    monkeypatch.setattr(rebalance_mod, "ProbeService", _ProbeUnstableCurrent)
    monkeypatch.setattr(rebalance_mod, "RuntimeBackend", lambda p: _FakeBackend())
    monkeypatch.setattr(rebalance_mod, "ProcessBackend", lambda p: _RunningProc())
    monkeypatch.setattr(rebalance_mod, "APIBackend", _FakeAPI)
    monkeypatch.setattr(rebalance_mod, "QueryService", _FakeQuery)

    report = RebalanceService(paths).rebalance()
    assert report.hot_reloaded is True
    assert reloaded == ["/tmp/ignored-runtime.yaml"]
    assert applied == [Path("/tmp/ignored-runtime.yaml")]
    assert report.switched_to == "stable"
    assert switched == [("AI-MANUAL", "stable")]


def test_rebalance_suppresses_tail_noise_when_top_k_unchanged(tmp_path, monkeypatch):
    """当候选集不变且前 REBALANCE_TOP_K(5) 名完全一致，仅第 6、7 名因单轮抖动互换时，
    不应重写 ai-manual-order.json，也不触发无意义的热重载。"""
    paths = _paths(tmp_path)
    previous = ["N1", "N2", "N3", "N4", "N5", "N6", "N7"]
    order_file = ai_manual_order_file(paths)
    order_file.parent.mkdir(parents=True, exist_ok=True)
    order_file.write_text(json.dumps(previous), encoding="utf-8")

    runtime_path = paths.data_dir / "runtime.yaml"
    runtime_path.parent.mkdir(parents=True, exist_ok=True)
    runtime_path.write_text("same: true\n", encoding="utf-8")

    class _ProbeTailSwap:
        def __init__(self, p):
            pass

        def probe(self, **kwargs):
            # N1..N5 顺序不变，仅尾部 N6 与 N7 延迟互换
            return SimpleNamespace(
                summaries=[
                    _summary("N1", (100, 100)),
                    _summary("N2", (110, 110)),
                    _summary("N3", (120, 120)),
                    _summary("N4", (130, 130)),
                    _summary("N5", (140, 140)),
                    _summary("N7", (200,)),
                    _summary("N6", (205,)),
                ]
            )

    reloaded: list[str] = []

    class _NoChangeBackend:
        def render_runtime(self):
            return runtime_path

    class _RunningProc:
        def is_running(self):
            return True

        def is_runtime_stale(self, path=None):
            return False

    monkeypatch.setattr(rebalance_mod, "ProbeService", _ProbeTailSwap)
    monkeypatch.setattr(rebalance_mod, "RuntimeBackend", lambda p: _NoChangeBackend())
    monkeypatch.setattr(rebalance_mod, "ProcessBackend", lambda p: _RunningProc())
    monkeypatch.setattr(rebalance_mod, "APIBackend", lambda p: SimpleNamespace(reload_config=reloaded.append))

    report = RebalanceService(paths).rebalance()
    assert list(report.ordered) == previous
    assert json.loads(order_file.read_text(encoding="utf-8")) == previous
    assert report.hot_reloaded is False
    assert reloaded == []


def test_render_gemini_respects_order_and_prefer(tmp_path):
    from cproxy.config import ai_gemini_order_file

    config = """\
mixed-port: 7890
external-controller: 127.0.0.1:9090
ai-gemini-prefer:
  - 🇫🇷法国 01 | IEPL 1.7X | CM
proxies:
  - name: US-01
  - name: SG-01
  - name: 🇰🇷韩国 01 | 1X
  - name: 🇩🇪德国 01 | IEPL 1.7X | CM
  - name: 🇬🇧英国 01 | IEPL 1.7X | CM
  - name: 🇫🇷法国 01 | IEPL 1.7X | CM
proxy-groups:
  - name: Auto
    type: fallback
    proxies: [US-01, SG-01]
rules:
  - MATCH,Auto
"""
    paths = default_paths(tmp_path)
    paths.config_dir.mkdir(parents=True, exist_ok=True)
    (paths.config_dir / "config.yaml").write_text(config, encoding="utf-8")
    gemini_order = ai_gemini_order_file(paths)
    gemini_order.parent.mkdir(parents=True, exist_ok=True)
    gemini_order.write_text(
        json.dumps(["🇩🇪德国 01 | IEPL 1.7X | CM", "🇬🇧英国 01 | IEPL 1.7X | CM", "🇰🇷韩国 01 | 1X"]),
        encoding="utf-8",
    )
    RuntimeBackend(paths).render_runtime()
    data = yaml.safe_load((paths.data_dir / "runtime.yaml").read_text(encoding="utf-8"))
    gemini = next(g for g in data["proxy-groups"] if g["name"] == "AI-GEMINI")
    assert gemini["proxies"] == [
        "🇫🇷法国 01 | IEPL 1.7X | CM",
        "🇩🇪德国 01 | IEPL 1.7X | CM",
        "🇬🇧英国 01 | IEPL 1.7X | CM",
        "🇰🇷韩国 01 | 1X",
    ]


def test_rebalance_orders_and_switches_ai_gemini(tmp_path, monkeypatch):
    from cproxy.backend.models import ProxyGroup
    from cproxy.config import ai_gemini_order_file
    from cproxy.services.probe import StabilityVerdict

    paths = _paths(tmp_path)
    switched: list[tuple[str, str]] = []

    class _DualGroupProbe:
        def __init__(self, p):
            pass

        def probe(self, **kwargs):
            group = kwargs.get("group")
            if group == "AI-GEMINI":
                return SimpleNamespace(
                    current="KR-1X",
                    current_verdict=StabilityVerdict(False, "失败 3 次"),
                    verdict=StabilityVerdict(True, "5/5 稳定"),
                    summaries=[
                        _summary("KR-1X", (700,), failures=3),
                        _summary("DE-IEPL", (180, 190)),
                    ],
                )
            return SimpleNamespace(
                current="US-01",
                current_verdict=StabilityVerdict(True, "5/5 稳定"),
                verdict=StabilityVerdict(True, "5/5 稳定"),
                summaries=[
                    _summary("US-01", (150, 160)),
                    _summary("SG-01", (220, 230)),
                ],
            )

    class _RunningProc:
        def is_running(self):
            return True

        def is_runtime_stale(self, path=None):
            return False

        def mark_runtime_applied(self, path=None):
            return True

    class _FakeAPI:
        def __init__(self, p):
            pass

        def reload_config(self, path: str):
            pass

        def get_groups(self):
            return {
                "AI-MANUAL": ProxyGroup(name="AI-MANUAL", type="fallback", current="US-01", candidates=["US-01", "SG-01"]),
                "AI-GEMINI": ProxyGroup(
                    name="AI-GEMINI", type="fallback", current="KR-1X", candidates=["KR-1X", "DE-IEPL"]
                ),
            }

    class _FakeQuery:
        def __init__(self, p):
            self.api = _FakeAPI(p)

        def switch_group(self, group: str, target: str):
            switched.append((group, target))

    monkeypatch.setattr(rebalance_mod, "ProbeService", _DualGroupProbe)
    monkeypatch.setattr(rebalance_mod, "RuntimeBackend", lambda p: _FakeBackend())
    monkeypatch.setattr(rebalance_mod, "ProcessBackend", lambda p: _RunningProc())
    monkeypatch.setattr(rebalance_mod, "APIBackend", _FakeAPI)
    monkeypatch.setattr(rebalance_mod, "QueryService", _FakeQuery)

    report = RebalanceService(paths).rebalance()
    assert json.loads(ai_gemini_order_file(paths).read_text(encoding="utf-8")) == ["DE-IEPL", "KR-1X"]
    assert report.gemini_ordered == ("DE-IEPL", "KR-1X")
    assert report.gemini_switched_to == "DE-IEPL"
    assert switched == [("AI-GEMINI", "DE-IEPL")]
