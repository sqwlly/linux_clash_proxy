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


def test_healthy_1x_preferred_over_iepl_for_gemini_unless_severely_degraded() -> None:
    from cproxy.services.probe import (
        ProbeSummary,
        is_healthy_non_iepl_summary,
        probe_rank_key,
    )

    normal_ws_1x = ProbeSummary(
        name="🇬🇧英国 01 | 1X",
        delays=(580, 620, 680),
        failures=0,
    )
    severely_degraded_1x = ProbeSummary(
        name="🇫🇷法国 01 | 1X",
        delays=(1200, 1350, 1900),
        failures=0,
    )
    fast_iepl = ProbeSummary(
        name="🇰🇷韩国 01 | IEPL 1.7X | CM",
        delays=(150, 155, 175),
        failures=0,
    )

    # 零丢包、平均 <=1100ms 的 1X WebSocket 节点对 Gemini 长流式连接远比 50s connIdle 断流的 IEPL 稳定
    assert is_healthy_non_iepl_summary(normal_ws_1x) is True
    assert is_healthy_non_iepl_summary(severely_degraded_1x) is False
    assert probe_rank_key(normal_ws_1x, prefer_non_iepl=True) < probe_rank_key(fast_iepl, prefer_non_iepl=True)
    assert probe_rank_key(fast_iepl, prefer_non_iepl=True) < probe_rank_key(severely_degraded_1x, prefer_non_iepl=True)


def test_default_agy_log_dir_resolves_xdg_state_home(tmp_path: Path) -> None:
    from cproxy.config import AppPaths
    from cproxy.services.ai_watch import default_agy_log_dir

    home = tmp_path / "root"
    paths = AppPaths(
        config_dir=home / ".config" / "cproxy",
        data_dir=home / ".local" / "share" / "cproxy",
        state_dir=home / ".local" / "state" / "cproxy",
    )
    assert default_agy_log_dir(paths) == home / ".gemini" / "antigravity-cli" / "log"


def test_detect_recent_agy_retry_failure_scans_all_concurrent_session_logs(tmp_path: Path) -> None:
    import os
    import time
    from datetime import datetime

    from cproxy.services.ai_watch import detect_recent_agy_retry_failure

    now_ts = time.time()
    ts_str = datetime.fromtimestamp(now_ts - 5).strftime("%m%d %H:%M:%S")
    older_session_log = tmp_path / "cli-20261018_181938.log"
    # attempt 1/2 的普通单次 EOF 由 agy 自身 4s 重试消化，不触发切流雪崩
    older_session_log.write_text(
        f"I{ts_str}.100000 12345 run.go:395] Run: attempt 1 failed "
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

    hit_1, _, _ = detect_recent_agy_retry_failure(
        older_session_log.parent,
        now=now_ts,
        window_seconds=60.0,
    )
    assert hit_1 is False

    # 连续失败达到 attempt 3（或出现 tls: bad record MAC）时立即触发自愈
    older_session_log.write_text(
        f"I{ts_str}.100000 12345 run.go:395] Run: attempt 3 failed "
        '(Post "https://daily-cloudcode-pa.googleapis.com/v1internal:streamGenerateContent?alt=sse": EOF), '
        "retrying in 12s...\n",
        encoding="utf-8",
    )
    hit, event_ts, reason = detect_recent_agy_retry_failure(
        older_session_log.parent,
        now=now_ts,
        window_seconds=60.0,
    )
    assert hit is True
    assert event_ts > 0
    assert "attempt 3" in reason
    assert "EOF" in reason

    # 若后续已收到 streamGenerateContent ResponseID（重试已恢复并正在流式输出），绝不误触发切流掐断活跃流
    older_session_log.write_text(
        f"I{ts_str}.100000 12345 run.go:395] Run: attempt 3 failed "
        '(Post "https://daily-cloudcode-pa.googleapis.com/v1internal:streamGenerateContent?alt=sse": EOF), '
        "retrying in 12s...\n"
        f"I{ts_str}.900000 12345 http_helpers.go:349] URL: "
        "https://daily-cloudcode-pa.googleapis.com/v1internal:streamGenerateContent?alt=sse "
        "Trace: 0x1234 ResponseID: abcdef\n",
        encoding="utf-8",
    )
    hit_recovered, _, _ = detect_recent_agy_retry_failure(
        older_session_log.parent,
        now=now_ts,
        window_seconds=60.0,
    )
    assert hit_recovered is False

    # stream reading error: unexpected EOF 说明节点已建连成功拿到 200 ResponseID，不触发切节点
    older_session_log.write_text(
        f"I{ts_str}.100000 12345 run.go:395] Run: attempt 3 failed "
        "(stream reading error: unexpected EOF), retrying in 12s...\n",
        encoding="utf-8",
    )
    hit_stream_eof, _, _ = detect_recent_agy_retry_failure(
        older_session_log.parent,
        now=now_ts,
        window_seconds=60.0,
    )
    assert hit_stream_eof is False


def test_gemini_active_candidates_retains_all_healthy_1x_and_migrates_off_slow_fallback(tmp_path: Path) -> None:
    from types import SimpleNamespace

    from cproxy.backend.models import ProxyGroup
    from cproxy.config import ai_gemini_order_file
    from cproxy.services.ai_watch import AIWatchService, _promote_gemini_order_head
    from cproxy.services.probe import ProbeSummary, StabilityVerdict, active_candidates_after_round
    from cproxy.services.rebalance import RebalanceService

    # 1. 5 个健康 1X + 6 个 IEPL（共 11 个）：折半淘汰不应把排在第 4/5 名的健康 1X 提前淘汰
    results = {
        "🇰🇷韩国 01 | 1X": {"delays": [250, 260], "failures": 0},
        "🇬🇧英国 01 | 1X": {"delays": [380, 390], "failures": 0},
        "🇩🇪德国 01 | 1X": {"delays": [720, 740], "failures": 0},
        "🇫🇷法国 01 | 1X": {"delays": [760, 780], "failures": 0},
        "🇧🇷巴西 01 | 1X": {"delays": [840, 860], "failures": 0},
        "🇰🇷韩国 01 | IEPL 1.7X | CM": {"delays": [150, 160], "failures": 0},
        "🇬🇧英国 01 | IEPL 1.7X | CM": {"delays": [180, 190], "failures": 0},
    }
    kept = active_candidates_after_round(results, current=None, prefer_non_iepl=True)
    assert {
        "🇰🇷韩国 01 | 1X",
        "🇬🇧英国 01 | 1X",
        "🇩🇪德国 01 | 1X",
        "🇫🇷法国 01 | 1X",
        "🇧🇷巴西 01 | 1X",
    }.issubset(kept)

    # 2. 当 AI-GEMINI 滞留在高延迟 🇫🇷法国 01 | 1X (780ms) 时，应自动迁回 🇰🇷韩国 01 | 1X (260ms)；
    #    但若当前已在 🇬🇧英国 01 | 1X (350ms) 且与榜首差距 <180ms，保持 Sticky Active Head 不乱切。
    fr_slow_report = SimpleNamespace(
        current="🇫🇷法国 01 | 1X",
        current_verdict=StabilityVerdict(True, "5/5 稳定"),
        verdict=StabilityVerdict(True, "5/5 稳定"),
        summaries=[
            ProbeSummary("🇰🇷韩国 01 | 1X", (250, 260, 270, 255, 265), 0),
            ProbeSummary("🇫🇷法国 01 | 1X", (760, 780, 790, 770, 800), 0),
        ],
    )
    assert RebalanceService._should_switch_current(fr_slow_report, "🇰🇷韩国 01 | 1X", "AI-GEMINI") is True

    gb_close_report = SimpleNamespace(
        current="🇬🇧英国 01 | 1X",
        current_verdict=StabilityVerdict(True, "5/5 稳定"),
        verdict=StabilityVerdict(True, "5/5 稳定"),
        summaries=[
            ProbeSummary("🇰🇷韩国 01 | 1X", (260, 270, 280, 265, 275), 0),
            ProbeSummary("🇬🇧英国 01 | 1X", (350, 360, 370, 355, 365), 0),
        ],
    )
    assert RebalanceService._should_switch_current(gb_close_report, "🇰🇷韩国 01 | 1X", "AI-GEMINI") is False

    # 3. _pick_next 在所有 1X 均处于冷却期时，优先回收最早冷却的存活 1X，绝不落入 50s 必断的 IEPL
    paths = _make_paths(tmp_path)
    svc = AIWatchService(paths)
    now = 10_000.0
    groups = {
        "AI-GEMINI": ProxyGroup(
            name="AI-GEMINI",
            type="fallback",
            current="🇬🇧英国 01 | 1X",
            candidates=["🇰🇷韩国 01 | 1X", "🇬🇧英国 01 | 1X", "🇰🇷韩国 01 | IEPL 1.7X | CM"],
        ),
        "🇰🇷韩国 01 | 1X": ProxyGroup("🇰🇷韩国 01 | 1X", "VLESS", "-", [], delay=250, alive=True),
        "🇬🇧英国 01 | 1X": ProxyGroup("🇬🇧英国 01 | 1X", "VLESS", "-", [], delay=380, alive=True),
        "🇰🇷韩国 01 | IEPL 1.7X | CM": ProxyGroup("🇰🇷韩国 01 | IEPL 1.7X | CM", "VLESS", "-", [], delay=120, alive=True),
    }
    picked = svc._pick_next(
        groups,
        groups["AI-GEMINI"],
        "🇬🇧英国 01 | 1X",
        {"🇰🇷韩国 01 | 1X": now - 120.0},
        now,
    )
    assert picked == "🇰🇷韩国 01 | 1X"

    # 4. _promote_gemini_order_head 绝不把 IEPL 节点提拔到 ai-gemini-order.json 首位
    order_path = ai_gemini_order_file(paths)
    order_path.write_text('["🇰🇷韩国 01 | 1X", "🇰🇷韩国 01 | IEPL 1.7X | CM"]', encoding="utf-8")
    _promote_gemini_order_head(paths, "🇰🇷韩国 01 | IEPL 1.7X | CM")
    assert order_path.read_text(encoding="utf-8") == '["🇰🇷韩国 01 | 1X", "🇰🇷韩国 01 | IEPL 1.7X | CM"]'


def test_gemini_multi_session_retry_loop_and_connection_preservation(tmp_path: Path, monkeypatch) -> None:
    import os
    import time
    from datetime import datetime

    from cproxy.backend.models import ProxyGroup
    from cproxy.services import ai_watch as ai_watch_mod
    from cproxy.services.ai_watch import AIWatchService, detect_recent_agy_retry_failure
    from cproxy.services.diagnostics import PROBE_OK
    from cproxy.services.probe import ProbeSummary
    from cproxy.services.rebalance import _smooth_rank_key

    now_ts = time.time()
    fail_str = datetime.fromtimestamp(now_ts - 10).strftime("%m%d %H:%M:%S")
    ok_str = datetime.fromtimestamp(now_ts - 5).strftime("%m%d %H:%M:%S")

    # 1. 同一请求的长尾重试（attempt 4..10）不应每隔 3 分钟重复触发连环切流
    session_a = tmp_path / "cli-20261018_181938.log"
    session_a.write_text(
        f"I{fail_str}.100000 12345 run.go:395] Run: attempt 6 failed "
        '(Post "https://daily-cloudcode-pa.googleapis.com/v1internal:streamGenerateContent?alt=sse": EOF), '
        "retrying in 60s...\n",
        encoding="utf-8",
    )
    os.utime(session_a, (now_ts - 10, now_ts - 10))
    hit_tail, _, _ = detect_recent_agy_retry_failure(tmp_path, now=now_ts, window_seconds=60.0)
    assert hit_tail is False

    # 2. 当会话 A 刚抵达 attempt 3，但并发会话 B 在更晚时间已成功拿到 ResponseID 时，证明当前节点存活，不切全局节点
    session_a.write_text(
        f"I{fail_str}.100000 12345 run.go:395] Run: attempt 3 failed "
        '(Post "https://daily-cloudcode-pa.googleapis.com/v1internal:streamGenerateContent?alt=sse": EOF), '
        "retrying in 12s...\n",
        encoding="utf-8",
    )
    session_b = tmp_path / "cli-20261018_204342.log"
    session_b.write_text(
        f"I{ok_str}.200000 54321 http_helpers.go:349] URL: "
        "https://daily-cloudcode-pa.googleapis.com/v1internal:streamGenerateContent?alt=sse "
        "Trace: 0x9999 ResponseID: alive_on_kr\n",
        encoding="utf-8",
    )
    os.utime(session_a, (now_ts - 10, now_ts - 10))
    os.utime(session_b, (now_ts - 5, now_ts - 5))
    hit_cross, _, _ = detect_recent_agy_retry_failure(tmp_path, now=now_ts, window_seconds=60.0)
    assert hit_cross is False

    # 3. 当 AI-GEMINI 已在健康的 1X 节点（如 🇰🇷韩国 01 | 1X）且主动探测 PROBE_OK 时，EOF 日志不误杀切走
    session_b.unlink()
    paths = _make_paths(tmp_path / "app")
    groups = {
        "AI-MANUAL": ProxyGroup("AI-MANUAL", "fallback", "🇰🇷韩国 01 | 1X", ["🇰🇷韩国 01 | 1X"]),
        "AI-GEMINI": ProxyGroup("AI-GEMINI", "fallback", "🇰🇷韩国 01 | 1X", ["🇰🇷韩国 01 | 1X", "🇬🇧英国 01 | 1X"]),
        "🇰🇷韩国 01 | 1X": ProxyGroup("🇰🇷韩国 01 | 1X", "VLESS", "-", [], delay=250, alive=True),
        "🇬🇧英国 01 | 1X": ProxyGroup("🇬🇧英国 01 | 1X", "VLESS", "-", [], delay=380, alive=True),
    }
    switch_calls: list[tuple[str, str, bool]] = []

    class _FakeQuery:
        def __init__(self):
            self.api = type("_API", (), {"get_groups": lambda _self: groups})()

        def switch_group(self, group: str, target: str, *, drop_connections: bool = True) -> None:
            switch_calls.append((group, target, drop_connections))

    monkeypatch.setattr(ai_watch_mod, "ai_target_verdict", lambda _p, _url: PROBE_OK)
    svc = AIWatchService(paths, agy_log_dir=tmp_path)
    svc.query = _FakeQuery()  # type: ignore[assignment]
    report = svc.watch()
    gemini_res = next(r for r in report.results if r.group == "AI-GEMINI")
    assert gemini_res.switched is False
    assert switch_calls == []

    # 4. _smooth_rank_key 与 probe_rank_key 在健康 1X 节点中始终将亚太/英国（KR/GB）排在欧陆/南美（DE/FR/BR）之前
    from cproxy.services.probe import is_healthy_non_iepl_summary, probe_rank_key
    from cproxy.services.rebalance import _should_keep_previous_order

    kr_loaded = ProbeSummary("🇰🇷韩国 01 | 1X", (520, 560, 580), 0)
    br_idle = ProbeSummary("🇧🇷巴西 01 | 1X", (480, 490, 500), 0)
    fr_slow = ProbeSummary("🇫🇷法国 01 | 1X", (761, 1648), 0)
    assert _smooth_rank_key(kr_loaded, prefer_non_iepl=True) < _smooth_rank_key(br_idle, prefer_non_iepl=True)
    assert probe_rank_key(kr_loaded, prefer_non_iepl=True) < probe_rank_key(br_idle, prefer_non_iepl=True)
    assert is_healthy_non_iepl_summary(fr_slow) is True

    # 5. 前 2 名主备不变且前 5 名集合相同（仅第 3~5 名互换）时，不触发无意义热重载
    prev_order = ["N1", "N2", "N3", "N4", "N5", "N6"]
    curr_order = ["N1", "N2", "N5", "N3", "N4", "N6"]
    assert _should_keep_previous_order(prev_order, curr_order) is True


def test_multi_airport_subscription_import_compatibility(tmp_path: Path, monkeypatch) -> None:
    import base64

    from cproxy.config import ai_gemini_order_file, ai_manual_order_file, ai_watch_state_file, read_config
    from cproxy.services import refresh as refresh_mod
    from cproxy.services.ai_watch import _is_stream_safe_gemini_leaf
    from cproxy.services.nodelist import match_region, parse_subscription_payload
    from cproxy.services.probe import is_iepl_node
    from cproxy.services.refresh import update_source_from_subscription

    # 1. 繁体地名、城市名与数据中心三字码识别
    assert match_region("🇰🇷 首尔 01") == "KR"
    assert match_region("🇹🇼 台灣 Hinet 01") == "TW"
    assert match_region("🇸🇬 獅城 BGP 02") == "SG"
    assert match_region("🇺🇸 洛杉矶 LAX 01") == "US"
    assert match_region("🇩🇪 法兰克福 01") == "DE"
    assert match_region("🇬🇧 伦敦 01") == "GB"

    # 2. vmess://、legacy ss://、hy2://、anytls:// 分享链接订阅解析
    vmess_json = base64.b64encode(
        json.dumps(
            {
                "v": "2",
                "ps": "🇰🇷 首尔 VMess 01",
                "add": "kr.example.com",
                "port": "443",
                "id": "11111111-2222-3333-4444-555555555555",
                "aid": "0",
                "net": "ws",
                "path": "/ws",
                "host": "kr.example.com",
                "tls": "tls",
            }
        ).encode("utf-8")
    ).decode("ascii")
    legacy_ss = base64.b64encode(b"aes-256-gcm:pass123@us.example.com:8388").decode("ascii")
    share_lines = "\n".join(
        [
            f"vmess://{vmess_json}",
            f"ss://{legacy_ss}#%F0%9F%87%BA%F0%9F%87%B8%20%E6%B4%9B%E6%9D%89%E7%9F%B6%2001",
            "hy2://hypass@tw.example.com:443?sni=tw.example.com#%F0%9F%87%B9%F0%9F%87%BC%20%E5%8F%B0%E5%8C%97%2001",
            "anytls://anypass@jp.example.com:443?sni=jp.example.com#%F0%9F%87%AF%F0%9F%87%B5%20%E4%B8%9C%E4%BA%AC%2001",
        ]
    )
    parsed = parse_subscription_payload(base64.b64encode(share_lines.encode("utf-8")))
    assert [p["type"] for p in parsed["proxies"]] == ["vmess", "ss", "hysteria2", "anytls"]
    assert parsed["proxies"][0]["network"] == "ws"

    # 3. 从机场 A (CyberGuard) 切换到机场 B (FlowerCloud/Proxy，且仅有 US/KR/TW 无 SG)：
    #    - 彻底清理机场 A 的旧主组 CyberGuard 与共享 uuid_A 的旧节点
    #    - 保留自建节点 Vultr 与附加订阅 Mitce，并自动迁移挂载到新主组 Proxy
    #    - 自动清理 ai-gemini-order.json / ai-manual-order.json / ai-watch.json 中的机场 A 旧节点
    #    - render_runtime 在缺少 SG 节点时不崩溃，且不带 "1X" 标签的 🇰🇷 首尔 01 被识别为抗断流安全叶子
    paths = _make_paths(tmp_path / "switch")
    uuid_a = "aaaa1111-2222-3333-4444-555555555555"
    pw_b = "flowercloud-shared-pw"
    write_cfg = {
        "mixed-port": 7890,
        "subscriptions": [{"name": "Mitce", "url": "https://mitce.example.com/sub", "attach-to": "CyberGuard"}],
        "proxies": [
            {"name": "🇭🇰香港 01 | IEPL", "type": "vless", "server": "hk.a.com", "port": 443, "uuid": uuid_a},
            {"name": "🇰🇷韩国 01 | 1X", "type": "vless", "server": "kr.a.com", "port": 443, "uuid": uuid_a},
            {"name": "🇺🇸美国 01 | 1X", "type": "vless", "server": "us.a.com", "port": 443, "uuid": uuid_a},
            {"name": "🇯🇵日本 Vultr-Reality", "type": "vless", "server": "45.32.47.247", "port": 443, "uuid": "custom-uuid"},
            {"name": "Mitce HK-1", "type": "hysteria2", "server": "hk.mitce.com", "port": 443, "password": "m-pw"},
        ],
        "proxy-groups": [
            {
                "name": "CyberGuard",
                "type": "select",
                "proxies": ["Auto", "🇭🇰香港 01 | IEPL", "🇰🇷韩国 01 | 1X", "🇯🇵日本 Vultr-Reality", "Mitce", "DIRECT"],
            },
            {"name": "Auto", "type": "url-test", "proxies": ["🇭🇰香港 01 | IEPL", "🇰🇷韩国 01 | 1X"]},
            {"name": "Mitce-HK", "type": "url-test", "proxies": ["Mitce HK-1"]},
            {"name": "Mitce", "type": "select", "proxies": ["Mitce-HK"]},
        ],
        "rules": ["MATCH,CyberGuard"],
    }
    config_file(paths).write_text(yaml.safe_dump(write_cfg, allow_unicode=True, sort_keys=False), encoding="utf-8")
    ai_gemini_order_file(paths).write_text(json.dumps(["🇰🇷韩国 01 | 1X", "🇰🇷 首尔 01"]), encoding="utf-8")
    ai_manual_order_file(paths).write_text(json.dumps(["🇺🇸美国 01 | 1X", "🇺🇸 洛杉矶 LAX 01"]), encoding="utf-8")
    ai_watch_state_file(paths).write_text(
        json.dumps({"AI-GEMINI": {"last_active": "🇰🇷韩国 01 | 1X", "cooldown": {"🇰🇷韩国 01 | 1X": 123.0}}}),
        encoding="utf-8",
    )

    airport_b_payload = {
        "proxies": [
            {"name": "🇰🇷 首尔 01", "type": "hysteria2", "server": "kr.b.com", "port": 443, "password": pw_b},
            {"name": "[IPLC] 韓國 02", "type": "ss", "server": "kr2.b.com", "port": 8388, "password": pw_b},
            {"name": "🇹🇼 台灣 Hinet 01", "type": "hysteria2", "server": "tw.b.com", "port": 443, "password": pw_b},
            {"name": "🇺🇸 洛杉矶 LAX 01", "type": "vless", "server": "us.b.com", "port": 443, "uuid": pw_b, "network": "ws"},
        ],
        "proxy-groups": [
            {
                "name": "Proxy",
                "type": "select",
                "proxies": ["自动选择", "🇰🇷 首尔 01", "[IPLC] 韓國 02", "🇹🇼 台灣 Hinet 01", "🇺🇸 洛杉矶 LAX 01", "DIRECT"],
            },
            {
                "name": "自动选择",
                "type": "url-test",
                "proxies": ["🇰🇷 首尔 01", "🇹🇼 台灣 Hinet 01", "🇺🇸 洛杉矶 LAX 01"],
            },
        ],
        "rules": ["MATCH,Proxy"],
    }
    raw_b = yaml.safe_dump(airport_b_payload, allow_unicode=True, sort_keys=False).encode("utf-8")
    monkeypatch.setattr(refresh_mod, "_download_subscription", lambda _p, _u, _t=10: (raw_b, None))

    update_source_from_subscription(paths, "https://airport-b.example.com/sub")
    merged = read_config(paths)
    merged_names = [p["name"] for p in merged["proxies"]]
    merged_groups = {g["name"]: g for g in merged["proxy-groups"]}

    assert "🇭🇰香港 01 | IEPL" not in merged_names
    assert "🇰🇷韩国 01 | 1X" not in merged_names
    assert "CyberGuard" not in merged_groups
    assert "🇯🇵日本 Vultr-Reality" in merged_names
    assert "Mitce HK-1" in merged_names
    assert "Mitce" in merged_groups
    assert "🇯🇵日本 Vultr-Reality" in merged_groups["Proxy"]["proxies"]
    assert "Mitce" in merged_groups["Proxy"]["proxies"]
    assert merged["subscriptions"][0]["attach-to"] == "Proxy"

    # 旧机场节点已从排序与看门狗状态文件中剔除
    assert json.loads(ai_gemini_order_file(paths).read_text(encoding="utf-8")) == ["🇰🇷 首尔 01"]
    assert json.loads(ai_manual_order_file(paths).read_text(encoding="utf-8")) == ["🇺🇸 洛杉矶 LAX 01"]
    watch_state = json.loads(ai_watch_state_file(paths).read_text(encoding="utf-8"))
    assert watch_state["AI-GEMINI"]["last_active"] is None
    assert watch_state["AI-GEMINI"]["cooldown"] == {}

    # 缺少 SG 节点时 render_runtime 优雅回退，且 IPLC 排在非专线节点之后
    runtime_path = RuntimeBackend(paths).render_runtime()
    rendered = yaml.safe_load(runtime_path.read_text(encoding="utf-8"))
    r_groups = {g["name"]: g["proxies"] for g in rendered["proxy-groups"]}
    assert r_groups["AI-GEMINI"][0] == "🇰🇷 首尔 01"
    assert r_groups["AI-GEMINI"].index("🇰🇷 首尔 01") < r_groups["AI-GEMINI"].index("[IPLC] 韓國 02")
    assert is_iepl_node("[IPLC] 韓國 02") is True
    assert _is_stream_safe_gemini_leaf(paths, "🇰🇷 首尔 01") is True
    assert _is_stream_safe_gemini_leaf(paths, "[IPLC] 韓國 02") is False


def test_ws_cdn_server_optimization_and_apac_uk_tier_stability() -> None:
    from types import SimpleNamespace

    from cproxy.backend.runtime import _optimize_ws_cdn_servers
    from cproxy.services.probe import ProbeSummary, StabilityVerdict
    from cproxy.services.rebalance import RebalanceService

    proxies = [
        {
            "name": "🇰🇷韩国 01 | 1X",
            "type": "vless",
            "server": "cyberguard.cyberguard-u1s1.art",
            "port": 443,
            "servername": "cyberguard-u1s1.art",
            "network": "ws",
            "ws-opts": {"path": "/cgkr1", "headers": {"Host": "cgkr1.cyberguard-u1s1.art"}},
        },
        {
            "name": "🇭🇰香港 01 | 1X | CMI 01",
            "type": "vless",
            "server": "ch-jp2.599789.xyz",
            "port": 43229,
            "servername": "cghk1.xn--download-ne4yj89e.com",
            "network": "ws",
            "ws-opts": {"path": "/cghk1", "headers": {"Host": "cghk1.xn--download-ne4yj89e.com"}},
        },
    ]
    _optimize_ws_cdn_servers(proxies)
    assert proxies[0]["server"] == "cgkr1.cyberguard-u1s1.art"
    assert proxies[1]["server"] == "ch-jp2.599789.xyz"

    # 当现役节点已在 GEMINI_FAST_REGIONS（如 🇰🇷韩国 01 | 1X）且健康时，即使承载大包上传导致探测延迟偏高 (613ms)，
    # 也不应因与空闲 🇬🇧英国 01 | 1X (376ms) 差距 >=180ms 而每 30 分钟乒乓切走
    kr_loaded = ProbeSummary("🇰🇷韩国 01 | 1X", (580, 620, 640), 0)
    gb_idle = ProbeSummary("🇬🇧英国 01 | 1X", (360, 380, 390), 0)
    kr_loaded_report = SimpleNamespace(
        current="🇰🇷韩国 01 | 1X",
        current_verdict=StabilityVerdict(True, "5/5 稳定"),
        verdict=StabilityVerdict(True, "5/5 稳定"),
        summaries=[gb_idle, kr_loaded],
    )
    assert RebalanceService._should_switch_current(kr_loaded_report, "🇬🇧英国 01 | 1X", "AI-GEMINI") is False

    # 通用 AI 出口（AI-MANUAL：OpenAI/Codex/Claude）同样优先健康 1X 节点并在滞留 IEPL 时主动迁出
    us_1x = ProbeSummary("🇺🇸美国 01 | 1X", (320, 330, 340), 0)
    gb_iepl = ProbeSummary("🇬🇧英国 01 | IEPL 1.7X | CM", (180, 190, 200), 0)
    manual_iepl_report = SimpleNamespace(
        current="🇬🇧英国 01 | IEPL 1.7X | CM",
        current_verdict=StabilityVerdict(True, "5/5 稳定"),
        verdict=StabilityVerdict(True, "5/5 稳定"),
        summaries=[gb_iepl, us_1x],
    )
    assert RebalanceService._should_switch_current(manual_iepl_report, "🇺🇸美国 01 | 1X", "AI-MANUAL") is True
    manual_sticky_report = SimpleNamespace(
        current="🇺🇸美国 01 | 1X",
        current_verdict=StabilityVerdict(True, "5/5 稳定"),
        verdict=StabilityVerdict(True, "5/5 稳定"),
        summaries=[kr_loaded, gb_idle, us_1x],
    )
    assert RebalanceService._should_switch_current(manual_sticky_report, "🇬🇧英国 01 | 1X", "AI-MANUAL") is False

    # 当 1X 节点带 80..199ms 历史惩罚但本轮 5/5 零失败（或 4/5 多数成功）时，仍必须排在 54s 断流的 IEPL 之前
    from cproxy.services.rebalance import _smooth_rank_key

    kr_1x_hist89 = ProbeSummary("🇰🇷韩国 01 | 1X", (500, 520, 540, 510, 530), 0, history_penalty_ms=89)
    kr_1x_4of5 = ProbeSummary("🇬🇧英国 01 | 1X", (320, 340, 350, 330), 1, history_penalty_ms=40)
    jp_iepl_hist71 = ProbeSummary("🇯🇵日本 01 | IEPL + UDP 1.7X | CM", (350, 360, 370, 380, 390), 0, history_penalty_ms=71)
    assert _smooth_rank_key(kr_1x_hist89, prefer_non_iepl=True) < _smooth_rank_key(jp_iepl_hist71, prefer_non_iepl=True)
    assert _smooth_rank_key(kr_1x_4of5, prefer_non_iepl=True) < _smooth_rank_key(jp_iepl_hist71, prefer_non_iepl=True)


def test_airport_without_iepl_labels_protocol_and_dynamic_stream_fragility(tmp_path: Path, monkeypatch) -> None:
    import time

    from cproxy.backend.models import ProxyGroup
    from cproxy.config import config_file
    from cproxy.services import ai_watch as ai_watch_mod
    from cproxy.services.ai_watch import AIWatchService, _is_stream_safe_gemini_leaf
    from cproxy.services.diagnostics import PROBE_OK
    from cproxy.services.probe import (
        ProbeSummary,
        active_candidates_after_round,
        is_iepl_node,
        probe_paths_context,
    )
    from cproxy.services.rebalance import _smooth_rank_key

    paths = _make_paths(tmp_path / "no-iepl-airport")
    cfg = {
        "mixed-port": 7890,
        "proxies": [
            {"name": "🇯🇵日本 01 | TE", "type": "vless", "flow": "xtls-rprx-vision", "server": "1.1.1.1", "port": 443},
            {"name": "🇯🇵日本 01 | 1X | 推荐", "type": "vless", "flow": "xtls-rprx-vision", "server": "1.1.1.2", "port": 443},
            {"name": "🇯🇵 Tokyo 01", "type": "vless", "network": "ws", "server": "1.1.1.3", "port": 443},
            {"name": "🇸🇬 Singapore 01", "type": "vless", "network": "ws", "server": "1.1.1.4", "port": 443},
        ],
    }
    config_file(paths).write_text(yaml.safe_dump(cfg, allow_unicode=True), encoding="utf-8")

    # 1. 即便节点名称不含 IEPL（甚至含 1X），只要底层协议是裸 TCP xtls-rprx-vision，统一判为易断流节点
    assert is_iepl_node("🇯🇵日本 01 | TE", paths) is True
    assert is_iepl_node("🇯🇵日本 01 | 1X | 推荐", paths) is True
    assert _is_stream_safe_gemini_leaf(paths, "🇯🇵日本 01 | 1X | 推荐") is False
    assert is_iepl_node("🇯🇵 Tokyo 01", paths) is False
    assert _is_stream_safe_gemini_leaf(paths, "🇯🇵 Tokyo 01") is True

    # 2. 完全无 IEPL 标签的机场：当 🇯🇵 Tokyo 01 连续出现 attempt 4 流式重试失败时，
    #    看门狗立即切往 🇸🇬 Singapore 01、断开陈旧连接，并把 🇯🇵 Tokyo 01 记入动态断流表（stream_fragile）
    log_dir = tmp_path / "agy-logs"
    log_dir.mkdir()
    now_ts = time.time()
    lt = time.localtime(now_ts - 2.0)
    log_line = (
        f"I{lt.tm_mon:02d}{lt.tm_mday:02d} {lt.tm_hour:02d}:{lt.tm_min:02d}:{lt.tm_sec:02d}.123456 "
        '99999 run.go:395] Run: attempt 4 failed (Post "https://daily-cloudcode-pa.googleapis.com/'
        'v1internal:streamGenerateContent?alt=sse": EOF), retrying in 20s...\n'
    )
    (log_dir / "cli-20261019.log").write_text(log_line, encoding="utf-8")

    groups = {
        "AI-MANUAL": ProxyGroup("AI-MANUAL", "fallback", "🇯🇵日本 01 | TE", ["🇯🇵日本 01 | TE", "🇸🇬 Singapore 01"]),
        "AI-GEMINI": ProxyGroup("AI-GEMINI", "fallback", "🇯🇵 Tokyo 01", ["🇯🇵 Tokyo 01", "🇸🇬 Singapore 01"]),
        "🇯🇵日本 01 | TE": ProxyGroup("🇯🇵日本 01 | TE", "VLESS", "-", [], delay=180, alive=True),
        "🇯🇵 Tokyo 01": ProxyGroup("🇯🇵 Tokyo 01", "VLESS", "-", [], delay=200, alive=True),
        "🇸🇬 Singapore 01": ProxyGroup("🇸🇬 Singapore 01", "VLESS", "-", [], delay=220, alive=True),
    }
    switches: list[tuple[str, str, bool]] = []

    class _Query:
        def __init__(self):
            self.api = type("_API", (), {"get_groups": lambda _self: groups})()

        def switch_group(self, grp: str, target: str, *, drop_connections: bool = True) -> None:
            switches.append((grp, target, drop_connections))

    monkeypatch.setattr(ai_watch_mod, "ai_target_verdict", lambda _p, _u: PROBE_OK)
    svc = AIWatchService(paths, agy_log_dir=log_dir)
    svc.query = _Query()  # type: ignore[assignment]
    report = svc.watch()

    # AI-MANUAL 从裸 vision 的 🇯🇵日本 01 | TE 主动迁往 🇸🇬 Singapore 01；
    # AI-GEMINI 因 attempt 4 连续失败从 🇯🇵 Tokyo 01 切往 🇸🇬 Singapore 01 并断开旧连接
    assert ("AI-MANUAL", "🇸🇬 Singapore 01", True) in switches
    assert ("AI-GEMINI", "🇸🇬 Singapore 01", True) in switches
    assert is_iepl_node("🇯🇵 Tokyo 01", paths) is True

    with probe_paths_context(paths):
        tokyo_summary = ProbeSummary("🇯🇵 Tokyo 01", (180, 190, 200), 0)
        sg_summary = ProbeSummary("🇸🇬 Singapore 01", (240, 250, 260), 0)
        assert _smooth_rank_key(sg_summary, prefer_non_iepl=True) < _smooth_rank_key(
            tokyo_summary, prefer_non_iepl=True
        )

    # 3. 逐轮筛选（active_candidates_after_round）绝不因第 2 轮偶发 1 次超时而把存活的非易断流节点提前淘汰
    round2_results = {
        "🇸🇬 Singapore 01": {"delays": [240], "failures": 1},
        "🇯🇵日本 01 | IEPL": {"delays": [150, 160], "failures": 0},
        "🇰🇷韩国 01 | IEPL": {"delays": [170, 180], "failures": 0},
        "🇺🇸美国 01 | IEPL": {"delays": [190, 200], "failures": 0},
    }
    kept = active_candidates_after_round(round2_results, current=None, prefer_non_iepl=True)
    assert "🇸🇬 Singapore 01" in kept


def test_ws_cdn_tls_blackhole_filter_play_log_reject_and_cross_session_retry(
    tmp_path: Path, monkeypatch
) -> None:
    import os
    import socket
    import time
    import yaml
    from cproxy.backend import runtime as runtime_mod
    from cproxy.backend.runtime import RuntimeBackend, _optimize_ws_cdn_servers
    from cproxy.config import config_file, runtime_file
    from cproxy.services.ai_watch import detect_recent_agy_retry_failure

    # 1. 多 A 记录 CDN 域名存在 TLS 黑洞（104.21.87.131 超时 None，172.67.143.95 415ms）时，
    #    _optimize_ws_cdn_servers(probe_tls=True) 自动将 server 钉选到健康 IP
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda host, port, *_a, **_kw: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("104.21.87.131", port)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("172.67.143.95", port)),
        ],
    )
    monkeypatch.setattr(
        runtime_mod,
        "_tls_handshake_latency_ms",
        lambda ip, _port, _sni, timeout=1.5: None if ip == "104.21.87.131" else 415,
    )
    proxies = [
        {
            "name": "🇰🇷韩国 01 | 1X",
            "type": "vless",
            "server": "cyberguard.cyberguard-u1s1.art",
            "port": 443,
            "servername": "cyberguard-u1s1.art",
            "network": "ws",
            "ws-opts": {"path": "/cgkr1", "headers": {"Host": "cgkr1.cyberguard-u1s1.art"}},
        }
    ]
    _optimize_ws_cdn_servers(proxies, probe_tls=True)
    assert proxies[0]["server"] == "172.67.143.95"

    # 2. render_runtime 在 googleapis.com 之前注入 DOMAIN,play.googleapis.com,REJECT，防止遥测日志占满 AI-GEMINI 上行
    paths = _make_paths(tmp_path / "play-reject")
    cfg = {
        "mixed-port": 7890,
        "proxies": [
            {"name": "🇺🇸美国 01 | 1X", "type": "vless", "server": "1.1.1.1", "port": 443},
            {"name": "🇸🇬新加坡 01 | 1X", "type": "vless", "server": "1.1.1.2", "port": 443},
            {"name": "🇰🇷韩国 01 | 1X", "type": "vless", "server": "1.1.1.3", "port": 443},
        ],
        "proxy-groups": [{"name": "PROXY", "type": "select", "proxies": ["🇺🇸美国 01 | 1X"]}],
        "rules": ["MATCH,PROXY"],
    }
    config_file(paths).write_text(yaml.safe_dump(cfg, allow_unicode=True), encoding="utf-8")
    RuntimeBackend(paths).render_runtime()
    rendered = yaml.safe_load(runtime_file(paths).read_text(encoding="utf-8"))
    rules = rendered["rules"]
    assert "DOMAIN,play.googleapis.com,REJECT" in rules
    assert rules.index("DOMAIN,play.googleapis.com,REJECT") < rules.index(
        "DOMAIN-SUFFIX,googleapis.com,AI-GEMINI"
    )

    # 3. 并发多会话下：会话 A 正在发生 attempt 4 大包上传失败，即便会话 B 在 1 秒后有小包 sir_flash_lite
    #    收到 ResponseID，detect_recent_agy_retry_failure 仍必须捕获会话 A 的严重失败（attempt == 4）
    log_dir = tmp_path / "multi-agy-logs"
    log_dir.mkdir()
    now_ts = time.time()
    lt_fail = time.localtime(now_ts - 5.0)
    lt_ok = time.localtime(now_ts - 2.0)
    log_a = log_dir / "cli-20261019_122739.log"
    log_b = log_dir / "cli-20261018_204342.log"
    log_a.write_text(
        f"I{lt_fail.tm_mon:02d}{lt_fail.tm_mday:02d} {lt_fail.tm_hour:02d}:{lt_fail.tm_min:02d}:{lt_fail.tm_sec:02d}.100000 "
        '110261 run.go:395] Run: attempt 4 failed (request failed: Post "https://daily-cloudcode-pa.googleapis.com/'
        'v1internal:streamGenerateContent?alt=sse": EOF), retrying in 23s\n',
        encoding="utf-8",
    )
    log_b.write_text(
        f"I{lt_ok.tm_mon:02d}{lt_ok.tm_mday:02d} {lt_ok.tm_hour:02d}:{lt_ok.tm_min:02d}:{lt_ok.tm_sec:02d}.500000 "
        "1019535 http_helpers.go:349] URL: https://daily-cloudcode-pa.googleapis.com/v1internal:"
        "streamGenerateContent?alt=sse Trace: 0x123 ResponseID: abc\n",
        encoding="utf-8",
    )
    os.utime(log_a, (now_ts - 5.0, now_ts - 5.0))
    os.utime(log_b, (now_ts - 2.0, now_ts - 2.0))
    failed, _ts, reason = detect_recent_agy_retry_failure(log_dir, now=now_ts)
    assert failed is True
    assert reason.startswith("attempt 4:")

