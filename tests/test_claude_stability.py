"""Claude Code 独立出口（AI-CLAUDE）、语义看门狗、重排与 H2 Keepalive 稳定性测试。"""
from __future__ import annotations

import io
import json
import shutil
import subprocess
import time
from email.message import Message
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError

import pytest
import yaml

from cproxy.backend.models import ProxyGroup
from cproxy.backend.runtime import (
    AI_CLAUDE_GROUP,
    AI_CLAUDE_KEEPALIVE_GROUP,
    RuntimeBackend,
    resolve_ai_route,
)
from cproxy.config import AppPaths, ai_claude_order_file, config_file
from cproxy.names import resolve_group_alias
from cproxy.services import ai_watch as ai_watch_mod
from cproxy.services import diagnostics as diag_mod
from cproxy.services import h2_keepalive as h2_mod
from cproxy.services import probe as probe_mod
from cproxy.services import rebalance as rebalance_mod
from cproxy.services.query import QueryService
from cproxy.services.verge import check_verge_effectiveness, try_hot_reload_verge_runtime

ROOT_DIR = Path(__file__).resolve().parents[1]


def _make_paths(tmp_path: Path) -> AppPaths:
    paths = AppPaths(
        config_dir=tmp_path / "cfg",
        data_dir=tmp_path / "data",
        state_dir=tmp_path / "state",
    )
    paths.config_dir.mkdir(parents=True, exist_ok=True)
    paths.data_dir.mkdir(parents=True, exist_ok=True)
    paths.state_dir.mkdir(parents=True, exist_ok=True)
    return paths


def _anytls_node(name: str, server: str, *, timeout: int = 30, check: int = 30) -> dict:
    return {
        "name": name,
        "type": "anytls",
        "server": server,
        "port": 443,
        "idle-session-timeout": timeout,
        "idle-session-check-interval": check,
    }


def test_runtime_renders_ai_claude_group_rules_anytls_and_keepalive(tmp_path: Path, monkeypatch):
    paths = _make_paths(tmp_path)
    src_cfg = {
        "mixed-port": 7890,
        "ai-claude-prefer": ["🇯🇵 Japan 02"],
        "proxies": [
            _anytls_node("🇭🇰 Hong Kong 01", "hk1.example.com"),
            _anytls_node("🇺🇸 United States 01 | IEPL", "us1.example.com"),
            _anytls_node("🇺🇸 United States 02", "us2.example.com"),
            _anytls_node("🇯🇵 Japan 02", "jp2.example.com"),
            _anytls_node("🇸🇬 Singapore 01", "sg1.example.com"),
            _anytls_node("🇨🇳 Taiwan 01", "tw1.example.com", timeout=180, check=60),
        ],
        "proxy-groups": [
            {"name": "PROXY", "type": "select", "proxies": ["🇭🇰 Hong Kong 01", "🇺🇸 United States 02"]},
        ],
        "rules": [
            "DOMAIN-SUFFIX,claude.com,PROXY",
            "DOMAIN-SUFFIX,anthropic.com,PROXY",
            "MATCH,PROXY",
        ],
    }
    config_file(paths).write_text(yaml.safe_dump(src_cfg, allow_unicode=True), encoding="utf-8")
    monkeypatch.setattr("cproxy.services.h2_keepalive.is_h2_keepalive_running", lambda _sd: True)
    probe_mod.record_claude_blocked_node(paths, "🇺🇸 United States 02")

    out_path = RuntimeBackend(paths).render_runtime()
    data = yaml.safe_load(out_path.read_text(encoding="utf-8"))

    # anytls 短空闲会话超时应自动放宽至 >= 120s / 检查间隔 >= 60s
    proxies_by_name = {p["name"]: p for p in data["proxies"]}
    assert proxies_by_name["🇯🇵 Japan 02"]["idle-session-timeout"] == 120
    assert proxies_by_name["🇯🇵 Japan 02"]["idle-session-check-interval"] == 60
    assert proxies_by_name["🇨🇳 Taiwan 01"]["idle-session-timeout"] == 180

    groups = {g["name"]: g for g in data["proxy-groups"]}
    assert AI_CLAUDE_GROUP in groups
    assert AI_CLAUDE_KEEPALIVE_GROUP in groups
    claude_members = groups[AI_CLAUDE_GROUP]["proxies"]
    assert "🇭🇰 Hong Kong 01" not in claude_members
    assert claude_members[0] == "🇯🇵 Japan 02"
    # 被隔离节点应沉底到列表末尾
    assert claude_members[-1] == "🇺🇸 United States 02"
    assert groups[AI_CLAUDE_KEEPALIVE_GROUP]["proxies"] == ["H2-KEEPALIVE", AI_CLAUDE_GROUP]

    rules = data["rules"]
    assert "DOMAIN-SUFFIX,claude.com,PROXY" not in rules
    assert "DOMAIN-SUFFIX,anthropic.com,PROXY" not in rules
    assert "DOMAIN-SUFFIX,anthropic.com,AI-CLAUDE" in rules
    assert "DOMAIN-SUFFIX,claude.ai,AI-CLAUDE" in rules
    assert "DOMAIN-SUFFIX,claude.com,AI-CLAUDE" in rules
    assert "DOMAIN-SUFFIX,claudeusercontent.com,AI-CLAUDE" in rules
    ka_rule = "AND,((DOMAIN,api.anthropic.com),(NETWORK,TCP),(OR,((IN-TYPE,HTTP),(IN-TYPE,TUN)))),AI-CLAUDE-KEEPALIVE"
    assert ka_rule in rules
    assert rules.index(ka_rule) < rules.index("DOMAIN-SUFFIX,anthropic.com,AI-CLAUDE")


def test_clash_verge_script_synced_and_builds_ai_claude_and_anytls(tmp_path: Path):
    script_repo = ROOT_DIR / "scripts" / "clash-verge-script.js"
    script_pkg = ROOT_DIR / "src" / "cproxy" / "data" / "verge" / "clash-verge-script.js"
    assert script_repo.read_bytes() == script_pkg.read_bytes()

    node_bin = shutil.which("node")
    if not node_bin:
        pytest.skip("node not available")

    sample_cfg = {
        "proxies": [
            _anytls_node("🇭🇰 Hong Kong 01", "hk.example.com"),
            _anytls_node("🇺🇸 United States 01", "us1.example.com"),
            _anytls_node("🇯🇵 Japan 01 | IEPL", "jp1.example.com"),
            _anytls_node("🇹🇼 Taiwan 01", "tw1.example.com"),
        ],
        "proxy-groups": [{"name": "Proxy", "type": "select", "proxies": ["🇭🇰 Hong Kong 01"]}],
        "rules": [
            "DOMAIN-SUFFIX,claude.com,Proxy",
            "DOMAIN-SUFFIX,anthropic.com,Proxy",
            "MATCH,Proxy",
        ],
    }
    in_json = tmp_path / "in.json"
    out_json = tmp_path / "out.json"
    runner = tmp_path / "run.js"
    in_json.write_text(json.dumps(sample_cfg), encoding="utf-8")
    runner.write_text(
        "const fs = require('fs');\n"
        "const vm = require('vm');\n"
        f"const cfg = JSON.parse(fs.readFileSync({json.dumps(str(in_json))}, 'utf8'));\n"
        f"const code = fs.readFileSync({json.dumps(str(script_repo))}, 'utf8') + '\\n;globalThis.__main = main;';\n"
        "const ctx = { console, globalThis: {} }; ctx.globalThis = ctx;\n"
        "vm.createContext(ctx); vm.runInContext(code, ctx);\n"
        "const out = ctx.__main(cfg);\n"
        f"fs.writeFileSync({json.dumps(str(out_json))}, JSON.stringify(out));\n",
        encoding="utf-8",
    )
    subprocess.run([node_bin, str(runner)], check=True, capture_output=True, timeout=10.0)
    out = json.loads(out_json.read_text(encoding="utf-8"))

    proxies_map = {p["name"]: p for p in out["proxies"]}
    assert proxies_map["🇺🇸 United States 01"]["idle-session-timeout"] == 120
    assert proxies_map["🇺🇸 United States 01"]["idle-session-check-interval"] == 60

    groups = {g["name"]: g for g in out["proxy-groups"]}
    assert "AI-CLAUDE" in groups
    assert "AI-CLAUDE-KEEPALIVE" in groups
    assert "🇭🇰 Hong Kong 01" not in groups["AI-CLAUDE"]["proxies"]
    assert groups["AI-CLAUDE"]["proxies"][0] == "🇺🇸 United States 01"
    assert groups["AI-CLAUDE-KEEPALIVE"]["proxies"] == ["H2-KEEPALIVE", "AI-CLAUDE"]

    rules = out["rules"]
    assert "DOMAIN-SUFFIX,claude.com,Proxy" not in rules
    assert "DOMAIN-SUFFIX,claude.com,AI-CLAUDE" in rules
    assert "DOMAIN-SUFFIX,anthropic.com,AI-CLAUDE" in rules
    assert "DOMAIN-SUFFIX,claude.ai,AI-CLAUDE" in rules
    ka_idx = next(i for i, r in enumerate(rules) if r.endswith(",AI-CLAUDE-KEEPALIVE"))
    assert "api.anthropic.com" in rules[ka_idx]
    assert ka_idx < rules.index("DOMAIN-SUFFIX,anthropic.com,AI-CLAUDE")


def test_h2_keepalive_cert_and_hosts_cover_anthropic(tmp_path: Path):
    assert h2_mod.ANTHROPIC_API_HOST in h2_mod.KEEPALIVE_HOSTS
    assert h2_mod.ANTHROPIC_API_HOST in h2_mod.H1_TO_H2_HOSTS
    assert "api.anthropic.com" in h2_mod.LEAF_CERT_SANS
    assert "*.anthropic.com" in h2_mod.LEAF_CERT_SANS

    tls_dir = tmp_path / "tls"
    _ca, leaf_crt, _key, _mtime = h2_mod.ensure_local_ca_and_leaf_cert(tls_dir, install_system_trust=False)
    from cryptography import x509

    cert = x509.load_pem_x509_certificate(leaf_crt.read_bytes())
    sans = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value.get_values_for_type(x509.DNSName)
    assert "api.anthropic.com" in sans
    assert "*.anthropic.com" in sans


def test_anthropic_semantic_probe_distinguishes_405_ok_vs_403_blocked():
    def _http_err(url: str, code: int, body: bytes = b"") -> HTTPError:
        hdrs = Message()
        hdrs["Content-Type"] = "application/json"
        return HTTPError(url, code, "err", hdrs=hdrs, fp=io.BytesIO(body))

    class _Opener:
        def __init__(self, exc: Exception):
            self._exc = exc

        def open(self, request, timeout):
            raise self._exc

    url = "https://api.anthropic.com/v1/messages"
    # 健康无鉴权 GET 返回 405 Method Not Allowed -> ok
    verdict_405, _ = diag_mod._probe_target(_Opener(_http_err(url, 405, b'{"type":"error"}')), url, 3, 0)
    assert verdict_405 == diag_mod.PROBE_OK
    # 健康无鉴权 POST 返回 401 -> ok
    verdict_401, _ = diag_mod._probe_target(_Opener(_http_err(url, 401, b'{"type":"error"}')), url, 3, 0)
    assert verdict_401 == diag_mod.PROBE_OK
    # Anthropic / Cloudflare WAF 403 -> geo_blocked
    verdict_403, _ = diag_mod._probe_target(_Opener(_http_err(url, 403, b"Forbidden")), url, 3, 0)
    assert verdict_403 == diag_mod.PROBE_GEO_BLOCKED
    # 地区受限 400 -> geo_blocked
    verdict_400_geo, _ = diag_mod._probe_target(
        _Opener(_http_err(url, 400, b'{"error":{"message":"Your country, region, or territory not supported"}}')),
        url,
        3,
        0,
    )
    assert verdict_400_geo == diag_mod.PROBE_GEO_BLOCKED
    # chatgpt.com 403 仍保持旧契约 ok（避免 stdlib OpenSSL 触发 CF 挑战误报）
    verdict_chatgpt_403, _ = diag_mod._probe_target(
        _Opener(_http_err("https://chatgpt.com", 403, b"Just a moment")),
        "https://chatgpt.com",
        3,
        0,
    )
    assert verdict_chatgpt_403 == diag_mod.PROBE_OK


def test_ai_watch_quarantines_and_switches_blocked_claude_node(tmp_path: Path, monkeypatch):
    paths = _make_paths(tmp_path)
    config_file(paths).write_text("mixed-port: 7890\n", encoding="utf-8")
    ai_claude_order_file(paths).write_text(
        json.dumps(["🇺🇸 United States 01", "🇯🇵 Japan 01"]), encoding="utf-8"
    )

    groups = {
        "AI-MANUAL": ProxyGroup("AI-MANUAL", "Fallback", "🇺🇸 United States 01", ["🇺🇸 United States 01"]),
        "AI-GEMINI": ProxyGroup("AI-GEMINI", "Fallback", "🇹🇼 Taiwan 01", ["🇹🇼 Taiwan 01"]),
        "AI-CLAUDE": ProxyGroup(
            "AI-CLAUDE",
            "Fallback",
            "🇺🇸 United States 01",
            ["🇺🇸 United States 01", "🇯🇵 Japan 01"],
        ),
        "🇺🇸 United States 01": ProxyGroup("🇺🇸 United States 01", "AnyTLS", "-", [], alive=True, delay=180),
        "🇯🇵 Japan 01": ProxyGroup("🇯🇵 Japan 01", "AnyTLS", "-", [], alive=True, delay=95),
        "🇹🇼 Taiwan 01": ProxyGroup("🇹🇼 Taiwan 01", "AnyTLS", "-", [], alive=True, delay=60),
    }

    switched: list[tuple[str, str, bool, bool]] = []

    class _FakeQuery:
        def __init__(self):
            self.api = SimpleNamespace(
                get_groups=lambda: groups,
                get_connections=lambda: {"connections": []},
                close_connection=lambda _cid: None,
            )

        def switch_group(self, group, target, drop_connections=True, force_drop=False):
            switched.append((group, target, drop_connections, force_drop))
            groups[group] = ProxyGroup(group, "Fallback", target, list(groups[group].candidates))

    def _verdict(_p, url, **_kw):
        if "anthropic.com" in url:
            return diag_mod.PROBE_GEO_BLOCKED
        return diag_mod.PROBE_OK

    monkeypatch.setattr(ai_watch_mod, "ai_target_verdict", _verdict)
    svc = ai_watch_mod.AIWatchService(
        paths,
        proc_root=tmp_path / "no-proc",
        agy_log_dir=tmp_path / "no-agy",
        codex_log_dir=tmp_path / "no-codex",
    )
    svc.query = _FakeQuery()

    # 连续两次 403/地区风控触发强切 + 12h 隔离 + 顺位沉底
    r1 = svc.watch()
    c1 = next(r for r in r1.results if r.group == AI_CLAUDE_GROUP)
    assert c1.verdict == diag_mod.PROBE_GEO_BLOCKED
    assert c1.switched is False

    r2 = svc.watch()
    c2 = next(r for r in r2.results if r.group == AI_CLAUDE_GROUP)
    assert c2.switched is True
    assert c2.target == "🇯🇵 Japan 01"
    assert switched == [(AI_CLAUDE_GROUP, "🇯🇵 Japan 01", True, True)]
    assert probe_mod.is_claude_blocked("🇺🇸 United States 01", paths) is True
    new_order = json.loads(ai_claude_order_file(paths).read_text(encoding="utf-8"))
    assert new_order == ["🇯🇵 Japan 01", "🇺🇸 United States 01"]


def test_query_connection_isolation_and_aliases_for_ai_claude():
    claude_conn = {
        "chains": ["US-01", "AI-CLAUDE"],
        "metadata": {"host": "api.anthropic.com", "process": "claude.exe"},
    }
    manual_conn = {
        "chains": ["US-01", "AI-MANUAL"],
        "metadata": {"host": "chatgpt.com", "process": "codex.exe"},
    }
    gemini_conn = {
        "chains": ["TW-01", "AI-GEMINI"],
        "metadata": {"host": "daily-cloudcode-pa.googleapis.com", "process": "agy.exe"},
    }

    assert QueryService._connection_follows_ai_switch(claude_conn, "AI-CLAUDE") is True
    assert QueryService._connection_follows_ai_switch(manual_conn, "AI-CLAUDE") is False
    assert QueryService._connection_follows_ai_switch(gemini_conn, "AI-CLAUDE") is False
    assert QueryService._connection_follows_ai_switch(claude_conn, "AI-MANUAL") is False
    assert QueryService._connection_follows_ai_switch(claude_conn, "AI-GEMINI") is False

    assert resolve_group_alias("claude", ["AI-MANUAL", "AI-GEMINI", "AI-CLAUDE"]) == "AI-CLAUDE"
    assert resolve_group_alias("anthropic", ["AI-MANUAL", "AI-GEMINI", "AI-CLAUDE"]) == "AI-CLAUDE"
    # 向后兼容：旧运行态没有 AI-CLAUDE 时回落 AI-MANUAL
    assert resolve_group_alias("claude", ["AI-MANUAL", "AI-GEMINI"]) == "AI-MANUAL"
    assert probe_mod.default_group_for_profile("claude") == "AI-CLAUDE"

    groups = {
        "AI-MANUAL": ProxyGroup("AI-MANUAL", "Fallback", "US-01", ["US-01"]),
        "AI-AUTO": ProxyGroup("AI-AUTO", "Fallback", "AI-US", ["AI-US"]),
        "AI-US": ProxyGroup("AI-US", "Fallback", "US-01", ["US-01"]),
        "AI-SG": ProxyGroup("AI-SG", "Fallback", "SG-01", ["SG-01"]),
        "AI-GEMINI": ProxyGroup("AI-GEMINI", "Fallback", "TW-01", ["TW-01"]),
        "AI-CLAUDE": ProxyGroup("AI-CLAUDE", "Fallback", "JP-01", ["JP-01"]),
        "US-01": ProxyGroup("US-01", "AnyTLS", "-", [], alive=True, delay=180),
        "SG-01": ProxyGroup("SG-01", "AnyTLS", "-", [], alive=True, delay=80),
        "TW-01": ProxyGroup("TW-01", "AnyTLS", "-", [], alive=True, delay=60),
        "JP-01": ProxyGroup("JP-01", "AnyTLS", "-", [], alive=True, delay=100),
    }
    route = resolve_ai_route(groups)
    assert route.claude is not None
    assert route.claude.group == "AI-CLAUDE"
    assert route.claude.node == "JP-01"
    assert route.claude.delay == 100


def test_rebalance_persists_ai_claude_order_and_switches_blocked_current(tmp_path: Path, monkeypatch):
    paths = _make_paths(tmp_path)
    config_file(paths).write_text("mixed-port: 7890\nverge-mode: true\n", encoding="utf-8")
    probe_mod.record_claude_blocked_node(paths, "🇺🇸 United States 01")

    def _fake_probe(self, *, group, profile, record_history=False, **_kw):
        if group == "AI-CLAUDE":
            s_us = probe_mod.ProbeSummary("🇺🇸 United States 01", (180, 180, 180), 0)
            s_jp = probe_mod.ProbeSummary("🇯🇵 Japan 01", (110, 110, 110), 0)
            return SimpleNamespace(
                group=group,
                profile=profile,
                current="🇺🇸 United States 01",
                current_verdict=probe_mod.StabilityVerdict(True, "ok"),
                verdict=probe_mod.StabilityVerdict(True, "ok"),
                summaries=(s_us, s_jp),
            )
        s = probe_mod.ProbeSummary("🇹🇼 Taiwan 01", (60, 60, 60), 0)
        return SimpleNamespace(
            group=group,
            profile=profile,
            current="🇹🇼 Taiwan 01",
            current_verdict=probe_mod.StabilityVerdict(True, "ok"),
            verdict=probe_mod.StabilityVerdict(True, "ok"),
            summaries=(s,),
        )

    switched: list[tuple[str, str]] = []

    class _FakeQuery:
        def __init__(self, _p):
            self.api = SimpleNamespace(
                get_groups=lambda: {
                    "AI-MANUAL": ProxyGroup("AI-MANUAL", "Fallback", "🇹🇼 Taiwan 01", ["🇹🇼 Taiwan 01"]),
                    "AI-GEMINI": ProxyGroup("AI-GEMINI", "Fallback", "🇹🇼 Taiwan 01", ["🇹🇼 Taiwan 01"]),
                    "AI-CLAUDE": ProxyGroup(
                        "AI-CLAUDE",
                        "Fallback",
                        "🇺🇸 United States 01",
                        ["🇺🇸 United States 01", "🇯🇵 Japan 01"],
                    ),
                },
                get_connections=lambda: {"connections": []},
            )

        def switch_group(self, group, target, **_kw):
            switched.append((group, target))

    monkeypatch.setattr(rebalance_mod.ProbeService, "probe", _fake_probe)
    monkeypatch.setattr(
        rebalance_mod.ProbeService,
        "__init__",
        lambda self, p: setattr(self, "paths", p)
        or setattr(
            self,
            "api",
            SimpleNamespace(get_groups=lambda: {"AI-MANUAL": None, "AI-GEMINI": None, "AI-CLAUDE": None}),
        ),
    )
    monkeypatch.setattr(rebalance_mod, "QueryService", _FakeQuery)

    report = rebalance_mod.RebalanceService(paths).rebalance(external_only=True)
    assert report.claude_ordered == ("🇯🇵 Japan 01", "🇺🇸 United States 01")
    assert report.claude_switched_to == "🇯🇵 Japan 01"
    assert ("AI-CLAUDE", "🇯🇵 Japan 01") in switched
    saved = json.loads(ai_claude_order_file(paths).read_text(encoding="utf-8"))
    assert saved == ["🇯🇵 Japan 01", "🇺🇸 United States 01"]


def test_verge_status_and_hot_reload_surface_ai_claude(tmp_path: Path, monkeypatch):
    paths = _make_paths(tmp_path)
    config_file(paths).write_text("mixed-port: 7897\nverge-mode: true\n", encoding="utf-8")
    now = time.time()
    (paths.state_dir / "verge-daemon.json").write_text(
        json.dumps(
            {
                "pid": 12345,
                "updated_at": now,
                "last_watch_ts": now,
                "watch_runs": 5,
                "rebalance_runs": 1,
                "total_switches": 0,
                "total_reaped_connections": 0,
                "last_verdicts": {"AI-MANUAL": "ok", "AI-GEMINI": "ok", "AI-CLAUDE": "ok"},
            }
        ),
        encoding="utf-8",
    )

    class _FakeAPI:
        def __init__(self, _p):
            pass

        def version(self):
            return {"version": "v1.19.33"}

        def get_groups(self):
            return {
                "AI-MANUAL": ProxyGroup("AI-MANUAL", "fallback", "US-01", ["US-01"]),
                "AI-GEMINI": ProxyGroup("AI-GEMINI", "fallback", "TW-01", ["TW-01"]),
                "AI-CLAUDE": ProxyGroup("AI-CLAUDE", "fallback", "JP-01", ["JP-01"]),
                "AI-AUTO": ProxyGroup("AI-AUTO", "fallback", "US-01", ["US-01"]),
                "AI-MANUAL-KEEPALIVE": ProxyGroup(
                    "AI-MANUAL-KEEPALIVE", "fallback", "H2-KEEPALIVE", ["H2-KEEPALIVE", "AI-MANUAL"]
                ),
                "AI-GEMINI-KEEPALIVE": ProxyGroup(
                    "AI-GEMINI-KEEPALIVE", "fallback", "H2-KEEPALIVE", ["H2-KEEPALIVE", "AI-GEMINI"]
                ),
                "AI-CLAUDE-KEEPALIVE": ProxyGroup(
                    "AI-CLAUDE-KEEPALIVE", "fallback", "H2-KEEPALIVE", ["H2-KEEPALIVE", "AI-CLAUDE"]
                ),
                "US-01": ProxyGroup("US-01", "anytls", "-", [], delay=180, alive=True),
                "TW-01": ProxyGroup("TW-01", "anytls", "-", [], delay=60, alive=True),
                "JP-01": ProxyGroup("JP-01", "anytls", "-", [], delay=100, alive=True),
            }

        def get_rules(self):
            return {
                "rules": [
                    {"type": "DomainSuffix", "payload": "googleapis.com", "proxy": "AI-GEMINI"},
                    {"type": "DomainSuffix", "payload": "openai.com", "proxy": "AI-MANUAL"},
                    {"type": "DomainSuffix", "payload": "anthropic.com", "proxy": "AI-CLAUDE"},
                    {"type": "DomainSuffix", "payload": "claude.com", "proxy": "AI-CLAUDE"},
                ]
            }

        def get_connections(self):
            return {"connections": [{"id": "c-cl", "chains": ["JP-01", "AI-CLAUDE"]}]}

    monkeypatch.setattr("cproxy.services.verge.APIBackend", _FakeAPI)
    monkeypatch.setattr("cproxy.services.verge._is_pid_alive", lambda _pid: True)
    rep = check_verge_effectiveness(paths, now=now)
    assert rep.overall_effective is True
    assert rep.ai_claude_rules_count == 2
    claude_egress = next(e for e in rep.egress if e.group == "AI-CLAUDE")
    assert claude_egress.leaf_node == "JP-01"
    assert claude_egress.active_connections == 1
    assert claude_egress.last_verdict == "ok"
    assert try_hot_reload_verge_runtime is not None


def test_ai_claude_prefer_region_and_node_order_end_to_end(tmp_path: Path, monkeypatch):
    from cproxy.services.nodelist import apply_prefer_order, prefer_rank_for_node
    from cproxy.services.verge import render_verge_preprocess_script

    prefer = ["🇯🇵 Japan 01", "JP", "🇸🇬 Singapore 01", "SG", "🇺🇸 United States 02", "US"]
    nodes = [
        "🇺🇸 United States 01",
        "🇺🇸 United States 02",
        "🇹🇼 Taiwan 03",
        "🇸🇬 Singapore 04",
        "🇸🇬 Singapore 01",
        "🇯🇵 Japan 03",
        "🇯🇵 Japan 01",
    ]
    assert prefer_rank_for_node("🇯🇵 Japan 01", prefer) == 0
    assert prefer_rank_for_node("🇯🇵 Japan 03", prefer) == 1
    assert prefer_rank_for_node("🇸🇬 Singapore 01", prefer) == 2
    assert prefer_rank_for_node("🇹🇼 Taiwan 03", prefer) == len(prefer)
    assert prefer_rank_for_node("🇬🇧 United Kingdom 01", ["UK"]) == 0
    assert apply_prefer_order(nodes, prefer) == [
        "🇯🇵 Japan 01",
        "🇯🇵 Japan 03",
        "🇸🇬 Singapore 01",
        "🇸🇬 Singapore 04",
        "🇺🇸 United States 02",
        "🇺🇸 United States 01",
        "🇹🇼 Taiwan 03",
    ]

    # 1. Runtime rendering respects exact + region prefer and still demotes high-multiplier JP nodes
    paths = _make_paths(tmp_path)
    raw_cfg = {
        "mixed-port": 7890,
        "ai-claude-prefer": prefer,
        "proxies": [
            _anytls_node("🇺🇸 United States 01", "us1.example.com"),
            _anytls_node("🇺🇸 United States 02", "us2.example.com"),
            _anytls_node("🇹🇼 Taiwan 03", "tw3.example.com"),
            _anytls_node("🇸🇬 Singapore 01", "sg1.example.com"),
            _anytls_node("🇯🇵 Japan 03", "jp3.example.com"),
            _anytls_node("🇯🇵 Japan 01", "jp1.example.com"),
            _anytls_node("🇯🇵 Japan 5X", "jp5x.example.com"),
        ],
        "proxy-groups": [],
        "rules": ["MATCH,DIRECT"],
    }
    config_file(paths).write_text(yaml.safe_dump(raw_cfg, allow_unicode=True), encoding="utf-8")
    rt_path = RuntimeBackend(paths).render_runtime()
    rt = yaml.safe_load(rt_path.read_text(encoding="utf-8"))
    claude_grp = next(g for g in rt["proxy-groups"] if g["name"] == AI_CLAUDE_GROUP)
    assert claude_grp["proxies"] == [
        "🇯🇵 Japan 01",
        "🇯🇵 Japan 03",
        "🇸🇬 Singapore 01",
        "🇺🇸 United States 02",
        "🇺🇸 United States 01",
        "🇹🇼 Taiwan 03",
        "🇯🇵 Japan 5X",
    ]

    # 2. probe_rank_key prioritizes healthy preferred node over faster unpreferred region
    s_jp1 = probe_mod.ProbeSummary("🇯🇵 Japan 01", (88, 89, 88), 0)
    s_tw3 = probe_mod.ProbeSummary("🇹🇼 Taiwan 03", (60, 60, 62), 0)
    s_jp_dead = probe_mod.ProbeSummary("🇯🇵 Japan 02", (), 3)
    assert probe_mod.probe_rank_key(s_jp1, prefer_non_iepl=True, prefer=prefer) < probe_mod.probe_rank_key(
        s_tw3, prefer_non_iepl=True, prefer=prefer
    )
    assert probe_mod.probe_rank_key(s_tw3, prefer_non_iepl=True, prefer=prefer) < probe_mod.probe_rank_key(
        s_jp_dead, prefer_non_iepl=True, prefer=prefer
    )

    # 3. AIWatchService._pick_next prefers same-region JP backup before faster TW when JP 01 fails
    groups = {
        "AI-CLAUDE": ProxyGroup(
            "AI-CLAUDE",
            "Fallback",
            "🇯🇵 Japan 01",
            ["🇹🇼 Taiwan 03", "🇺🇸 United States 02", "🇸🇬 Singapore 01", "🇯🇵 Japan 03", "🇯🇵 Japan 01"],
        ),
        "🇹🇼 Taiwan 03": ProxyGroup("🇹🇼 Taiwan 03", "AnyTLS", "-", [], alive=True, delay=60),
        "🇸🇬 Singapore 01": ProxyGroup("🇸🇬 Singapore 01", "AnyTLS", "-", [], alive=True, delay=85),
        "🇺🇸 United States 02": ProxyGroup("🇺🇸 United States 02", "AnyTLS", "-", [], alive=True, delay=195),
        "🇯🇵 Japan 03": ProxyGroup("🇯🇵 Japan 03", "AnyTLS", "-", [], alive=True, delay=105),
        "🇯🇵 Japan 01": ProxyGroup("🇯🇵 Japan 01", "AnyTLS", "-", [], alive=True, delay=88),
    }
    svc = ai_watch_mod.AIWatchService(paths)
    svc.query = SimpleNamespace(api=SimpleNamespace(get_groups=lambda: groups))
    assert svc._pick_next(groups, groups["AI-CLAUDE"], "🇯🇵 Japan 01", {}, time.time()) == "🇯🇵 Japan 03"

    # 4. RebalanceService persists ai-claude-order.json in prefer-tier order (JP -> SG -> US -> TW)
    def _fake_probe(self, *, group, profile, record_history=False, **_kw):
        if group == AI_CLAUDE_GROUP:
            summaries = (
                probe_mod.ProbeSummary("🇹🇼 Taiwan 03", (60, 60, 60), 0),
                probe_mod.ProbeSummary("🇺🇸 United States 01", (210, 210, 210), 0),
                probe_mod.ProbeSummary("🇺🇸 United States 02", (195, 195, 195), 0),
                probe_mod.ProbeSummary("🇸🇬 Singapore 01", (85, 85, 85), 0),
                probe_mod.ProbeSummary("🇯🇵 Japan 03", (105, 105, 105), 0),
                probe_mod.ProbeSummary("🇯🇵 Japan 01", (88, 88, 88), 0),
            )
            return SimpleNamespace(
                group=group,
                profile=profile,
                current="🇯🇵 Japan 01",
                current_verdict=probe_mod.StabilityVerdict(True, "ok"),
                verdict=probe_mod.StabilityVerdict(True, "ok"),
                summaries=summaries,
            )
        s = probe_mod.ProbeSummary("🇹🇼 Taiwan 03", (60, 60, 60), 0)
        return SimpleNamespace(
            group=group,
            profile=profile,
            current="🇹🇼 Taiwan 03",
            current_verdict=probe_mod.StabilityVerdict(True, "ok"),
            verdict=probe_mod.StabilityVerdict(True, "ok"),
            summaries=(s,),
        )

    monkeypatch.setattr(rebalance_mod.ProbeService, "probe", _fake_probe)
    monkeypatch.setattr(
        rebalance_mod.ProbeService,
        "__init__",
        lambda self, p: setattr(self, "paths", p)
        or setattr(
            self,
            "api",
            SimpleNamespace(get_groups=lambda: {"AI-MANUAL": None, "AI-GEMINI": None, "AI-CLAUDE": None}),
        ),
    )
    rep = rebalance_mod.RebalanceService(paths).rebalance(dry_run=True, external_only=True)
    assert rep.claude_ordered == (
        "🇯🇵 Japan 01",
        "🇯🇵 Japan 03",
        "🇸🇬 Singapore 01",
        "🇺🇸 United States 02",
        "🇺🇸 United States 01",
        "🇹🇼 Taiwan 03",
    )

    # 5. Rendered Clash Verge Script.js preserves JP-first prefer order even on manual profile refresh
    node_bin = shutil.which("node")
    if node_bin:
        rendered_js = render_verge_preprocess_script(paths)
        assert "🇯🇵 Japan 01" in rendered_js
        script_file = tmp_path / "Script.js"
        script_file.write_text(rendered_js, encoding="utf-8")
        runner = (
            "const fs = require('fs'); const vm = require('vm');"
            f"const code = fs.readFileSync({json.dumps(str(script_file))}, 'utf8') + '\\n;globalThis.__main = main;';"
            "const ctx = { console, globalThis: {} }; ctx.globalThis = ctx;"
            "vm.createContext(ctx); vm.runInContext(code, ctx);"
            "const out = ctx.__main(JSON.parse(fs.readFileSync(0, 'utf8')));"
            "process.stdout.write(JSON.stringify(out));"
        )
        proc = subprocess.run(
            [node_bin, "-e", runner],
            input=json.dumps(raw_cfg),
            text=True,
            capture_output=True,
            check=True,
        )
        out_cfg = json.loads(proc.stdout)
        verge_claude = next(g for g in out_cfg["proxy-groups"] if g["name"] == AI_CLAUDE_GROUP)
        assert verge_claude["proxies"] == [
            "🇯🇵 Japan 01",
            "🇯🇵 Japan 03",
            "🇸🇬 Singapore 01",
            "🇺🇸 United States 02",
            "🇺🇸 United States 01",
            "🇹🇼 Taiwan 03",
            "🇯🇵 Japan 5X",
        ]
