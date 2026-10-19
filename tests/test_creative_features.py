from __future__ import annotations

import time
from pathlib import Path

import yaml

from cproxy.backend.models import ProxyGroup
from cproxy.backend.runtime import (
    RuntimeBackend,
    _prefer_non_iepl_nodes,
    is_stream_fragile_proxy,
)
from cproxy.config import AppPaths, config_file, default_paths
from cproxy.doctor import _subscription_doctor_check
from cproxy.services import ai_watch as ai_watch_mod
from cproxy.services.ai_watch import AIWatchService, detect_recent_agy_retry_failure
from cproxy.services.diagnostics import PROBE_OK
from cproxy.services.ops import get_active_ai_processes
from cproxy.services.subscription_info import (
    MAIN_SUBSCRIPTION_KEY,
    load_subscription_info,
    parse_panel_info_nodes,
    record_subscription_info,
)


def _paths(tmp_path: Path) -> AppPaths:
    paths = default_paths(tmp_path)
    paths.config_dir.mkdir(parents=True, exist_ok=True)
    paths.data_dir.mkdir(parents=True, exist_ok=True)
    paths.state_dir.mkdir(parents=True, exist_ok=True)
    return paths


def test_feature1_agy_log_retry_triggers_instant_first_attempt_failover(tmp_path, monkeypatch):
    paths = _paths(tmp_path)
    log_dir = tmp_path / "agy-logs"
    log_dir.mkdir()
    now_ts = time.time()
    lt = time.localtime(now_ts - 2.0)
    log_line = (
        f"I{lt.tm_mon:02d}{lt.tm_mday:02d} {lt.tm_hour:02d}:{lt.tm_min:02d}:{lt.tm_sec:02d}.123456 "
        "99999 run.go:336] Run: attempt 3 failed (Post \"https://daily-cloudcode-pa.googleapis.com/"
        "v1internal:streamGenerateContent?alt=sse\": EOF), retrying in 451ms...\n"
    )
    (log_dir / "cli-20261018.log").write_text(log_line, encoding="utf-8")

    hit, event_ts, reason = detect_recent_agy_retry_failure(log_dir, now=now_ts)
    assert hit is True
    assert event_ts > 0
    assert "attempt 3" in reason

    groups = {
        "AI-MANUAL": ProxyGroup(name="AI-MANUAL", type="Fallback", current="US-01", candidates=["US-01", "SG-01"]),
        "AI-GEMINI": ProxyGroup(name="AI-GEMINI", type="Fallback", current="KR-01", candidates=["KR-01", "DE-01"]),
        "US-01": ProxyGroup(name="US-01", type="Shadowsocks", current="-", candidates=[], delay=80, alive=True),
        "SG-01": ProxyGroup(name="SG-01", type="Shadowsocks", current="-", candidates=[], delay=90, alive=True),
        "KR-01": ProxyGroup(name="KR-01", type="Shadowsocks", current="-", candidates=[], delay=70, alive=True),
        "DE-01": ProxyGroup(name="DE-01", type="Shadowsocks", current="-", candidates=[], delay=110, alive=True),
    }
    switches: list[tuple[str, str]] = []

    class _FakeQuery:
        def __init__(self):
            self.api = type("_API", (), {"get_groups": lambda _self: groups})()

        def switch_group(self, group: str, target: str) -> None:
            switches.append((group, target))

    # 即便短包 HTTP 探测仍返回 PROBE_OK，客户端日志里的首次重试也应立即触发 AI-GEMINI 切换
    monkeypatch.setattr(ai_watch_mod, "ai_target_verdict", lambda p, url: PROBE_OK)
    svc = AIWatchService(paths, agy_log_dir=log_dir)
    svc.query = _FakeQuery()  # type: ignore[assignment]
    report = svc.watch()
    gemini_res = next(r for r in report.results if r.group == "AI-GEMINI")
    assert gemini_res.switched is True
    assert gemini_res.target == "DE-01"
    assert switches == [("AI-GEMINI", "DE-01")]

    # 同一条日志时间戳已被记录，第二次巡检不会重复切
    switches.clear()
    svc2 = AIWatchService(paths, agy_log_dir=log_dir)
    svc2.query = _FakeQuery()  # type: ignore[assignment]
    report2 = svc2.watch()
    gemini_res2 = next(r for r in report2.results if r.group == "AI-GEMINI")
    assert gemini_res2.switched is False
    assert switches == []


def test_feature2_protocol_fingerprint_and_cross_provider_ai_fallback(tmp_path):
    # 即使节点名不含 IEPL，裸 TCP + xtls-rprx-vision 也会被识别为易断流节点排在 ws/grpc 后
    vision_tcp = {"name": "🇰🇷韩国 01 | 专线", "type": "vless", "flow": "xtls-rprx-vision", "network": "tcp"}
    ws_node = {"name": "🇰🇷韩国 02 | 标准", "type": "vless", "network": "ws"}
    assert is_stream_fragile_proxy(vision_tcp) is True
    assert is_stream_fragile_proxy(ws_node) is False
    proxy_map = {p["name"]: p for p in (vision_tcp, ws_node)}
    assert _prefer_non_iepl_nodes([vision_tcp["name"], ws_node["name"]], proxy_map) == [
        ws_node["name"],
        vision_tcp["name"],
    ]

    paths = _paths(tmp_path)
    cfg = {
        "mixed-port": 7890,
        "subscriptions": [{"name": "BackupSub", "url": "https://example.com/sub"}],
        "proxies": [
            {"name": "🇺🇸美国 01 | 1X", "type": "ss", "server": "1.1.1.1", "port": 443},
            {"name": "🇸🇬新加坡 01 | 1X", "type": "ss", "server": "1.1.1.2", "port": 443},
            {"name": "🇰🇷韩国 01 | 专线", "type": "vless", "flow": "xtls-rprx-vision", "network": "tcp"},
            {"name": "🇰🇷韩国 02 | 1X", "type": "vless", "network": "ws"},
            {"name": "BackupSub 🇺🇸美国 备1", "type": "ss", "server": "2.2.2.1", "port": 443},
            {"name": "BackupSub 🇰🇷韩国 备1", "type": "ss", "server": "2.2.2.2", "port": 443},
        ],
        "proxy-groups": [
            {"name": "Proxy", "type": "select", "proxies": ["🇺🇸美国 01 | 1X", "🇸🇬新加坡 01 | 1X"]},
        ],
        "rules": ["MATCH,Proxy"],
    }
    config_file(paths).write_text(yaml.safe_dump(cfg, allow_unicode=True), encoding="utf-8")
    runtime_path = RuntimeBackend(paths).render_runtime()
    rendered = yaml.safe_load(runtime_path.read_text(encoding="utf-8"))
    groups = {g["name"]: g["proxies"] for g in rendered["proxy-groups"]}

    # AI-GEMINI: ws 节点排在裸 vision 节点前，且附加机场韩国节点挂在尾部做跨供应商热备
    assert groups["AI-GEMINI"] == ["🇰🇷韩国 02 | 1X", "🇰🇷韩国 01 | 专线", "BackupSub 🇰🇷韩国 备1"]
    # AI-MANUAL: 附加机场美国/韩国节点排在主节点之后、AI-AUTO 之前
    assert "BackupSub 🇺🇸美国 备1" in groups["AI-MANUAL"]
    assert groups["AI-MANUAL"][-1] == "AI-AUTO"
    # 国产 AI 直连规则已注入
    assert "DOMAIN-SUFFIX,deepseek.com,DIRECT" in rendered["rules"]
    assert "DOMAIN-SUFFIX,moonshot.cn,DIRECT" in rendered["rules"]


def test_feature3_panel_info_nodes_fallback_and_doctor_quota_check(tmp_path):
    paths = _paths(tmp_path)
    proxies = [
        {"name": "剩余流量：577.12 GB"},
        {"name": "套餐到期：2099-12-31"},
        {"name": "🇺🇸美国 01"},
    ]
    usage = parse_panel_info_nodes(proxies)
    assert usage is not None
    assert usage.remaining is not None
    assert abs(usage.remaining / (1024**3) - 577.12) < 0.05
    assert usage.expire is not None

    # 无响应头时自动用面板节点兜底落盘
    assert record_subscription_info(paths, MAIN_SUBSCRIPTION_KEY, None, proxies) is True
    loaded = load_subscription_info(paths)[MAIN_SUBSCRIPTION_KEY]
    assert loaded.remaining == usage.remaining

    check_ok = _subscription_doctor_check(paths, None)
    assert check_ok is not None and check_ok.status == "正常"

    # 模拟流量极低 (< 1 GB) 时 doctor 告警
    low_proxies = [{"name": "剩余流量：200 MB"}]
    record_subscription_info(paths, MAIN_SUBSCRIPTION_KEY, None, low_proxies)
    check_low = _subscription_doctor_check(paths, None)
    assert check_low is not None and check_low.status == "失败"


def test_feature4_active_ai_processes_mapping(tmp_path, monkeypatch):
    paths = _paths(tmp_path)
    fake_connections = {
        "connections": [
            {
                "metadata": {
                    "process": "/usr/share/antigravity/bin/agy.1775816521.old (deleted)",
                    "host": "daily-cloudcode-pa.googleapis.com",
                },
                "chains": ["🇩🇪德国 02 | 1X", "AI-GEMINI"],
            },
            {
                "metadata": {
                    "process": "/usr/share/antigravity/bin/agy",
                    "host": "daily-cloudcode-pa.googleapis.com",
                },
                "chains": ["🇩🇪德国 02 | 1X", "AI-GEMINI"],
            },
            {
                "metadata": {
                    "process": "/usr/share/antigravity/bin/agy",
                    "host": "antigravity-unleash.goog",
                },
                "chains": ["🇩🇪德国 02 | 1X", "AI-GEMINI"],
            },
            {
                "metadata": {
                    "process": "/usr/share/antigravity/bin/agy",
                    "host": "oauth2.googleapis.com",
                },
                "chains": ["🇩🇪德国 02 | 1X", "AI-GEMINI"],
            },
            {
                "metadata": {
                    "process": "/usr/local/bin/claude",
                    "host": "api.deepseek.com",
                },
                "chains": ["DIRECT"],
            },
        ]
    }
    monkeypatch.setattr(
        "cproxy.services.ops.APIBackend.get_connections",
        lambda _self: fake_connections,
    )
    routes = get_active_ai_processes(paths)
    assert len(routes) == 2
    by_proc = {r.process: r for r in routes}
    assert by_proc["agy"].count == 4
    assert by_proc["agy"].host == "daily-cloudcode-pa.googleapis.com, antigravity-unleash.goog 等 3 个"
    assert by_proc["agy"].route == "AI-GEMINI -> 🇩🇪德国 02 | 1X"
    assert by_proc["claude"].route == "DIRECT"


def test_history_delay_ignores_concurrent_burst_trailing_timeout():
    from cproxy.backend.api import _latest_history_delay

    # 同秒多组并发测速：前序 660ms 成功，0.8s 后尾项 5001ms 超时结束，应取 660ms
    burst_history = [
        {"time": "2026-10-18T20:46:33.370667916+08:00", "delay": 660},
        {"time": "2026-10-18T20:46:34.168334856+08:00", "delay": 5001},
    ]
    assert _latest_history_delay(burst_history, alive=True) == 660

    # 跨周期（相隔 300s）的真实超时：必须保留 5000 超时哨兵
    real_timeout_history = [
        {"time": "2026-10-18T20:41:33.370667916+08:00", "delay": 660},
        {"time": "2026-10-18T20:46:34.168334856+08:00", "delay": 5000},
    ]
    assert _latest_history_delay(real_timeout_history, alive=True) == 5000
