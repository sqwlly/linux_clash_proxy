from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest
import yaml

ROOT_DIR = Path(__file__).resolve().parents[1]
SRC_DIR = ROOT_DIR / "src"
sys.path.insert(0, str(SRC_DIR))

from cproxy.config import default_paths  # noqa: E402
from cproxy.services.probe_history import (  # noqa: E402
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


def test_penalties_ignores_pruned_zero_failure_and_separates_chronic_failures(tmp_path: Path):
    """回归：折半淘汰下 0 失败但仅跑了 1 轮的健康节点不应被罚分；
    偶发 1 次失败的节点与高频失败节点的惩罚值必须拉开差距，不能双双封顶 300ms。"""
    history = tmp_path / "history.jsonl"
    entries = []
    for idx in range(10):
        entries.append(
            {
                "group": "G",
                "profile": "codex",
                "url": "https://x.com",
                "rounds": 5,
                "nodes": [
                    {"name": "pruned-iepl", "success": 1, "total": 1, "failures": 0},
                    {"name": "rare-blip", "success": 0 if idx == 0 else 5, "failures": 5 if idx == 0 else 0},
                    {"name": "chronic-1x", "success": 0 if idx < 8 else 5, "failures": 5 if idx < 8 else 0},
                ],
            }
        )
    _write_history(history, entries)
    penalties = load_history_penalties(history, "G", "codex", "https://x.com", 5)
    assert penalties.get("pruned-iepl", 0) == 0
    assert 0 < penalties["rare-blip"] < 60
    assert penalties["chronic-1x"] >= 200
    assert penalties["chronic-1x"] > penalties["rare-blip"] * 4


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


def _groups_by_name(data: dict) -> dict:
    return {g["name"]: g for g in data["proxy-groups"]}


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
    assert "JP-01" in manual["proxies"]
    assert "AI-US" not in manual["proxies"]
    assert "AI-SG" not in manual["proxies"]
    assert "🇯🇵 Japan" not in manual["proxies"]
    assert "🇺🇸 United States" not in manual["proxies"]
    assert manual["proxies"][0] == "US-01"
    assert "🇯🇵 Japan" not in groups


def test_render_prefers_non_iepl_nodes_in_region_groups(tmp_path: Path):
    config = """\
mixed-port: 7890
external-controller: 127.0.0.1:9090
proxies:
  - name: 🇺🇸 US 01 | 1X
  - name: 🇺🇸 US 02 | IEPL
  - name: 🇸🇬 SG 01 | 1X
  - name: 🇯🇵 JP 01 | 1X
  - name: 🇯🇵 JP 02 | IEPL + UDP 3X | CM
proxy-groups:
  - name: Auto
    type: fallback
    proxies: [🇺🇸 US 01 | 1X]
  - name: 🇺🇸 United States
    type: select
    proxies: [🇺🇸 US 01 | 1X, 🇺🇸 US 02 | IEPL]
  - name: 🇸🇬 Singapore
    type: select
    proxies: [🇸🇬 SG 01 | 1X]
  - name: 🇯🇵 Japan
    type: select
    proxies: [🇯🇵 JP 01 | 1X, 🇯🇵 JP 02 | IEPL + UDP 3X | CM]
rules:
  - MATCH,Auto
"""
    data = _render_config(tmp_path, config)
    groups = {g["name"]: g for g in data["proxy-groups"]}
    assert "🇯🇵 Japan" not in groups
    assert "🇺🇸 United States" not in groups
    assert groups["AI-MANUAL"]["proxies"][0] == "🇺🇸 US 01 | 1X"
    assert "🇯🇵 JP 01 | 1X" in groups["AI-MANUAL"]["proxies"]
    assert groups["AI-US"]["proxies"][0] == "🇺🇸 US 01 | 1X"


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
    assert "🇯🇵 Japan" not in groups
    assert "🇯🇵 JP 01" in groups["AI-MANUAL"]["proxies"]
    assert "🇯🇵 JP 02" in groups["AI-MANUAL"]["proxies"]


def test_render_ssrdog_rules_removed(tmp_path: Path):
    data = _render_config(tmp_path, "tcp-concurrent: true\n" + BASE_CONFIG)
    assert data["tcp-concurrent"] is False
    ssrdog = [r for r in data["rules"] if "SSRDOG" in str(r)]
    assert ssrdog == []
    manual_rules = [r for r in data["rules"] if "AI-MANUAL" in str(r)]
    gemini_rules = [r for r in data["rules"] if "AI-GEMINI" in str(r)]
    assert len(manual_rules) == 16
    assert len(gemini_rules) == 9
    # Google / Gemini 规则前移到首条，且排在 PROCESS-NAME,agy 之前
    assert "AI-GEMINI" in str(data["rules"][0])
    groups = _groups_by_name(data)
    assert groups["AI-GEMINI"]["type"] == "fallback"
    # 无韩台欧节点时退回日美新
    assert groups["AI-GEMINI"]["proxies"] == ["JP-01", "US-01", "SG-01"]


def test_render_gemini_prefers_friendly_regions(tmp_path: Path):
    config = """\
mixed-port: 7890
external-controller: 127.0.0.1:9090
proxies:
  - name: 🇺🇸 US 01 | 1X
  - name: 🇸🇬 SG 01 | 1X
  - name: 🇯🇵 JP 01 | 1X
  - name: 🇰🇷 KR 01 | IEPL
  - name: 🇰🇷 KR 01 | 1X
  - name: 🇹🇼 TW 01 | 1X
  - name: 🇭🇰 HK 01 | 1X
proxy-groups:
  - name: Auto
    type: fallback
    proxies: [🇺🇸 US 01 | 1X]
rules:
  - MATCH,Auto
"""
    data = _render_config(tmp_path, config)
    gemini = _groups_by_name(data)["AI-GEMINI"]["proxies"]
    assert gemini[0] == "🇰🇷 KR 01 | 1X"
    assert "🇹🇼 TW 01 | 1X" in gemini
    assert "🇭🇰 HK 01 | 1X" not in gemini
    assert "🇺🇸 US 01 | 1X" not in gemini
    assert "DOMAIN-SUFFIX,googleapis.com,AI-GEMINI" in data["rules"]
    assert data["rules"].index("DOMAIN-SUFFIX,google.com,AI-GEMINI") < data["rules"].index(
        "PROCESS-NAME,agy,AI-MANUAL"
    )


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
    from cproxy.backend.models import ProxyGroup
    from cproxy.cli import run

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


def test_render_match_country_selectors_and_strips_panel(tmp_path: Path):
    config = """\
mixed-port: 7890
external-controller: 127.0.0.1:9090
proxies:
  - name: 🇭🇰香港 01
  - name: 🇯🇵日本 01
  - name: 🇺🇸美国 01
  - name: 🇸🇬新加坡 01
  - name: 剩余流量：10 GB
  - name: Mitce HK-1
proxy-groups:
  - name: CyberGuard
    type: select
    proxies: [自动选择, 🇭🇰香港 01, 🇯🇵日本 01, 🇺🇸美国 01, 🇸🇬新加坡 01, 剩余流量：10 GB, Mitce]
  - name: 自动选择
    type: url-test
    proxies: [🇭🇰香港 01, 🇯🇵日本 01, 剩余流量：10 GB]
  - name: Mitce
    type: select
    proxies: [Mitce HK-1]
subscriptions:
  - name: Mitce
    url: https://example.test/mitce
rules:
  - MATCH,CyberGuard
"""
    data = _render_config(tmp_path, config)
    groups = {g["name"]: g for g in data["proxy-groups"]}
    cyber = groups["CyberGuard"]
    assert "Mitce" not in cyber["proxies"]
    assert "剩余流量：10 GB" not in cyber["proxies"]
    assert "🇯🇵 Japan" not in cyber["proxies"]
    assert "自动选择" in cyber["proxies"]
    assert "香港" in cyber["proxies"]
    assert "日本" in cyber["proxies"]
    assert "美国" in cyber["proxies"]
    assert "新加坡" in cyber["proxies"]
    assert groups["香港"]["proxies"] == ["🇭🇰香港 01"]
    assert groups["日本"]["proxies"] == ["🇯🇵日本 01"]
    assert "剩余流量：10 GB" not in groups["自动选择"]["proxies"]
    manual = groups["AI-MANUAL"]
    assert "AI-US" not in manual["proxies"]
    assert "AI-SG" not in manual["proxies"]
    assert "🇺🇸 United States" not in manual["proxies"]
    assert "🇺🇸美国 01" in manual["proxies"]
    assert "AI-AUTO" in manual["proxies"]
    assert "🇯🇵 Japan" not in groups
    assert groups["AI-US"]["type"] == "fallback"
    assert groups["AI-SG"]["type"] == "fallback"


def test_render_upgrades_http_auto_group_url_and_adds_ip_cidr_no_resolve(tmp_path: Path):
    """自动测速/故障转移组的明文 http:// 测速地址应升级为 HTTPS TEST_URL 并剔除繁体面板伪节点；
    裸 3 段 IP-CIDR / IP-CIDR6 规则应自动补齐 `,no-resolve`。"""
    from cproxy.backend.runtime import TEST_URL

    config = """\
mixed-port: 7890
external-controller: 127.0.0.1:9090
proxies:
  - name: 🇺🇸美国 01
  - name: 🇸🇬新加坡 01
  - name: "官網: www.cyberguard.best"
  - name: 距离下次重置剩余：9 天
proxy-groups:
  - name: CyberGuard
    type: select
    proxies: [自动选择, 故障转移, 🇺🇸美国 01, 🇸🇬新加坡 01, "官網: www.cyberguard.best", 距离下次重置剩余：9 天]
  - name: 自动选择
    type: url-test
    url: http://www.gstatic.com/generate_204
    proxies: ["官網: www.cyberguard.best", 距离下次重置剩余：9 天, 🇺🇸美国 01, 🇸🇬新加坡 01]
  - name: 故障转移
    type: fallback
    url: http://www.gstatic.com/generate_204
    proxies: ["官網: www.cyberguard.best", 🇺🇸美国 01, 🇸🇬新加坡 01]
rules:
  - IP-CIDR,192.168.0.0/16,DIRECT
  - IP-CIDR6,fe80::/10,DIRECT
  - IP-CIDR,10.0.0.0/8,DIRECT,no-resolve
  - MATCH,CyberGuard
"""
    data = _render_config(tmp_path, config)
    groups = {g["name"]: g for g in data["proxy-groups"]}
    assert "其他" not in groups
    for gname in ("自动选择", "故障转移"):
        assert groups[gname]["url"] == TEST_URL
        assert "官網: www.cyberguard.best" not in groups[gname]["proxies"]
        assert "距离下次重置剩余：9 天" not in groups[gname]["proxies"]
    assert "IP-CIDR,192.168.0.0/16,DIRECT,no-resolve" in data["rules"]
    assert "IP-CIDR6,fe80::/10,DIRECT,no-resolve" in data["rules"]
    assert "IP-CIDR,10.0.0.0/8,DIRECT,no-resolve" in data["rules"]


# ---------------------------------------------------------------------------
# 重名节点防护：机场订阅会给出同名不同 server 的节点，mihomo 遇重名直接
# fatal 退出（"proxy X is the duplicate name"），刷新因此把代理搞挂。
# ---------------------------------------------------------------------------

def test_dedupe_proxy_names_renames_and_rewrites_group_refs():
    from cproxy.services.refresh import _dedupe_proxy_names

    config = {
        "proxies": [
            {"name": "JP 01", "server": "1.1.1.1"},
            {"name": "US 01", "server": "2.2.2.2"},
            {"name": "JP 01", "server": "3.3.3.3"},
        ],
        "proxy-groups": [{"name": "G", "proxies": ["JP 01", "US 01", "JP 01"]}],
    }

    renames = _dedupe_proxy_names(config)

    assert renames == [("JP 01", "JP 01 #2")]
    assert [p["name"] for p in config["proxies"]] == ["JP 01", "US 01", "JP 01 #2"]
    # 组内引用按出现顺序同步改写，两个节点都还在
    assert config["proxy-groups"][0]["proxies"] == ["JP 01", "US 01", "JP 01 #2"]


def test_dedupe_proxy_names_noop_without_duplicates():
    from cproxy.services.refresh import _dedupe_proxy_names

    config = {
        "proxies": [{"name": "A"}, {"name": "B"}],
        "proxy-groups": [{"name": "G", "proxies": ["A", "B"]}],
    }
    before = json.dumps(config, sort_keys=True, ensure_ascii=False)

    assert _dedupe_proxy_names(config) == []
    assert json.dumps(config, sort_keys=True, ensure_ascii=False) == before


def test_dedupe_proxy_names_avoids_collision_with_existing_name():
    from cproxy.services.refresh import _dedupe_proxy_names

    # 订阅里本就存在 "A #2"：生成的新名必须避让，否则又造出新的重名
    config = {
        "proxies": [{"name": "A"}, {"name": "A #2"}, {"name": "A"}],
        "proxy-groups": [],
    }

    _dedupe_proxy_names(config)

    names = [p["name"] for p in config["proxies"]]
    assert len(set(names)) == len(names), names


def test_update_source_from_subscription_dedupes_duplicate_nodes(tmp_path: Path, monkeypatch):
    """端到端：订阅自带重名节点时，落盘的 config.yaml 必须已无重名。"""
    import cproxy.services.refresh as refresh_mod

    payload = {
        "proxies": [
            {"name": "JP 01", "type": "ss", "server": "1.1.1.1", "port": 1, "cipher": "aes-128-gcm", "password": "x"},
            {"name": "JP 01", "type": "ss", "server": "2.2.2.2", "port": 1, "cipher": "aes-128-gcm", "password": "x"},
        ],
        "proxy-groups": [{"name": "G", "type": "select", "proxies": ["JP 01", "JP 01"]}],
    }
    raw = yaml.safe_dump(payload, allow_unicode=True).encode("utf-8")
    monkeypatch.setattr(refresh_mod, "_download_subscription", lambda *a, **k: (raw, None))

    paths = default_paths(tmp_path)
    paths.config_dir.mkdir(parents=True, exist_ok=True)
    (paths.config_dir / "config.yaml").write_text("proxies: []\nproxy-groups: []\n", encoding="utf-8")

    out = refresh_mod.update_source_from_subscription(paths, "https://example.com/sub")
    data = yaml.safe_load(out.read_text(encoding="utf-8"))

    names = [proxy["name"] for proxy in data["proxies"]]
    assert len(set(names)) == len(names), names
    assert len(names) == 2  # 节点没被丢弃


# ---------------------------------------------------------------------------
# 刷新后的启动校验：配置被接受 ≠ 内核活着。新 runtime 加载失败时必须回滚，
# 而不是让 refresh 回报成功却留下一个挂掉的代理。
# ---------------------------------------------------------------------------

def _make_paths(tmp_path: Path):
    paths = default_paths(tmp_path)
    for directory in (paths.config_dir, paths.data_dir, paths.state_dir):
        directory.mkdir(parents=True, exist_ok=True)
    return paths


def test_rollback_runtime_restores_latest_snapshot(tmp_path: Path):
    from cproxy.config import runtime_file
    from cproxy.services.refresh import RefreshService
    from cproxy.snapshots import snapshot_file

    paths = _make_paths(tmp_path)
    runtime = runtime_file(paths)
    runtime.write_text("marker: good\n", encoding="utf-8")
    snapshot_file(paths, runtime, "runtime")
    runtime.write_text("marker: broken\n", encoding="utf-8")  # 模拟 render 写入的坏配置

    restored = RefreshService(paths)._rollback_runtime()

    assert restored == runtime
    assert runtime.read_text(encoding="utf-8") == "marker: good\n"


def test_rollback_runtime_returns_none_without_snapshot(tmp_path: Path):
    from cproxy.services.refresh import RefreshService

    assert RefreshService(_make_paths(tmp_path))._rollback_runtime() is None


def test_refresh_rolls_back_and_restarts_when_new_config_fails(tmp_path: Path, monkeypatch):
    import cproxy.services.refresh as refresh_mod
    from cproxy.config import runtime_file
    from cproxy.snapshots import snapshot_file

    paths = _make_paths(tmp_path)
    runtime = runtime_file(paths)
    good = "marker: good\n"
    runtime.write_text(good, encoding="utf-8")
    snapshot_file(paths, runtime, "runtime")  # 上一版可用配置
    (paths.config_dir / "config.yaml").write_text(
        "proxies: []\nproxy-groups: []\nsubscription-url: https://example.com/sub\n", encoding="utf-8"
    )

    service = refresh_mod.RefreshService(paths)
    monkeypatch.setattr(service.process, "is_running", lambda: True)
    restarts = []
    monkeypatch.setattr(service.process, "restart", lambda: restarts.append(1) or 42)
    monkeypatch.setattr(refresh_mod, "update_source_from_subscription", lambda *a, **k: runtime)

    def fake_render(self):
        runtime.write_text("marker: broken\n", encoding="utf-8")  # 新配置(坏)
        return runtime

    monkeypatch.setattr(refresh_mod.RuntimeBackend, "render_runtime", fake_render)

    # 强制走重启分支：热重载不可用
    class _DeadAPI:
        def reload_config(self, _path):
            raise RuntimeError("hot reload unavailable")

    monkeypatch.setattr(service, "_api_factory", lambda _paths: _DeadAPI())

    waits = []

    def fake_wait():
        waits.append(1)
        if len(waits) == 1:  # 新配置加载失败
            raise RuntimeError("Mihomo API 未就绪")

    monkeypatch.setattr(service, "_wait_for_api", fake_wait)

    with pytest.raises(RuntimeError, match="已回滚"):
        service.refresh()

    assert len(waits) == 2, "回滚后应复检一次 API"
    assert len(restarts) == 2, "重启应发生两次：应用新配置 + 回滚后恢复"
    assert runtime.read_text(encoding="utf-8") == good, "runtime 必须已回滚到可用版本"

