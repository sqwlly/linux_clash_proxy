"""回归测试：覆盖 runtime / config / refresh / process / traffic / rules / snapshots / geodata / verge 审计修复。"""

from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import threading
import time
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError

import pytest
import yaml

from cproxy.backend import runtime as runtime_mod
from cproxy.backend import runtime_metrics as metrics_mod
from cproxy.backend.api import (
    APIBackend,
    APIRequestError,
    APIUnavailableError,
    normalize_controller_endpoint,
    normalize_controller_url,
)
from cproxy.backend.models import ProxyGroup
from cproxy.backend.process import ProcessBackend
from cproxy.backend.runtime import (
    AI_GEMINI_KEEPALIVE_GROUP,
    AI_MANUAL_KEEPALIVE_GROUP,
    KEEPALIVE_TEST_URL,
    OPENAI_DIRECT_HOSTS,
    RuntimeBackend,
    _ws_cdn_ip_log_penalties,
    refresh_ws_cdn_ips_if_changed,
)
from cproxy.config import (
    AppPaths,
    atomic_write_bytes,
    atomic_write_text,
    config_file,
    load_yaml_file,
    read_config,
    runtime_file,
)
from cproxy.geodata import check_country_mmdb
from cproxy.services import h2_keepalive as h2_mod
from cproxy.services import refresh as refresh_mod
from cproxy.services import verge as verge_mod
from cproxy.services.rules import add_custom_rule, load_custom_rules, remove_custom_rule
from cproxy.services.traffic import TrafficService
from cproxy.snapshots import _evaluate_tier_retention, list_snapshots, restore_snapshot, snapshot_file, snapshots_dir

ROOT_DIR = Path(__file__).resolve().parents[1]


def _paths(tmp_path: Path) -> AppPaths:
    paths = AppPaths(
        config_dir=tmp_path / "cfg",
        data_dir=tmp_path / "data",
        state_dir=tmp_path / "state",
    )
    paths.config_dir.mkdir(parents=True, exist_ok=True)
    paths.data_dir.mkdir(parents=True, exist_ok=True)
    paths.state_dir.mkdir(parents=True, exist_ok=True)
    return paths


def _minimal_render_cfg() -> dict:
    return {
        "mixed-port": 7890,
        "external-controller": "127.0.0.1:9090",
        "secret": "top-secret-token",
        "proxies": [
            {"name": "🇺🇸美国 01 | 1X", "type": "vless", "server": "1.1.1.1", "port": 443},
            {"name": "🇸🇬新加坡 01 | 1X", "type": "vless", "server": "1.1.1.2", "port": 443},
            {"name": "🇰🇷韩国 01 | 1X", "type": "vless", "server": "1.1.1.3", "port": 443},
        ],
        "proxy-groups": [
            {"name": "Auto", "type": "fallback", "proxies": ["🇺🇸美国 01 | 1X"]},
        ],
        "rules": ["MATCH,Auto"],
    }


# ---------------------------------------------------------------------------
# Item 1: Atomic private writes & YAML cache poisoning
# ---------------------------------------------------------------------------


def test_atomic_write_permissions_and_failure_cleanup(tmp_path: Path, monkeypatch):
    target = tmp_path / "secret.yaml"
    atomic_write_text(target, "secret: s1\n", mode=0o600)
    assert target.read_text(encoding="utf-8") == "secret: s1\n"
    if os.name != "nt":
        assert (target.stat().st_mode & 0o777) == 0o600

    atomic_write_bytes(target, b"secret: s2\n", mode=0o600)
    assert target.read_bytes() == b"secret: s2\n"

    def _boom(_src, _dst):
        raise OSError("disk full")

    monkeypatch.setattr("cproxy.config.os.replace", _boom)
    with pytest.raises(OSError, match="disk full"):
        atomic_write_text(target, "secret: corrupt\n", mode=0o600)

    # 原文件内容不受损且无残留临时文件
    assert target.read_bytes() == b"secret: s2\n"
    leftovers = [p for p in tmp_path.iterdir() if p != target]
    assert leftovers == []


def test_load_yaml_file_deepcopy_prevents_cache_poisoning(tmp_path: Path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("mixed-port: 7890\nproxy-groups:\n  - name: G\n    proxies: [A]\n", encoding="utf-8")

    first = load_yaml_file(cfg)
    assert first["mixed-port"] == 7890
    first["mixed-port"] = 9999
    first["proxy-groups"][0]["proxies"].append("POISON")

    second = load_yaml_file(cfg)
    assert second["mixed-port"] == 7890
    assert second["proxy-groups"][0]["proxies"] == ["A"]


def test_render_runtime_and_snapshot_private_modes(tmp_path: Path):
    paths = _paths(tmp_path)
    config_file(paths).write_text(yaml.safe_dump(_minimal_render_cfg(), allow_unicode=True), encoding="utf-8")

    rt = RuntimeBackend(paths).render_runtime()
    if os.name != "nt":
        assert (rt.stat().st_mode & 0o777) == 0o600

    cfg2 = _minimal_render_cfg()
    cfg2["mixed-port"] = 7891
    config_file(paths).write_text(yaml.safe_dump(cfg2, allow_unicode=True), encoding="utf-8")
    RuntimeBackend(paths).render_runtime()

    snaps = list_snapshots(paths, "runtime")
    assert len(snaps) == 1
    if os.name != "nt":
        assert (snapshots_dir(paths).stat().st_mode & 0o777) == 0o700
        assert (snaps[0].stat().st_mode & 0o777) == 0o600

    restored = restore_snapshot(paths, snaps[0])
    if os.name != "nt":
        assert (restored.stat().st_mode & 0o777) == 0o600


# ---------------------------------------------------------------------------
# Item 2: ChinaMax.yml fresh install resilience
# ---------------------------------------------------------------------------


def test_render_runtime_ensures_chinamax_ruleset_present(tmp_path: Path, monkeypatch):
    paths = _paths(tmp_path / "with-fallback")
    config_file(paths).write_text(yaml.safe_dump(_minimal_render_cfg(), allow_unicode=True), encoding="utf-8")
    RuntimeBackend(paths).render_runtime()
    chinamax = paths.data_dir / "ruleset" / "ChinaMax.yml"
    assert chinamax.is_file()
    assert chinamax.stat().st_size > 0

    # 无离线副本时也必须生成最小合法 payload: []，避免 mihomo 启动失败
    paths_empty = _paths(tmp_path / "no-fallback")
    config_file(paths_empty).write_text(yaml.safe_dump(_minimal_render_cfg(), allow_unicode=True), encoding="utf-8")
    monkeypatch.setattr(runtime_mod, "_chinamax_fallback_paths", lambda: [tmp_path / "nonexistent.yml"])
    RuntimeBackend(paths_empty).render_runtime()
    seeded = paths_empty.data_dir / "ruleset" / "ChinaMax.yml"
    assert seeded.is_file()
    assert yaml.safe_load(seeded.read_text(encoding="utf-8")) == {"payload": []}


# ---------------------------------------------------------------------------
# Item 3: Linux H2-KEEPALIVE fallback groups + TCP + aicode.googleapis.com
# ---------------------------------------------------------------------------


def test_render_runtime_injects_keepalive_fallback_groups_and_tcp_rules(tmp_path: Path, monkeypatch):
    paths = _paths(tmp_path)
    cfg = _minimal_render_cfg()
    cfg["rules"] = [
        "AND,((DOMAIN,daily-cloudcode-pa.googleapis.com),(NOT,((IN-TYPE,SOCKS5)))),H2-KEEPALIVE",
        "MATCH,Auto",
    ]
    config_file(paths).write_text(yaml.safe_dump(cfg, allow_unicode=True), encoding="utf-8")
    monkeypatch.setattr(h2_mod, "is_h2_keepalive_running", lambda _state_dir: True)

    rt = RuntimeBackend(paths).render_runtime()
    rendered = yaml.safe_load(rt.read_text(encoding="utf-8"))
    groups = {g["name"]: g for g in rendered["proxy-groups"]}

    assert AI_GEMINI_KEEPALIVE_GROUP in groups
    assert groups[AI_GEMINI_KEEPALIVE_GROUP]["type"] == "fallback"
    assert groups[AI_GEMINI_KEEPALIVE_GROUP]["proxies"] == ["H2-KEEPALIVE", "AI-GEMINI"]
    assert groups[AI_GEMINI_KEEPALIVE_GROUP]["url"] == KEEPALIVE_TEST_URL
    assert groups[AI_GEMINI_KEEPALIVE_GROUP]["interval"] == 15
    assert groups[AI_GEMINI_KEEPALIVE_GROUP]["lazy"] is False

    assert AI_MANUAL_KEEPALIVE_GROUP in groups
    assert groups[AI_MANUAL_KEEPALIVE_GROUP]["type"] == "fallback"
    assert groups[AI_MANUAL_KEEPALIVE_GROUP]["proxies"] == ["H2-KEEPALIVE", "AI-MANUAL"]
    assert groups[AI_MANUAL_KEEPALIVE_GROUP]["url"] == KEEPALIVE_TEST_URL
    assert groups[AI_MANUAL_KEEPALIVE_GROUP]["interval"] == 15
    assert groups[AI_MANUAL_KEEPALIVE_GROUP]["lazy"] is False

    rules = rendered["rules"]
    gemini_ka = (
        "AND,((OR,((DOMAIN,daily-cloudcode-pa.googleapis.com),(DOMAIN,aicode.googleapis.com))),"
        "(NETWORK,TCP),(NOT,((IN-TYPE,SOCKS5)))),AI-GEMINI-KEEPALIVE"
    )
    manual_ka = (
        "AND,((OR,((DOMAIN,chatgpt.com),(DOMAIN,ws.chatgpt.com),(DOMAIN,api.openai.com))),(NETWORK,TCP),"
        "(OR,((IN-TYPE,HTTP),(IN-TYPE,TUN)))),AI-MANUAL-KEEPALIVE"
    )
    assert gemini_ka in rules
    assert manual_ka in rules
    assert rules.index(gemini_ka) < rules.index("DOMAIN-SUFFIX,googleapis.com,AI-GEMINI")
    assert "AND,((DOMAIN,daily-cloudcode-pa.googleapis.com),(NOT,((IN-TYPE,SOCKS5)))),H2-KEEPALIVE" not in rules


# ---------------------------------------------------------------------------
# Item 4: cproxy rule remove <index> & rule add validation
# ---------------------------------------------------------------------------


def test_rule_remove_strict_index_and_exact_before_substring(tmp_path: Path):
    paths = _paths(tmp_path)
    add_custom_rule(paths, "s1.openai.com", "ai")
    add_custom_rule(paths, "10.0.0.0/8", "direct")
    add_custom_rule(paths, "foo.com", "direct")
    # 顺序为最新在前：#1 foo.com, #2 10.0.0.0/8, #3 s1.openai.com
    removed = remove_custom_rule(paths, "1")
    assert [r.value for r in removed] == ["foo.com"]
    remaining = [r.value for r in load_custom_rules(paths)]
    assert remaining == ["10.0.0.0/8", "s1.openai.com"]

    add_custom_rule(paths, "beta.com", "direct")
    add_custom_rule(paths, "a.com", "direct")
    removed_exact = remove_custom_rule(paths, "a.com")
    assert [r.value for r in removed_exact] == ["a.com"]
    assert "beta.com" in [r.value for r in load_custom_rules(paths)]


def test_rule_add_validation_cidr_normalization_and_match_default(tmp_path: Path):
    paths = _paths(tmp_path)
    with pytest.raises(ValueError, match="不支持的规则类型"):
        add_custom_rule(paths, "example.com", "DIRECT", rule_type="INVALID-TYPE")
    with pytest.raises(ValueError):
        add_custom_rule(paths, "DOMAIN-SUFFIX,,DIRECT")
    with pytest.raises(ValueError):
        add_custom_rule(paths, "foo.com", "DIRECT\nMATCH,DIRECT")
    with pytest.raises(ValueError):
        add_custom_rule(paths, "foo.com", "AI,DIRECT")
    with pytest.raises(ValueError):
        add_custom_rule(paths, "foo.com,extra", "DIRECT")

    cidr_entry = add_custom_rule(paths, "IP-CIDR,10.0.0.1,DIRECT")
    assert cidr_entry.rule_type == "IP-CIDR"
    assert cidr_entry.value == "10.0.0.1/32"
    assert cidr_entry.format_mihomo_rule() == "IP-CIDR,10.0.0.1/32,DIRECT,no-resolve"

    default_entry = add_custom_rule(paths, "example.org", "proxy")
    assert default_entry.policy == "MATCH_DEFAULT"
    assert default_entry.format_mihomo_rule() == "DOMAIN-SUFFIX,example.org,MATCH_DEFAULT"
    assert default_entry.format_mihomo_rule(match_group="CyberGuard") == "DOMAIN-SUFFIX,example.org,CyberGuard"


# ---------------------------------------------------------------------------
# Item 5: refresh.py self-hosted proxies, strip keys, download & 4xx rollback
# ---------------------------------------------------------------------------


def test_preserve_multi_node_self_hosted_proxies_sharing_uuid():
    sub_uuid = "sub-uuid-1111"
    self_uuid = "self-uuid-9999"
    merged = {
        "proxies": [
            {"name": "🇯🇵日本 01", "type": "vless", "server": "jp1.sub.test", "port": 443, "uuid": sub_uuid},
            {"name": "🇺🇸美国 01", "type": "vless", "server": "us1.sub.test", "port": 443, "uuid": sub_uuid},
        ],
        "proxy-groups": [
            {"name": "Proxy", "type": "select", "proxies": ["🇯🇵日本 01", "🇺🇸美国 01"]},
        ],
        "rules": ["MATCH,Proxy"],
    }
    existing = {
        "proxies": [
            {"name": "🇯🇵日本 01", "type": "vless", "server": "jp1.sub.test", "port": 443, "uuid": sub_uuid},
            {"name": "🇰🇷旧订阅节点", "type": "vless", "server": "kr.sub.test", "port": 443, "uuid": sub_uuid},
            {"name": "自建-JP", "type": "vless", "server": "jp.my.test", "port": 443, "uuid": self_uuid},
            {"name": "自建-US", "type": "vless", "server": "us.my.test", "port": 443, "uuid": self_uuid},
        ],
        "proxy-groups": [
            {"name": "Proxy", "type": "select", "proxies": ["自建-JP", "自建-US", "🇯🇵日本 01", "🇰🇷旧订阅节点"]},
        ],
        "rules": ["MATCH,Proxy"],
    }

    refresh_mod._preserve_local_extra_proxies(merged, existing)
    names = [p["name"] for p in merged["proxies"]]
    assert "自建-JP" in names
    assert "自建-US" in names
    assert "🇰🇷旧订阅节点" not in names


def test_subscription_strip_keys_and_nodelist_dedupe_cleanup(tmp_path: Path, monkeypatch):
    paths = _paths(tmp_path)
    config_file(paths).write_text("mode: rule\n", encoding="utf-8")
    evil_payload = yaml.safe_dump(
        {
            "notify-webhook": "https://evil.example/hook",
            "notify-desktop": True,
            "subscriptions": [{"name": "Evil", "url": "https://evil.example/sub"}],
            "ai-chatgpt-url": "https://evil.example/chatgpt",
            "connectivity-test-urls": ["https://evil.example/probe"],
            "ip-check-urls": ["https://evil.example/ip"],
            "external-controller-pipe": r"\\.\pipe\evil",
            "external-ui": "/tmp/ui",
            "external-ui-url": "https://evil.example/ui.zip",
            "geox-url": {"mmdb": "https://evil.example/mmdb"},
            "authentication": ["admin:pass"],
            "tunnels": ["tcp/127.0.0.1:1000/1.2.3.4:80/DIRECT"],
            "listeners": [{"name": "evil", "type": "mixed", "port": 9999}],
            "proxies": [{"name": "US-1", "type": "ss", "server": "1.1.1.1", "port": 443}],
            "proxy-groups": [{"name": "Proxy", "type": "select", "proxies": ["US-1"]}],
            "rules": ["MATCH,Proxy"],
        },
        allow_unicode=True,
    ).encode("utf-8")
    monkeypatch.setattr(refresh_mod, "_download_subscription", lambda *_a, **_kw: (evil_payload, None))
    refresh_mod.update_source_from_subscription(paths, "https://sub.example/full")
    merged = read_config(paths)
    for stripped in (
        "notify-webhook",
        "notify-desktop",
        "subscriptions",
        "ai-chatgpt-url",
        "connectivity-test-urls",
        "ip-check-urls",
        "external-controller-pipe",
        "external-ui",
        "external-ui-url",
        "geox-url",
        "authentication",
        "tunnels",
        "listeners",
    ):
        assert stripped not in merged
    if os.name != "nt":
        assert (config_file(paths).stat().st_mode & 0o777) == 0o600

    # nodelist 分支也清理历史 #2 / #2_ 僵尸副本并保留多节点自建代理
    config_file(paths).write_text(
        yaml.safe_dump(
            {
                "proxies": [
                    {"name": "JP 01", "type": "ss", "server": "1.1.1.1", "port": 1, "uuid": "sub-u"},
                    {"name": "JP 01 #2", "type": "ss", "server": "1.1.1.2", "port": 1, "uuid": "unique-a"},
                    {"name": "JP 01 #2_", "type": "ss", "server": "1.1.1.2", "port": 1, "uuid": "unique-b"},
                    {"name": "自建-1", "type": "vless", "server": "s1.my", "port": 443, "uuid": "my-self"},
                    {"name": "自建-2", "type": "vless", "server": "s2.my", "port": 443, "uuid": "my-self"},
                ],
                "proxy-groups": [{"name": "Proxy", "type": "select", "proxies": ["JP 01", "JP 01 #2", "自建-1"]}],
                "rules": ["MATCH,Proxy"],
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    nodelist_raw = yaml.safe_dump(
        {
            "proxies": [
                {"name": "JP 01", "type": "ss", "server": "1.1.1.1", "port": 1, "uuid": "sub-u"},
                {"name": "JP 01", "type": "ss", "server": "1.1.1.2", "port": 1, "uuid": "sub-u"},
            ]
        },
        allow_unicode=True,
    ).encode("utf-8")
    monkeypatch.setattr(refresh_mod, "_download_subscription", lambda *_a, **_kw: (nodelist_raw, None))
    refresh_mod.update_source_from_subscription(paths, "https://sub.example/nodes")
    after = read_config(paths)
    after_names = [p["name"] for p in after["proxies"]]
    assert "JP 01 #2_" not in after_names
    assert after_names.count("JP 01 #2") == 1
    assert "自建-1" in after_names and "自建-2" in after_names


def test_download_scheme_validation_502_fallback_and_extra_sub_max_bytes(tmp_path: Path, monkeypatch):
    paths = _paths(tmp_path)
    config_file(paths).write_text("mixed-port: 7890\n", encoding="utf-8")
    monkeypatch.setattr(refresh_mod, "SUBSCRIPTION_RETRY_INTERVAL", 0)

    with pytest.raises(ValueError, match="http/https"):
        refresh_mod._download_subscription(paths, "file:///etc/passwd", timeout=5)

    # 代理返回 502 时应回退直连
    calls = {"proxied": 0, "direct": 0}

    def _fake_opener(*handlers):
        from urllib.request import ProxyHandler

        h = next((x for x in handlers if isinstance(x, ProxyHandler)), None)
        is_proxied = bool(h and h.proxies)

        class _Resp:
            headers: dict = {}

            def read(self, n=-1):
                return b"direct-ok"

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        class _Opener:
            def open(self, req, timeout=None):
                if is_proxied:
                    calls["proxied"] += 1
                    raise HTTPError(req.full_url, 502, "Bad Gateway", hdrs=None, fp=BytesIO(b"bad gw"))
                calls["direct"] += 1
                return _Resp()

        return _Opener()

    monkeypatch.setattr(refresh_mod, "build_opener", _fake_opener)
    body, _ = refresh_mod._download_subscription(paths, "http://sub.example.com/list", timeout=5)
    assert body == b"direct-ok"
    assert calls == {"proxied": 1, "direct": 1}

    # apply_extra_subscriptions 超过 SUBSCRIPTION_MAX_BYTES 报错
    cfg = _minimal_render_cfg()
    cfg["subscriptions"] = [{"name": "BigSub", "url": "https://sub.example.com/big"}]
    config_file(paths).write_text(yaml.safe_dump(cfg, allow_unicode=True), encoding="utf-8")
    monkeypatch.setattr(
        refresh_mod,
        "_download_subscription",
        lambda *_a, **_kw: (b"x" * (refresh_mod.SUBSCRIPTION_MAX_BYTES + 1), None),
    )
    res = refresh_mod.apply_extra_subscriptions(paths)
    assert len(res) == 1
    assert res[0].status == "失败"
    assert "超过大小限制" in res[0].detail


def test_refresh_4xx_reload_rejection_rolls_back_without_restarting_mihomo(tmp_path: Path, monkeypatch):
    paths = _paths(tmp_path)
    config_file(paths).write_text(yaml.safe_dump(_minimal_render_cfg(), allow_unicode=True), encoding="utf-8")
    rt = runtime_file(paths)
    rt.write_text("marker: good-runtime\n", encoding="utf-8")
    snapshot_file(paths, rt, "runtime")

    restarts: list[int] = []
    svc = refresh_mod.RefreshService(paths)
    monkeypatch.setattr(svc.process, "is_running", lambda: True)
    monkeypatch.setattr(svc.process, "restart", lambda: restarts.append(1) or 99)

    def _fake_render(self):
        rt.write_text("marker: bad-runtime\n", encoding="utf-8")
        return rt

    monkeypatch.setattr(refresh_mod.RuntimeBackend, "render_runtime", _fake_render)

    class _Reject400API:
        def reload_config(self, _path):
            raise APIRequestError("invalid config", status=400, body="invalid rule")

    monkeypatch.setattr(svc, "_api_factory", lambda _p: _Reject400API())

    with pytest.raises(RuntimeError, match="HTTP 400.*保留运行中代理"):
        svc.refresh()

    assert restarts == []
    assert rt.read_text(encoding="utf-8") == "marker: good-runtime\n"


def test_refresh_extra_subscription_uses_shared_rollback(tmp_path: Path, monkeypatch):
    paths = _paths(tmp_path)
    config_file(paths).write_text(yaml.safe_dump(_minimal_render_cfg(), allow_unicode=True), encoding="utf-8")
    rt = runtime_file(paths)
    rt.write_text("marker: prev-good\n", encoding="utf-8")
    snapshot_file(paths, rt, "runtime")

    svc = refresh_mod.RefreshService(paths)
    monkeypatch.setattr(
        refresh_mod,
        "apply_extra_subscriptions",
        lambda _p: [refresh_mod.ExtraSubscriptionResult("Extra", "已更新", "1 个节点")],
    )
    monkeypatch.setattr(svc.process, "is_running", lambda: True)
    restarts: list[int] = []
    monkeypatch.setattr(svc.process, "restart", lambda: restarts.append(1) or 99)

    def _fake_render(self):
        rt.write_text("marker: broken-extra\n", encoding="utf-8")
        return rt

    monkeypatch.setattr(refresh_mod.RuntimeBackend, "render_runtime", _fake_render)

    class _Reject400API:
        def reload_config(self, _path):
            raise APIRequestError("bad extra", status=400, body="dup proxy")

    monkeypatch.setattr(svc, "_api_factory", lambda _p: _Reject400API())
    with pytest.raises(RuntimeError, match="HTTP 400"):
        svc.refresh_extra_subscription("Extra", "https://example.com/extra")
    assert restarts == []
    assert rt.read_text(encoding="utf-8") == "marker: prev-good\n"


# ---------------------------------------------------------------------------
# Item 6: runtime.py hardening
# ---------------------------------------------------------------------------


def test_refresh_ws_cdn_ips_does_not_mark_stale_runtime_applied_when_busy(tmp_path: Path, monkeypatch):
    paths = _paths(tmp_path)
    config_file(paths).write_text(yaml.safe_dump(_minimal_render_cfg(), allow_unicode=True), encoding="utf-8")
    RuntimeBackend(paths).render_runtime()

    marked: list[Path] = []
    monkeypatch.setattr("cproxy.backend.process.ProcessBackend.is_runtime_stale", lambda self, p=None: True)
    monkeypatch.setattr(
        "cproxy.backend.process.ProcessBackend.mark_runtime_applied",
        lambda self, p=None: marked.append(p) or True,
    )
    monkeypatch.setattr("cproxy.services.ai_watch.has_any_inflight_ai_connection", lambda _p: True)

    changed = refresh_ws_cdn_ips_if_changed(paths)
    assert changed is False
    assert marked == []


def test_reshape_match_country_groups_collisions_and_final_rule(tmp_path: Path):
    paths = _paths(tmp_path)
    cfg = {
        "mixed-port": 7890,
        "proxies": [
            {"name": "🇭🇰香港 01", "type": "ss", "server": "1.1.1.1", "port": 443},
            {"name": "🇺🇸美国 01", "type": "ss", "server": "1.1.1.2", "port": 443},
            {"name": "🇸🇬新加坡 01", "type": "ss", "server": "1.1.1.3", "port": 443},
        ],
        "proxy-groups": [
            {
                "name": "香港",
                "type": "url-test",
                "url": "https://cp.cloudflare.com/generate_204",
                "interval": 300,
                "proxies": ["🇭🇰香港 01"],
            },
            {"name": "Main", "type": "select", "proxies": ["香港", "🇺🇸美国 01", "🇸🇬新加坡 01"]},
        ],
        "rules": ["final,Main"],
    }
    config_file(paths).write_text(yaml.safe_dump(cfg, allow_unicode=True), encoding="utf-8")
    rt = RuntimeBackend(paths).render_runtime()
    rendered = yaml.safe_load(rt.read_text(encoding="utf-8"))
    groups = {g["name"]: g for g in rendered["proxy-groups"]}
    # 原有 url-test "香港" 组保持类型不被覆盖，且保留在 Main 中
    assert groups["香港"]["type"] == "url-test"
    assert "香港" in groups["Main"]["proxies"]
    assert len(groups["Main"]["proxies"]) > 0
    # FINAL 大小写不敏感，ChinaMax / GEOIP 插在 final,Main 之前
    rules = rendered["rules"]
    assert rules.index("RULE-SET,ChinaMax,DIRECT") < rules.index("final,Main")
    assert rules.index("GEOIP,CN,DIRECT,no-resolve") < rules.index("final,Main")


def test_ws_cdn_ip_boundary_and_openai_hosts_narrowed_and_non_dict_config(tmp_path: Path):
    assert "+.openai.com" not in OPENAI_DIRECT_HOSTS
    assert "api.openai.com" in OPENAI_DIRECT_HOSTS

    paths = _paths(tmp_path)
    runtime_file(paths).write_text(
        yaml.safe_dump({"proxies": [{"name": "n1", "server": "104.21.8.12", "port": 443}]}),
        encoding="utf-8",
    )
    _, _, active = _ws_cdn_ip_log_penalties(("104.21.8.1", "104.21.8.12"), 443, paths=paths)
    assert active == {"104.21.8.12"}

    config_file(paths).write_text("- not-a-dict\n", encoding="utf-8")
    with pytest.raises(ValueError, match="YAML 映射"):
        RuntimeBackend(paths).render_runtime()


# ---------------------------------------------------------------------------
# Item 7: api.py, verge.py & clash-verge-script.js
# ---------------------------------------------------------------------------


def test_api_controller_normalization_credential_guard_and_opener_cache(tmp_path: Path, monkeypatch):
    assert normalize_controller_url(":9097") == "http://127.0.0.1:9097"
    assert normalize_controller_url("0.0.0.0:9097/") == "http://127.0.0.1:9097"
    assert normalize_controller_url("[::]:9443", default_scheme="https") == "https://127.0.0.1:9443"
    assert normalize_controller_endpoint("0.0.0.0:9097/") == "127.0.0.1:9097"

    paths = _paths(tmp_path)
    config_file(paths).write_text(
        "external-controller: 0.0.0.0:9097/\nsecret-systemd-credential: ../escape\n",
        encoding="utf-8",
    )
    api = APIBackend(paths)
    assert api.controller_url() == "http://127.0.0.1:9097"
    with pytest.raises(APIUnavailableError, match="非法路径"):
        api.api_secret()

    # HTTP 400 抛出 APIRequestError（仍是 APIUnavailableError 子类）
    config_file(paths).write_text("external-controller: 127.0.0.1:9097\n", encoding="utf-8")

    class _ErrOpener:
        def open(self, req, timeout=None):
            raise HTTPError(req.full_url, 400, "Bad Request", hdrs=None, fp=BytesIO(b'{"message":"bad yaml"}'))

    created_keys: list[tuple[str, str | None]] = []

    def _fake_build_opener(*_handlers):
        created_keys.append(("built", None))
        return _ErrOpener()

    monkeypatch.setattr("cproxy.backend.api.build_opener", _fake_build_opener)
    with pytest.raises(APIRequestError) as exc_info:
        api.request("PUT", "/configs?force=true", {"path": "/tmp/x"})
    assert isinstance(exc_info.value, APIUnavailableError)
    assert exc_info.value.status == 400
    assert "bad yaml" in exc_info.value.body

    # 不同 (scheme, hostname) 使用各自缓存的 opener
    op_http = api._http_opener("http://127.0.0.1:9090/version")
    op_https = api._http_opener("https://127.0.0.1:9443/version")
    assert op_http is not op_https


def test_verge_run_once_preserves_pid_catches_system_exit_and_corrupt_yaml(tmp_path: Path, monkeypatch):
    verge_dir = tmp_path / "verge"
    verge_dir.mkdir()
    (verge_dir / "clash-verge.yaml").write_text(":::invalid: [yaml\n", encoding="utf-8")
    (verge_dir / "verge.yaml").write_text(
        "clash_core_external_controller: 0.0.0.0:9097/\nverge_mixed_port: 7897\n",
        encoding="utf-8",
    )
    detected = verge_mod.discover_clash_verge_config(verge_dir)
    assert detected.controller == "127.0.0.1:9097"

    paths = _paths(tmp_path)
    verge_mod.init_verge_config(paths, verge_dir=verge_dir)
    if os.name != "nt":
        assert (config_file(paths).stat().st_mode & 0o777) == 0o600

    # 先写入一个正在运行的 daemon PID，验证 run_once 不覆盖它
    verge_mod._save_daemon_state(paths, {"pid": 424242, "updated_at": time.time()})
    monkeypatch.setattr("cproxy.services.verge.sample_connections_activity", lambda *_a, **_kw: {})
    monkeypatch.setattr(
        "cproxy.services.verge.AIWatchService.watch",
        lambda *_a, **_kw: (_ for _ in ()).throw(SystemExit("boom")),
    )

    tick = verge_mod.VergeDaemonService(paths).run_once(run_rebalance=False)
    assert any("ai-watch" in w for w in tick.warnings)
    state = verge_mod._load_daemon_state(paths)
    assert state["pid"] == 424242


def test_verge_daemon_keepalive_respawn_backoff_records_error(tmp_path: Path, monkeypatch):
    paths = _paths(tmp_path)
    verge_mod.init_verge_config(paths, controller="127.0.0.1:9097", secret="s", mixed_port=7897)
    monkeypatch.setattr("cproxy.services.verge.sample_connections_activity", lambda *_a, **_kw: {})
    monkeypatch.setattr(
        "cproxy.services.verge.AIWatchService.watch",
        lambda *_a, **_kw: SimpleNamespace(results=[], reaped_connections=[]),
    )

    spawn_count = 0

    def _failing_start(self, *, on_error=None):
        nonlocal spawn_count
        spawn_count += 1
        if on_error is not None:
            on_error("port 7895 in use")
        t = threading.Thread(target=lambda: None)
        t.start()
        t.join()
        return t

    monkeypatch.setattr(verge_mod.VergeDaemonService, "_start_embedded_keepalive_thread", _failing_start)
    svc = verge_mod.VergeDaemonService(paths)
    svc.run_daemon(max_ticks=4, enable_keepalive=True, sleep_fn=lambda _s: None)
    # 有退避机制：连续 4 个 tick 内不会每 tick 都重建线程
    assert spawn_count == 1
    state = verge_mod._load_daemon_state(paths)
    assert "port 7895 in use" in str(state.get("last_keepalive_error") or "")


def test_verge_hot_reload_resolves_custom_global_script_and_dedupes_proxies(tmp_path: Path, monkeypatch):
    if not shutil.which("node"):
        pytest.skip("node not installed")

    paths = _paths(tmp_path)
    verge_dir = tmp_path / "verge-custom"
    (verge_dir / "profiles").mkdir(parents=True)
    (verge_dir / "profiles.yaml").write_text(
        yaml.safe_dump(
            {
                "current": "Sub1",
                "chain": ["MyGlobal"],
                "items": [
                    {"uid": "MyGlobal", "type": "script", "file": "CustomGlobal.js"},
                    {"uid": "Sub1", "type": "remote", "name": "Sub1", "option": {"script": "LocalSc"}},
                    {"uid": "LocalSc", "type": "script", "file": "local.js"},
                ],
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    verge_mod.inject_verge_global_script(verge_dir)
    assert (verge_dir / "profiles" / "CustomGlobal.js").is_file()
    (verge_dir / "profiles" / "local.js").write_text(
        "function main(cfg) {\n"
        "  cfg.proxies = cfg.proxies || [];\n"
        "  cfg.proxies.push({name: 'LOCAL-DUP', type: 'socks5', server: '127.0.0.1', port: 7893});\n"
        "  cfg.rules = ['PROCESS-NAME,python.exe,DIRECT', ...(cfg.rules || [])];\n"
        "  return cfg;\n"
        "}\n",
        encoding="utf-8",
    )
    (verge_dir / "clash-verge.yaml").write_text(
        yaml.safe_dump(
            {
                "mixed-port": 7897,
                "proxies": [
                    {"name": "🇺🇸 US 01", "type": "ss", "server": "1.1.1.1", "port": 443},
                    {"name": "LOCAL-DUP", "type": "socks5", "server": "127.0.0.1", "port": 7893},
                ],
                "proxy-groups": [{"name": "Proxy", "type": "select", "proxies": ["🇺🇸 US 01"]}],
                "rules": ["MATCH,Proxy"],
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )

    captured: dict = {}
    monkeypatch.setattr(
        APIBackend,
        "get_groups",
        lambda self: {"AI-MANUAL": ProxyGroup("AI-MANUAL", "Fallback", "🇺🇸 US 01", ["🇺🇸 US 01"])},
    )
    monkeypatch.setattr(APIBackend, "selector_now", lambda self: {})
    monkeypatch.setattr(
        APIBackend,
        "request",
        lambda self, method, path, payload=None, **kw: captured.setdefault("payload", payload) or {},
    )
    monkeypatch.setattr(APIBackend, "delay_test", lambda self, *a, **kw: {"delay": 1})

    assert verge_mod.try_hot_reload_verge_runtime(paths, verge_dir) is True
    loaded = yaml.safe_load(captured["payload"]["payload"])
    # 全局 AI 规则排在局部脚本规则之前（不再被局部脚本抢占）
    assert loaded["rules"][0] == "DOMAIN-SUFFIX,google.com,AI-GEMINI"
    assert "PROCESS-NAME,python.exe,DIRECT" in loaded["rules"]
    proxy_names = [p["name"] for p in loaded["proxies"]]
    assert len(proxy_names) == len(set(proxy_names))


def test_clash_verge_script_js_regions_multipliers_empty_guards_and_stale_rules(tmp_path: Path):
    script_repo = ROOT_DIR / "scripts" / "clash-verge-script.js"
    script_pkg = ROOT_DIR / "src" / "cproxy" / "data" / "verge" / "clash-verge-script.js"
    assert script_repo.read_bytes() == script_pkg.read_bytes()

    if not shutil.which("node"):
        pytest.skip("node not installed")

    harness = tmp_path / "verify_js.js"
    harness.write_text(
        script_repo.read_text(encoding="utf-8")
        + "\n"
        + """
const assert = require('assert');
assert.strictEqual(matchRegion('Australia 01'), 'AU');
assert.strictEqual(matchRegion('Node 01'), 'OTHER');
assert.strictEqual(matchRegion('Sweden 01'), 'OTHER');
assert.strictEqual(matchRegion('Free 01'), 'OTHER');
assert.strictEqual(matchRegion('Network 01'), 'OTHER');
assert.strictEqual(matchRegion('Auto'), 'OTHER');
assert.strictEqual(matchRegion('Israel 01'), 'OTHER');
assert.strictEqual(matchRegion('🇨🇳 中国大陆 01'), 'OTHER');
assert.strictEqual(matchRegion('🇨🇳 Taiwan 05'), 'TW');
assert.strictEqual(parseMultiplier('🇺🇸 LAX05'), 1.0);
assert.strictEqual(parseMultiplier('🇺🇸 Phoenix 04'), 1.0);
assert.strictEqual(parseMultiplier('Max 5'), 1.0);
assert.strictEqual(parseMultiplier('🇺🇸 US 01 | 5X'), 5.0);

// 空有效节点直接原样返回，不产出空 proxies 组
const emptyOut = main({
  proxies: [{ name: '剩余流量: 10GB', type: 'ss' }],
  'proxy-groups': [{ name: 'P', type: 'select', proxies: ['剩余流量: 10GB'] }],
  rules: ['MATCH,P'],
});
assert.strictEqual(emptyOut['proxy-groups'].length, 1);

// 清理旧版 H2-KEEPALIVE 规则且面板过滤不会把组清空
const out = main({
  proxies: [{ name: '🇺🇸 LAX05', type: 'ss', server: '1.1.1.1', port: 443 }],
  'proxy-groups': [{ name: 'PanelOnly', type: 'select', proxies: ['剩余流量: 10GB'] }],
  rules: [
    'AND,((DOMAIN,daily-cloudcode-pa.googleapis.com),(NOT,((IN-TYPE,SOCKS5)))),H2-KEEPALIVE',
    'MATCH,PanelOnly',
  ],
});
const panelGrp = out['proxy-groups'].find(g => g.name === 'PanelOnly');
assert.deepStrictEqual(panelGrp.proxies, ['剩余流量: 10GB']);
assert.ok(!out.rules.includes('AND,((DOMAIN,daily-cloudcode-pa.googleapis.com),(NOT,((IN-TYPE,SOCKS5)))),H2-KEEPALIVE'));
console.log('OK');
""",
        encoding="utf-8",
    )
    res = subprocess.run(["node", str(harness)], capture_output=True, text=True, check=True, timeout=5)
    assert res.stdout.strip() == "OK"


# ---------------------------------------------------------------------------
# Item 8: process.py, traffic.py, snapshots.py, geodata.py, runtime_metrics.py
# ---------------------------------------------------------------------------


def test_process_parse_listen_address_and_atomic_lock(tmp_path: Path):
    assert ProcessBackend._parse_listen_address("http://127.0.0.1:9090") == ("127.0.0.1", 9090)
    assert ProcessBackend._parse_listen_address("https://0.0.0.0:9443/") == ("127.0.0.1", 9443)
    assert ProcessBackend._parse_listen_address("[::]:9097") == ("127.0.0.1", 9097)
    assert ProcessBackend._parse_listen_address(":9090") == ("127.0.0.1", 9090)

    paths = _paths(tmp_path)
    backend = ProcessBackend(paths)
    assert backend._acquire_lock() is True
    assert backend._acquire_lock() is False
    if os.name != "nt":
        assert (backend._lock_path().stat().st_mode & 0o777) == 0o600
    backend._release_lock()
    assert backend._acquire_lock() is True
    backend._release_lock()


def test_traffic_case_sensitive_direct_glob_wal_and_batched_delete(tmp_path: Path):
    paths = _paths(tmp_path)
    svc = TrafficService(paths)

    class _BatchAPI:
        def __init__(self):
            self.step = 0

        def get_connections(self):
            self.step += 1
            if self.step == 1:
                conns = [
                    {
                        "id": f"c-{i}",
                        "metadata": {"host": "h.example", "process": "curl"},
                        "rule": "Match",
                        "chains": ["Direct", "Proxy"] if i == 0 else ["DIRECT"],
                        "download": 100,
                        "upload": 10,
                    }
                    for i in range(620)
                ]
                return {"connections": conns}
            return {"connections": []}

    svc.api = _BatchAPI()
    svc.collect()
    svc.collect()  # 批量删除 620 条已关闭连接（超过单批 500）

    audit = svc.audit(days=1)
    # "Proxy -> Direct"（小写）不得被误判为直连，必须计入 proxy_download
    assert audit["proxy_download"] == 100
    assert audit["direct_download"] == 619 * 100

    with sqlite3.connect(paths.state_dir / "traffic.db") as db:
        mode = db.execute("PRAGMA journal_mode").fetchone()[0]
        assert str(mode).lower() == "wal"


def test_snapshots_iso_year_week_boundary(tmp_path: Path):
    # 2024-12-30 是 ISO 2025-W01；2025-01-01 也是 ISO 2025-W01，两者必须落在同一 ISO 周桶，
    # 而 2024-01-01（ISO 2024-W01）与 2024-12-30（ISO 2025-W01）不能误碰撞。
    s_dir = tmp_path / "snaps"
    s_dir.mkdir()
    now = datetime(2025, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
    files: list[Path] = []
    # 先放 10 个近期保护层快照
    for i in range(10):
        p = s_dir / f"runtime-20250115T11{i:02d}00.000000Z.yaml"
        p.write_text(f"v: {i}\n", encoding="utf-8")
        files.append(p)
    # 再放 2025-01-01 和 2024-12-30（均属于 ISO 2025-W01，距 now 为 14~16 天）
    p_jan1 = s_dir / "runtime-20250101T120000.000000Z.yaml"
    p_dec30 = s_dir / "runtime-20241230T120000.000000Z.yaml"
    p_jan1.write_text("v: jan1\n", encoding="utf-8")
    p_dec30.write_text("v: dec30\n", encoding="utf-8")
    files.extend([p_jan1, p_dec30])
    files.sort(reverse=True)

    kept, pruned = _evaluate_tier_retention(files, now=now)
    assert p_jan1 in kept
    assert p_dec30 in pruned


def test_geodata_rejects_zero_byte_mmdb(tmp_path: Path):
    paths = _paths(tmp_path)
    (paths.data_dir / "country.mmdb").write_bytes(b"")
    res = check_country_mmdb(paths)
    assert res.ok is False


def test_runtime_metrics_guards_clk_tck_and_missing_paren(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(metrics_mod.os, "sysconf", lambda _name: 0)
    assert metrics_mod._clk_tck() == metrics_mod._DEFAULT_CLK_TCK

    monkeypatch.setattr(metrics_mod, "_clk_tck", lambda: 0)
    orig_read_text = Path.read_text

    def _fake_read_text(self, *args, **kwargs):
        if str(self).endswith("/stat"):
            return "123 malformed no paren 0 0 0"
        if str(self) == "/proc/uptime":
            return "1000.0 500.0\n"
        return orig_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", _fake_read_text)
    assert metrics_mod._read_uptime_seconds(123) is None
