from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT_DIR = Path(__file__).resolve().parents[1]
SRC_DIR = ROOT_DIR / "src"
sys.path.insert(0, str(SRC_DIR))

from cproxy.backend.api import APIUnavailableError
from cproxy.backend.models import ProxyGroup
from cproxy.config import default_paths
from cproxy.services.probe import (
    STRATEGIES,
    ProbeService,
    ProbeSummary,
    _preview_reason,
    active_candidates_after_round,
    collect_leaf_candidates,
    resolve_current_leaf,
    stability_verdict,
    switch_skip_reason,
)


def _g(name, gtype, now, candidates):
    return ProxyGroup(name=name, type=gtype, current=now, candidates=list(candidates))


NESTED = {
    "AI-MANUAL": _g("AI-MANUAL", "Selector", "AI-AUTO", ["AI-AUTO", "AI-US", "AI-SG", "JP"]),
    "AI-AUTO": _g("AI-AUTO", "Fallback", "AI-SG", ["AI-US", "AI-SG"]),
    "AI-US": _g("AI-US", "Fallback", "US-01", ["US-01", "US-02"]),
    "AI-SG": _g("AI-SG", "Fallback", "SG-01", ["SG-01"]),
    "JP": _g("JP", "Selector", "JP-01", ["JP-01"]),
}
CYCLE = {"A": _g("A", "Selector", "B", ["B"]), "B": _g("B", "Selector", "A", ["A"])}


# --- collect_leaf_candidates ---

def test_leaf_candidates_nested():
    c, p = collect_leaf_candidates(NESTED, "AI-MANUAL")
    assert set(c) == {"US-01", "US-02", "SG-01", "JP-01"}
    assert p["US-01"] == [("AI-MANUAL", "AI-AUTO"), ("AI-AUTO", "AI-US"), ("AI-US", "US-01")]
    assert p["JP-01"] == [("AI-MANUAL", "JP"), ("JP", "JP-01")]

def test_leaf_candidates_cycle_and_unknown():
    assert collect_leaf_candidates(CYCLE, "A")[0] == ["A"]
    c, p = collect_leaf_candidates({}, "X")
    assert c == ["X"] and p["X"] == []


# --- resolve_current_leaf ---

def test_resolve_current_leaf():
    assert resolve_current_leaf(NESTED, "AI-MANUAL") == "SG-01"
    assert resolve_current_leaf({"G": _g("G", "Selector", "n1", ["n1"])}, "G") == "n1"
    assert resolve_current_leaf(CYCLE, "A") in ("A", "B")
    assert resolve_current_leaf({}, "X") is None
    assert resolve_current_leaf({"G": _g("G", "Selector", "-", ["n1"])}, "G") is None


# --- active_candidates_after_round ---

def test_active_candidates():
    r = {"fast": {"delays": [80], "failures": 0}, "mid": {"delays": [200], "failures": 0},
         "slow": {"delays": [500], "failures": 0}, "cur": {"delays": [400], "failures": 0}}
    assert active_candidates_after_round(r, "cur") == {"fast", "mid", "cur"}
    assert len(active_candidates_after_round({"a": {"delays": [], "failures": 3}}, None)) >= 1


# --- stability_verdict ---

def test_stability_verdict():
    assert stability_verdict(ProbeSummary("n", (100, 110, 105), 0), 3, STRATEGIES["aggressive"]).stable
    v = stability_verdict(ProbeSummary("n", (100, 110), 1), 3, STRATEGIES["aggressive"])
    assert not v.stable and "全成功" in v.reason
    v = stability_verdict(ProbeSummary("n", (100, 5000, 100), 0), 3, STRATEGIES["aggressive"])
    assert not v.stable and "最大延迟" in v.reason
    v = stability_verdict(ProbeSummary("n", (2500, 2500, 2500), 0), 3, STRATEGIES["aggressive"])
    assert not v.stable and "平均延迟" in v.reason
    v = stability_verdict(ProbeSummary("n", (100,), 0), 1, STRATEGIES["conservative"])
    assert not v.stable and "至少" in v.reason
    assert not stability_verdict(None, 3, STRATEGIES["aggressive"]).stable


# --- switch_skip_reason / debounce ---

def test_switch_skip_reasons():
    S = STRATEGIES["conservative"]
    assert "没有可切换" in switch_skip_reason(None, "c", SimpleNamespace(stable=True), None, S)
    b = ProbeSummary("c", (100,), 0)
    assert "已是推荐" in switch_skip_reason(b, "c", SimpleNamespace(stable=True), b, S)
    assert switch_skip_reason(ProbeSummary("n", (80,), 0), "c", SimpleNamespace(stable=False),
                              ProbeSummary("c", (5000,), 0), S) == ""
    assert "防抖" in switch_skip_reason(ProbeSummary("n", (90, 90, 90), 0), "c",
                                        SimpleNamespace(stable=True), ProbeSummary("c", (100, 100, 100), 0), S)
    assert switch_skip_reason(ProbeSummary("n", (50, 50, 50), 0), "c",
                              SimpleNamespace(stable=True), ProbeSummary("c", (500, 500, 500), 0), S) == ""


def test_preview_reason_both_unstable_allows_switch():
    """When current is 0/5 and best candidate is 4/5, switch should not be blocked."""
    S = STRATEGIES["conservative"]
    best = ProbeSummary("US-01", (484, 500, 510, 551), 1)  # 4/5
    verdict = stability_verdict(best, 5, S)
    assert not verdict.stable  # 4/5 != 5/5

    current_summary = ProbeSummary("SG-01", (), 5)  # 0/5
    current_verdict = stability_verdict(current_summary, 5, S)
    assert not current_verdict.stable

    # Both unstable → should allow switch (empty string = no skip)
    assert _preview_reason(best, verdict, "SG-01", current_verdict, current_summary, S) == ""


def test_preview_reason_candidate_unstable_current_stable_blocks():
    """When current is stable but candidate is not, switch should be blocked."""
    S = STRATEGIES["conservative"]
    best = ProbeSummary("US-01", (484, 500, 510, 551), 1)  # 4/5
    verdict = stability_verdict(best, 5, S)
    assert not verdict.stable

    current_summary = ProbeSummary("SG-01", (100, 110, 105, 108, 102), 0)  # 5/5
    current_verdict = stability_verdict(current_summary, 5, S)
    assert current_verdict.stable

    reason = _preview_reason(best, verdict, "SG-01", current_verdict, current_summary, S)
    assert reason != "" and "全成功" in reason


# --- ProbeService integration ---

def _svc(tmp_path, groups, delays, log=None):
    d = tmp_path / ".config" / "cproxy"
    d.mkdir(parents=True, exist_ok=True)
    (d / "config.yaml").write_text("external-controller: 127.0.0.1:9090\n", encoding="utf-8")
    s = ProbeService(default_paths(tmp_path))
    def dt(t, u, to, *, request_timeout=None):
        if log is not None:
            log.setdefault("dc", []).append({"t": t, "to": to, "rt": request_timeout})
        if t in delays:
            return {"delay": delays[t]}
        raise APIUnavailableError("fail")
    def sw(g, t):
        if log is not None:
            log.setdefault("sw", []).append((g, t))
    s.api.get_groups = lambda: dict(groups)
    s.api.delay_test = dt
    s.api.switch_group = sw
    return s

def test_probe_reverse_switch(tmp_path):
    log: dict = {}
    r = _svc(tmp_path, NESTED, {"US-01": 300, "US-02": 80, "SG-01": 5000, "JP-01": 200}, log).probe(
        profile="chatgpt", strategy_name="aggressive", rounds=3, timeout=1000, switch=True)
    assert r.switched and r.best.name == "US-02"
    assert log["sw"] == [("AI-US", "US-02"), ("AI-AUTO", "AI-US"), ("AI-MANUAL", "AI-AUTO")]

def test_probe_no_switch_no_write(tmp_path):
    log: dict = {}
    r = _svc(tmp_path, NESTED, {"US-01": 300, "US-02": 80, "SG-01": 120, "JP-01": 200}, log).probe(
        profile="chatgpt", strategy_name="aggressive", rounds=3, timeout=1000)
    assert not r.switched and "sw" not in log

def test_probe_all_fail(tmp_path):
    r = _svc(tmp_path, NESTED, {}).probe(profile="chatgpt", strategy_name="aggressive", rounds=3, timeout=1000)
    assert r.best is None and all(s.success_count == 0 for s in r.summaries)

def test_probe_defaults_and_timeout(tmp_path):
    log: dict = {}
    r = _svc(tmp_path, {"G": _g("G", "Selector", "n", ["n"])}, {"n": 100}, log).probe(group="G", timeout=1000)
    assert r.rounds == 5 and r.strategy_name == "conservative"
    assert log["dc"][0]["rt"] == 10
    log2: dict = {}
    _svc(tmp_path, {"G": _g("G", "Selector", "n", ["n"])}, {"n": 100}, log2).probe(
        group="G", profile="codex", rounds=1, timeout=8000)
    assert log2["dc"][0]["rt"] == max(10, 8000 // 1000 + 2)

def test_probe_missing_group(tmp_path):
    with pytest.raises(RuntimeError, match="未找到"):
        _svc(tmp_path, {}, {}).probe(group="X")

def test_probe_debounce_and_switch(tmp_path):
    log: dict = {}
    r = _svc(tmp_path, {"G": _g("G", "Selector", "cur", ["cur", "new"])}, {"cur": 100, "new": 90}, log).probe(
        group="G", profile="chatgpt", strategy_name="aggressive", rounds=3, timeout=1000, switch=True)
    assert not r.switched and "防抖" in r.skip_reason
    log2: dict = {}
    r2 = _svc(tmp_path, {"G": _g("G", "Selector", "cur", ["cur", "new"])}, {"cur": 5000, "new": 80}, log2).probe(
        group="G", profile="chatgpt", strategy_name="aggressive", rounds=3, timeout=1000, switch=True)
    assert r2.switched and log2["sw"] == [("G", "new")]

def test_probe_invalid_rounds_timeout(tmp_path):
    s = _svc(tmp_path, {"G": _g("G", "Selector", "n", ["n"])}, {"n": 100})
    with pytest.raises(ValueError, match="rounds 必须大于 0"):
        s.probe(group="G", rounds=0, timeout=1000)
    with pytest.raises(ValueError, match="timeout 必须大于 0"):
        s.probe(group="G", rounds=1, timeout=0)


# --- CLI exit codes and output ---

def _cli(tmp_path, mp, groups, delays, switches=None, fail_all=False):
    from cproxy.cli import run
    d = tmp_path / ".config" / "cproxy"
    d.mkdir(parents=True, exist_ok=True)
    (d / "config.yaml").write_text("external-controller: 127.0.0.1:9090\n", encoding="utf-8")
    mp.setattr("cproxy.cli.default_paths", lambda: default_paths(tmp_path))
    mp.setattr("cproxy.services.probe.APIBackend.get_groups", lambda self: dict(groups))
    def fd(self, t, u, to, *, request_timeout=None):
        if fail_all or t not in delays:
            raise APIUnavailableError("fail")
        return {"delay": delays[t]}
    mp.setattr("cproxy.services.probe.APIBackend.delay_test", fd)
    if switches is not None:
        mp.setattr("cproxy.services.probe.APIBackend.switch_group", lambda self, g, t: switches.append((g, t)))
    return run

ARGS3 = ["--profile", "chatgpt", "--strategy", "aggressive", "--rounds", "3", "--timeout", "1000"]

def test_cli_rc_no_switch(tmp_path, monkeypatch, capsys):
    run = _cli(tmp_path, monkeypatch, {"G": _g("G", "Selector", "n", ["n"])}, {"n": 100})
    assert run(["probe-stable-node", "G", "--profile", "codex", "--rounds", "5", "--timeout", "1000"]) == 0
    run2 = _cli(tmp_path, monkeypatch, {"G": _g("G", "Selector", "n", ["n"])}, {}, fail_all=True)
    assert run2(["probe-stable-node", "G"] + ARGS3) == 1

def test_cli_rc_switch(tmp_path, monkeypatch, capsys):
    sw: list = []
    run = _cli(tmp_path, monkeypatch, {"G": _g("G", "Selector", "slow", ["slow", "fast"])},
               {"slow": 5000, "fast": 80}, sw)
    assert run(["probe-stable-node", "G"] + ARGS3 + ["--switch"]) == 0 and sw == [("G", "fast")]
    # already best
    run2 = _cli(tmp_path, monkeypatch, {"G": _g("G", "Selector", "n1", ["n1", "n2"])}, {"n1": 80, "n2": 200})
    assert run2(["probe-stable-node", "G"] + ARGS3 + ["--switch"]) == 0
    # debounce
    run3 = _cli(tmp_path, monkeypatch, {"G": _g("G", "Selector", "cur", ["cur", "new"])}, {"cur": 100, "new": 90})
    assert run3(["probe-stable-node", "G"] + ARGS3 + ["--switch"]) == 0
    # current is already best (n1=100 < n2=200) → "当前已是推荐稳定节点" → rc 0
    run4 = _cli(tmp_path, monkeypatch, {"G": _g("G", "Selector", "n1", ["n1", "n2"])}, {"n1": 100, "n2": 200})
    assert run4(["probe-stable-node", "G", "--strategy", "conservative", "--rounds", "1",
                 "--timeout", "1000", "--switch"]) == 0
    # both unstable (insufficient rounds), current is NOT best → switch allowed → rc 0
    run4b = _cli(tmp_path, monkeypatch, {"G": _g("G", "Selector", "n1", ["n1", "n2"])}, {"n1": 200, "n2": 100})
    assert run4b(["probe-stable-node", "G", "--strategy", "conservative", "--rounds", "1",
                  "--timeout", "1000", "--switch"]) == 0
    # all fail
    run5 = _cli(tmp_path, monkeypatch, {"G": _g("G", "Selector", "n", ["n"])}, {}, fail_all=True)
    assert run5(["probe-stable-node", "G"] + ARGS3 + ["--switch"]) == 1

def test_cli_rc_invalid_args(tmp_path, monkeypatch, capsys):
    run = _cli(tmp_path, monkeypatch, {"G": _g("G", "Selector", "n", ["n"])}, {"n": 100})
    assert run(["probe-stable-node", "G", "--rounds", "0", "--timeout", "1000"]) == 1
    assert "rounds 必须大于 0" in capsys.readouterr().err
    assert run(["probe-stable-node", "G", "--rounds", "1", "--timeout", "0"]) == 1
    assert "timeout 必须大于 0" in capsys.readouterr().err

def test_cli_raw_compat(tmp_path, monkeypatch, capsys):
    run = _cli(tmp_path, monkeypatch, {"G": _g("G", "Selector", "n1", ["n1", "n2"])}, {"n1": 100, "n2": 200})
    assert run(["probe-stable-node", "G"] + ARGS3 + ["--raw"]) == 0
    lines = capsys.readouterr().out.strip().split("\n")
    kv: dict[str, list] = {}
    for l in lines:
        p = l.split("\t")
        kv.setdefault(p[0], []).append(p)
    # CURRENT_STABLE / STABLE: true/false + reason
    assert kv["CURRENT_STABLE"][0][1] in ("true", "false") and len(kv["CURRENT_STABLE"][0]) >= 3
    assert kv["STABLE"][0][1] in ("true", "false") and len(kv["STABLE"][0]) >= 3
    # BEST: name + success + failures + avg + max + score
    b = kv["BEST"][0]
    assert b[1] == "n1" and "success=" in b[2] and "score=" in b[6]
    # No SWITCH / SKIP_SWITCH without --switch
    assert "SWITCH" not in kv and "SKIP_SWITCH" not in kv
    assert len(kv["NODE"]) == 2

def test_cli_raw_switch_and_skip(tmp_path, monkeypatch, capsys):
    sw: list = []
    run = _cli(tmp_path, monkeypatch, {"G": _g("G", "Selector", "slow", ["slow", "fast"])},
               {"slow": 5000, "fast": 80}, sw)
    assert run(["probe-stable-node", "G"] + ARGS3 + ["--switch", "--raw"]) == 0
    out = capsys.readouterr().out
    assert "SWITCH\tG\tfast" in out
    assert not any(l.startswith("SKIP_SWITCH\t") for l in out.strip().split("\n"))
    # debounce → SKIP_SWITCH with group + reason
    run2 = _cli(tmp_path, monkeypatch, {"G": _g("G", "Selector", "cur", ["cur", "new"])}, {"cur": 100, "new": 90})
    assert run2(["probe-stable-node", "G"] + ARGS3 + ["--switch", "--raw"]) == 0
    out2 = capsys.readouterr().out
    skips = [l for l in out2.strip().split("\n") if l.startswith("SKIP_SWITCH\t")]
    assert len(skips) == 1
    parts = skips[0].split("\t")
    assert parts[1] == "G" and "防抖" in parts[2]
    assert not any(l.startswith("SWITCH\t") for l in out2.strip().split("\n"))

def test_cli_human_preview(tmp_path, monkeypatch, capsys):
    # debounce → 不会切换 + reason
    run = _cli(tmp_path, monkeypatch, {"G": _g("G", "Selector", "cur", ["cur", "new"])}, {"cur": 100, "new": 90})
    run(["probe-stable-node", "G"] + ARGS3)
    out = capsys.readouterr().out
    assert "不会切换" in out and "防抖" in out
    # current unstable → 会切换到
    run2 = _cli(tmp_path, monkeypatch, {"G": _g("G", "Selector", "slow", ["slow", "fast"])},
                {"slow": 5000, "fast": 80})
    assert run2(["probe-stable-node", "G"] + ARGS3) == 0
    out2 = capsys.readouterr().out
    assert "会切换到" in out2 and "不会切换 ()" not in out2
