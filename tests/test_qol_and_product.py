"""人性化体验（QoL）与产品化能力单元测试。

覆盖：
1. 通用节点标签解析引擎 (`node_tags.py`)
2. 组名简写别名与节点模糊匹配 (`names.py`, `switch`)
3. 域名/URL/IP 分流追查 (`cproxy why`)
4. 节点拉黑与临时屏蔽 (`cproxy ban` / `cproxy unban` + `render_runtime` 联动 + 空组安全兜底)
5. 主动告警通知钩子 (`cproxy notify` + 多平台 payload 构造)
6. Shell 提示符极速微组件 (`cproxy prompt`)
"""

from __future__ import annotations

import json
from pathlib import Path

import yaml

from cproxy.backend.models import ProxyGroup
from cproxy.backend.runtime import RuntimeBackend
from cproxy.cli import run
from cproxy.config import AppPaths, config_file
from cproxy.names import match_candidates_fuzzy, resolve_candidate, resolve_group_alias
from cproxy.services.ban import add_ban, filter_unbanned_nodes, load_active_bans, remove_ban
from cproxy.services.benchmark import evaluate_providers
from cproxy.services.bundle import export_bundle, import_bundle
from cproxy.services.flush import flush_connections
from cproxy.services.node_tags import parse_node_tag
from cproxy.services.notify import (
    _build_webhook_payload,
    detect_webhook_kind,
    load_notify_config,
    save_notify_config,
)
from cproxy.services.prompt import PromptSnapshot, format_prompt
from cproxy.services.rules import add_custom_rule, load_custom_rules, remove_custom_rule
from cproxy.services.why import explain_route, extract_host


def _make_paths(tmp_path: Path) -> AppPaths:
    paths = AppPaths(
        config_dir=tmp_path / "config",
        data_dir=tmp_path / "data",
        state_dir=tmp_path / "state",
    )
    paths.config_dir.mkdir(parents=True, exist_ok=True)
    paths.data_dir.mkdir(parents=True, exist_ok=True)
    paths.state_dir.mkdir(parents=True, exist_ok=True)
    return paths


def test_parse_node_tag_across_airport_styles() -> None:
    tag1 = parse_node_tag("🇰🇷韩国 01 | IEPL | 1.7X | CM")
    assert tag1.region == "KR"
    assert tag1.line_type == "iepl"
    assert tag1.is_dedicated_line is True
    assert tag1.multiplier == 1.7
    assert tag1.index == "01"
    assert tag1.carrier == "CM"
    assert tag1.compact_label == "🇰🇷韩国01"

    tag2 = parse_node_tag("[IPLC] HK-BGP-03 (0.5倍率)")
    assert tag2.region == "HK"
    assert tag2.line_type == "iplc"
    assert tag2.multiplier == 0.5
    assert tag2.index == "03"
    assert tag2.carrier == "BGP"

    tag3 = parse_node_tag("🇬🇧英国 02 | 1X")
    assert tag3.region == "GB"
    assert tag3.line_type == "standard"
    assert tag3.multiplier == 1.0
    assert tag3.index == "02"
    assert tag3.compact_label == "🇬🇧英国02"


def test_group_aliases_and_fuzzy_candidate_resolution() -> None:
    groups = ["CyberGuard", "AI-MANUAL", "AI-GEMINI", "AI-US", "AI-SG", "AI-AUTO"]
    assert resolve_group_alias("ai", groups) == "AI-MANUAL"
    assert resolve_group_alias("codex", groups) == "AI-MANUAL"
    assert resolve_group_alias("gemini", groups) == "AI-GEMINI"
    assert resolve_group_alias("us", groups) == "AI-US"
    assert resolve_group_alias("default", groups, match_group="CyberGuard") == "CyberGuard"

    candidates = [
        "🇰🇷韩国 01 | IEPL | 1.7X | CM",
        "🇰🇷韩国 02 | IEPL | 1.7X | CM",
        "🇬🇧英国 01 | 1X",
        "🇺🇸美国 01 | IEPL | 1.7X | CM",
    ]
    # 紧凑简写唯一命中
    assert resolve_candidate("kr01", candidates) == "🇰🇷韩国 01 | IEPL | 1.7X | CM"
    assert resolve_candidate("韩国2", candidates) == "🇰🇷韩国 02 | IEPL | 1.7X | CM"
    assert resolve_candidate("uk", candidates) == "🇬🇧英国 01 | 1X"
    assert resolve_candidate("us01", candidates) == "🇺🇸美国 01 | IEPL | 1.7X | CM"

    # 多义模糊词返回 None（不盲猜），但 match_candidates_fuzzy 列出全部匹配项
    assert resolve_candidate("kr", candidates) is None
    assert len(match_candidates_fuzzy("kr", candidates)) == 2


def test_explain_route_matches_rules_and_resolves_chain(tmp_path: Path, monkeypatch) -> None:
    paths = _make_paths(tmp_path)
    runtime_cfg = {
        "proxy-groups": [
            {"name": "AI-GEMINI", "type": "fallback", "proxies": ["🇬🇧英国 01 | 1X"]},
            {"name": "AI-MANUAL", "type": "fallback", "proxies": ["🇰🇷韩国 01 | IEPL | 1.7X | CM"]},
            {"name": "CyberGuard", "type": "select", "proxies": ["🇭🇰香港 01 | 1X"]},
        ],
        "rules": [
            "DOMAIN-SUFFIX,googleapis.com,AI-GEMINI",
            "PROCESS-NAME,agy,AI-MANUAL",
            "DOMAIN-SUFFIX,anthropic.com,AI-MANUAL",
            "DOMAIN-SUFFIX,pypi.org,DIRECT",
            "MATCH,CyberGuard",
        ],
    }
    (paths.data_dir / "runtime.yaml").write_text(
        yaml.safe_dump(runtime_cfg, allow_unicode=True), encoding="utf-8"
    )

    monkeypatch.setattr(
        "cproxy.services.why.APIBackend.get_groups",
        lambda self, **kw: {
            "AI-GEMINI": ProxyGroup("AI-GEMINI", "Fallback", "🇬🇧英国 01 | 1X", ["🇬🇧英国 01 | 1X"]),
            "AI-MANUAL": ProxyGroup("AI-MANUAL", "Fallback", "🇰🇷韩国 01 | IEPL | 1.7X | CM", ["🇰🇷韩国 01 | IEPL | 1.7X | CM"]),
            "🇬🇧英国 01 | 1X": ProxyGroup("🇬🇧英国 01 | 1X", "VLESS", "-", [], delay=525, alive=True),
            "🇰🇷韩国 01 | IEPL | 1.7X | CM": ProxyGroup("🇰🇷韩国 01 | IEPL | 1.7X | CM", "VLESS", "-", [], delay=174, alive=True),
        },
    )
    monkeypatch.setattr("cproxy.services.why.APIBackend.get_connections", lambda self: [])

    assert extract_host("https://daily-cloudcode-pa.googleapis.com:443/v1/stream") == "daily-cloudcode-pa.googleapis.com"
    rep_gem = explain_route(paths, "https://daily-cloudcode-pa.googleapis.com/v1")
    assert rep_gem.matched_rule == "DOMAIN-SUFFIX,googleapis.com,AI-GEMINI"
    assert rep_gem.policy_group == "AI-GEMINI"
    assert rep_gem.exit_node == "🇬🇧英国 01 | 1X"
    assert rep_gem.exit_delay == 525

    rep_proc = explain_route(paths, "api.github.com", process="agy")
    assert rep_proc.matched_rule == "PROCESS-NAME,agy,AI-MANUAL"
    assert rep_proc.exit_node == "🇰🇷韩国 01 | IEPL | 1.7X | CM"

    rep_direct = explain_route(paths, "files.pypi.org")
    assert rep_direct.policy_group == "DIRECT"
    assert rep_direct.exit_node == "DIRECT"


def test_ban_and_unban_filter_runtime_with_safety_fallback(tmp_path: Path) -> None:
    paths = _make_paths(tmp_path)
    src_cfg = {
        "proxies": [
            {"name": "🇺🇸美国 01", "type": "ss", "server": "1.1.1.1", "port": 443, "cipher": "aes-128-gcm", "password": "x"},
            {"name": "🇺🇸美国 02", "type": "ss", "server": "1.1.1.2", "port": 443, "cipher": "aes-128-gcm", "password": "x"},
            {"name": "🇸🇬新加坡 01", "type": "ss", "server": "2.2.2.1", "port": 443, "cipher": "aes-128-gcm", "password": "x"},
        ],
        "proxy-groups": [
            {"name": "Proxy", "type": "select", "proxies": ["🇺🇸美国 01", "🇺🇸美国 02", "🇸🇬新加坡 01"]},
        ],
        "rules": ["MATCH,Proxy"],
    }
    config_file(paths).write_text(yaml.safe_dump(src_cfg, allow_unicode=True), encoding="utf-8")

    add_ban(paths, "us01", hours=12, reason="Claude 403")
    bans = load_active_bans(paths)
    assert len(bans) == 1

    runtime_path = RuntimeBackend(paths).render_runtime()
    rendered = yaml.safe_load(runtime_path.read_text(encoding="utf-8"))
    groups = {g["name"]: g["proxies"] for g in rendered["proxy-groups"]}
    assert "🇺🇸美国 01" not in groups["AI-US"]
    assert "🇺🇸美国 02" in groups["AI-US"]

    # 安全兜底：若把某必选组的全部节点都拉黑，保留原列表以防空组搞崩 mihomo
    assert filter_unbanned_nodes(["🇸🇬新加坡 01"], [add_ban(paths, "sg01")], keep_fallback=True) == ["🇸🇬新加坡 01"]

    removed = remove_ban(paths, clear_all=True)
    assert len(removed) == 2
    assert load_active_bans(paths) == []


def test_notify_config_and_webhook_payloads(tmp_path: Path) -> None:
    paths = _make_paths(tmp_path)
    cfg = save_notify_config(paths, webhook_url="https://open.feishu.cn/open-apis/bot/v2/hook/secret123", desktop=False)
    assert cfg.enabled is True
    assert cfg.channel_kind == "feishu"
    assert load_notify_config(paths).channel_kind == "feishu"

    feishu_payload = _build_webhook_payload("feishu", cfg.webhook_url, "标题", "内容", "test")
    assert feishu_payload["msg_type"] == "text"
    assert "标题" in feishu_payload["content"]["text"]

    tg_url = "https://api.telegram.org/bot123:ABC/sendMessage?chat_id=9988"
    assert detect_webhook_kind(tg_url) == "telegram"
    tg_payload = _build_webhook_payload("telegram", tg_url, "标题", "内容", "test")
    assert tg_payload["chat_id"] == "9988"


def test_prompt_formatting_and_cli_commands(tmp_path: Path, monkeypatch, capsys) -> None:
    snap_run = PromptSnapshot(
        running=True,
        proxy_env_active=True,
        ai_node="🇰🇷韩国 01 | IEPL | 1.7X | CM",
        gemini_node="🇬🇧英国 01 | 1X",
    )
    assert format_prompt(snap_run, "compact") == "[🌐⚡ 🇰🇷韩国01]"
    assert format_prompt(snap_run, "full") == "[🌐⚡ AI:🇰🇷韩国01 · Gem:🇬🇧英国01]"
    assert "#[fg=green]" in format_prompt(snap_run, "tmux")

    snap_off = PromptSnapshot(running=False, proxy_env_active=False)
    assert format_prompt(snap_off, "compact") == "[🌐 OFF]"

    paths = _make_paths(tmp_path)
    monkeypatch.setattr("cproxy.cli.default_paths", lambda: paths)
    assert run(["ban", "kr02", "--hours", "6", "--reason", "测试屏蔽", "--json"]) == 0
    out_ban = json.loads(capsys.readouterr().out)
    assert out_ban["command"] == "ban"
    assert out_ban["data"]["added"]["pattern"] == "kr02"

    assert run(["ban", "--json"]) == 0
    out_list = json.loads(capsys.readouterr().out)
    assert len(out_list["data"]["bans"]) == 1

    assert run(["unban", "kr02"]) == 0
    capsys.readouterr()
    assert load_active_bans(paths) == []


def test_staggered_intervals_multiplier_guard_and_custom_rules(tmp_path: Path) -> None:
    paths = _make_paths(tmp_path)
    src_cfg = {
        "proxies": [
            {"name": "🇺🇸美国 01 | 5X", "type": "ss", "server": "1.1.1.1", "port": 443, "cipher": "chacha20", "password": "x"},
            {"name": "🇺🇸美国 02 | 1.5X", "type": "ss", "server": "1.1.1.2", "port": 443, "cipher": "chacha20", "password": "x"},
            {"name": "🇸🇬新加坡 01 | 1X", "type": "ss", "server": "2.2.2.1", "port": 443, "cipher": "chacha20", "password": "x"},
            {"name": "🇬🇧英国 01 | 1X", "type": "ss", "server": "3.3.3.1", "port": 443, "cipher": "chacha20", "password": "x"},
        ],
        "proxy-groups": [
            {
                "name": "Proxy",
                "type": "select",
                "proxies": ["🇺🇸美国 01 | 5X", "🇺🇸美国 02 | 1.5X", "🇸🇬新加坡 01 | 1X", "🇬🇧英国 01 | 1X"],
            },
            {"name": "Auto", "type": "url-test", "interval": 300, "proxies": ["🇺🇸美国 02 | 1.5X"]},
        ],
        "rules": ["MATCH,Proxy"],
    }
    config_file(paths).write_text(yaml.safe_dump(src_cfg, allow_unicode=True), encoding="utf-8")

    # 添加一条自定义分流规则（用简写别名 ai -> AI-MANUAL）
    entry = add_custom_rule(paths, "cursor.sh", "ai", comment="Cursor 走 AI 组")
    assert entry.policy == "AI-MANUAL"
    assert entry.rule_type == "DOMAIN-SUFFIX"

    runtime_path = RuntimeBackend(paths).render_runtime()
    rendered = yaml.safe_load(runtime_path.read_text(encoding="utf-8"))
    groups = {g["name"]: g for g in rendered["proxy-groups"]}

    # 1. 质数错峰 interval：所有自动测速组 interval 互不相同
    intervals = [
        groups[name]["interval"]
        for name in ("AI-US", "AI-SG", "AI-AUTO", "AI-MANUAL", "AI-GEMINI", "Auto")
    ]
    assert len(set(intervals)) == len(intervals)
    assert groups["AI-MANUAL"]["interval"] == 300

    # 2. 高倍率防刺客：5X 节点被自动沉底，1.5X 排在前面
    assert groups["AI-US"]["proxies"] == ["🇺🇸美国 02 | 1.5X", "🇺🇸美国 01 | 5X"]

    # 3. 自定义规则注入在最前
    assert rendered["rules"][0] == "DOMAIN-SUFFIX,cursor.sh,AI-MANUAL"

    removed = remove_custom_rule(paths, "cursor.sh")
    assert len(removed) == 1
    assert load_custom_rules(paths) == []


def test_flush_stale_and_targeted_connections(tmp_path: Path, monkeypatch) -> None:
    paths = _make_paths(tmp_path)
    monkeypatch.setattr(
        "cproxy.services.flush.APIBackend.get_groups",
        lambda self, **kw: {
            "AI-MANUAL": ProxyGroup("AI-MANUAL", "Fallback", "🇰🇷韩国 01 | IEPL", ["🇰🇷韩国 01 | IEPL", "🇺🇸美国 01"]),
            "🇰🇷韩国 01 | IEPL": ProxyGroup("🇰🇷韩国 01 | IEPL", "VLESS", "-", []),
            "🇺🇸美国 01": ProxyGroup("🇺🇸美国 01", "VLESS", "-", []),
        },
    )
    closed_ids: list[str] = []
    monkeypatch.setattr(
        "cproxy.services.flush.APIBackend.get_connections",
        lambda self: {
            "connections": [
                # 滞留在旧节点（美国 01）上的陈旧连接
                {
                    "id": "conn-stale-1",
                    "chains": ["🇺🇸美国 01", "AI-MANUAL"],
                    "metadata": {"host": "api.anthropic.com", "destinationPort": "443", "process": "claude"},
                },
                # 已经在当前新节点（韩国 01）上的正常连接
                {
                    "id": "conn-fresh-2",
                    "chains": ["🇰🇷韩国 01 | IEPL", "AI-MANUAL"],
                    "metadata": {"host": "chatgpt.com", "destinationPort": "443", "process": "codex"},
                },
            ]
        },
    )
    monkeypatch.setattr(
        "cproxy.services.flush.APIBackend.close_connection",
        lambda self, cid: closed_ids.append(cid) or True,
    )

    rep_stale = flush_connections(paths, stale_only=True)
    assert len(rep_stale.closed) == 1
    assert closed_ids == ["conn-stale-1"]

    closed_ids.clear()
    rep_host = flush_connections(paths, "chatgpt.com")
    assert len(rep_host.closed) == 1
    assert closed_ids == ["conn-fresh-2"]


def test_bundle_export_with_redact_and_import(tmp_path: Path) -> None:
    src_paths = _make_paths(tmp_path / "src")
    dst_paths = _make_paths(tmp_path / "dst")
    cfg = {
        "subscription-url": "https://example.com/sub?token=supersecret",
        "ai-manual-prefer": ["🇰🇷韩国 01"],
        "max-ai-multiplier": 2.5,
    }
    config_file(src_paths).write_text(yaml.safe_dump(cfg, allow_unicode=True), encoding="utf-8")
    add_custom_rule(src_paths, "internal.corp", "DIRECT", comment="内网直连")
    add_ban(src_paths, "us99", hours=24, reason="测试")

    bundle_data = export_bundle(src_paths, redact=True)
    assert bundle_data["redacted"] is True
    assert "supersecret" not in str(bundle_data["config"].get("subscription-url", ""))

    bundle_file = tmp_path / "bundle.yaml"
    bundle_file.write_text(yaml.safe_dump(bundle_data, allow_unicode=True), encoding="utf-8")

    stats = import_bundle(dst_paths, bundle_file)
    assert stats["custom_rules_imported"] == 1
    assert stats["bans_imported"] == 1
    restored_cfg = yaml.safe_load(config_file(dst_paths).read_text(encoding="utf-8"))
    # 已脱敏的 subscription-url 不会覆盖目标机，但非敏感项被同步
    assert "subscription-url" not in restored_cfg
    assert restored_cfg["max-ai-multiplier"] == 2.5
    assert len(load_custom_rules(dst_paths)) == 1
    assert len(load_active_bans(dst_paths)) == 1


def test_provider_benchmark_scorecard(tmp_path: Path, monkeypatch) -> None:
    paths = _make_paths(tmp_path)
    cfg = {
        "proxies": [
            {"name": "🇰🇷韩国 01 | IEPL | 1.5X", "type": "vless", "server": "1.1.1.1", "port": 443},
            {"name": "🇺🇸美国 01 | IEPL | 1.5X", "type": "vless", "server": "1.1.1.2", "port": 443},
            {"name": "🇬🇧英国 01 | 1X", "type": "vless", "server": "1.1.1.3", "port": 443},
            {"name": "🇸🇬新加坡 01 | 1X", "type": "vless", "server": "1.1.1.4", "port": 443},
        ],
    }
    config_file(paths).write_text(yaml.safe_dump(cfg, allow_unicode=True), encoding="utf-8")
    monkeypatch.setattr(
        "cproxy.services.benchmark.APIBackend.get_groups",
        lambda self, **kw: {
            "🇰🇷韩国 01 | IEPL | 1.5X": ProxyGroup("🇰🇷韩国 01 | IEPL | 1.5X", "VLESS", "-", [], delay=120, alive=True),
            "🇺🇸美国 01 | IEPL | 1.5X": ProxyGroup("🇺🇸美国 01 | IEPL | 1.5X", "VLESS", "-", [], delay=160, alive=True),
            "🇬🇧英国 01 | 1X": ProxyGroup("🇬🇧英国 01 | 1X", "VLESS", "-", [], delay=220, alive=True),
            "🇸🇬新加坡 01 | 1X": ProxyGroup("🇸🇬新加坡 01 | 1X", "VLESS", "-", [], delay=95, alive=True),
        },
    )

    cards = evaluate_providers(paths)
    assert len(cards) == 1
    sc = cards[0]
    assert sc.total_nodes == 4
    assert sc.alive_nodes == 4
    assert sc.dedicated_count == 2
    assert sc.grade in {"S", "A"}


def test_congested_1x_does_not_beat_low_latency_iepl_for_gemini() -> None:
    from cproxy.services.probe import (
        ProbeSummary,
        active_candidates_after_round,
        is_healthy_non_iepl_summary,
        probe_rank_key,
    )

    congested_1x = ProbeSummary(
        name="🇬🇧英国 01 | 1X",
        delays=(680, 770, 920),
        failures=0,
    )
    fast_iepl = ProbeSummary(
        name="🇰🇷韩国 01 | IEPL 1.7X | CM",
        delays=(150, 155, 175),
        failures=0,
    )
    healthy_1x = ProbeSummary(
        name="🇰🇷韩国 01 | 1X",
        delays=(280, 310, 340),
        failures=0,
    )

    assert is_healthy_non_iepl_summary(congested_1x) is False
    assert is_healthy_non_iepl_summary(healthy_1x) is True
    # 晚高峰拥塞的 1X (>550ms) 不得压制 160ms 的 IEPL 节点
    assert probe_rank_key(fast_iepl, prefer_non_iepl=True) < probe_rank_key(congested_1x, prefer_non_iepl=True)
    # 真正健康低延迟的 1X (<=550ms) 仍优先于 IEPL
    assert probe_rank_key(healthy_1x, prefer_non_iepl=True) < probe_rank_key(fast_iepl, prefer_non_iepl=True)
    kept = active_candidates_after_round(
        {
            "🇬🇧英国 01 | 1X": {"delays": [680, 770, 920], "failures": 0},
            "🇫🇷法国 01 | 1X": {"delays": [700, 780, 930], "failures": 0},
            "🇩🇪德国 01 | 1X": {"delays": [710, 790, 940], "failures": 0},
            "🇧🇷巴西 01 | 1X": {"delays": [720, 810, 950], "failures": 0},
            "🇹🇼台湾 01 | 1X": {"delays": [730, 820, 960], "failures": 0},
            "🇰🇷韩国 01 | IEPL 1.7X | CM": {"delays": [150, 155, 175], "failures": 0},
        },
        None,
        prefer_non_iepl=True,
    )
    assert "🇰🇷韩国 01 | IEPL 1.7X | CM" in kept


def test_detect_recent_agy_retry_failure_scans_all_concurrent_session_logs(tmp_path: Path) -> None:
    import os
    import time
    from datetime import datetime

    from cproxy.services.ai_watch import detect_recent_agy_retry_failure

    now_ts = time.time()
    ts_str = datetime.fromtimestamp(now_ts - 5).strftime("%m%d %H:%M:%S")
    older_session_log = tmp_path / "cli-20261018_181938.log"
    older_session_log.write_text(
        f"I{ts_str}.100000 12345 run.go:395] Run: attempt 2 failed "
        '(Post "https://daily-cloudcode-pa.googleapis.com/v1internal:streamGenerateContent?alt=sse": EOF), '
        "retrying in 2s...\n",
        encoding="utf-8",
    )
    newer_idle_log = tmp_path / "cli-20261018_213301.log"
    newer_idle_log.write_text(
        f"I{ts_str}.500000 99999 exp.go:10] Background poll listExperiments ok\n",
        encoding="utf-8",
    )
    os.utime(older_session_log, (now_ts - 5, now_ts - 5))
    os.utime(newer_idle_log, (now_ts - 1, now_ts - 1))

    hit, event_ts, reason = detect_recent_agy_retry_failure(
        older_session_log.parent,
        now=now_ts,
        window_seconds=60.0,
    )
    assert hit is True
    assert event_ts > 0
    assert "attempt 2" in reason
    assert "EOF" in reason


