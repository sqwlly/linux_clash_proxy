"""Windows / Clash Verge (Rev) 旁路调度模式与跨平台连接时序跟踪测试。"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import yaml

ROOT_DIR = Path(__file__).resolve().parents[1]
SRC_DIR = ROOT_DIR / "src"
sys.path.insert(0, str(SRC_DIR))

from cproxy.backend.models import ProxyGroup
from cproxy.config import AppPaths, ai_manual_order_file, config_file, default_paths
from cproxy.services import ai_watch as ai_watch_mod
from cproxy.services import rebalance as rebalance_mod
from cproxy.services.ai_watch import (
    has_active_ai_transfer,
    reap_idle_gemini_connections,
    track_connections_idle_ms,
)
from cproxy.services.probe import ProbeSummary, StabilityVerdict
from cproxy.services.rebalance import RebalanceService
from cproxy.services.verge import (
    VergeDaemonService,
    discover_clash_verge_config,
    export_verge_script,
    init_verge_config,
    install_verge_oneclick,
)


def _paths(tmp_path: Path) -> AppPaths:
    return AppPaths(
        config_dir=tmp_path / "cfg",
        data_dir=tmp_path / "data",
        state_dir=tmp_path / "state",
    )


def test_windows_default_paths_and_env_override(tmp_path, monkeypatch):
    monkeypatch.setenv("CPROXY_CONFIG_DIR", str(tmp_path / "custom-cfg"))
    monkeypatch.setenv("CPROXY_DATA_DIR", str(tmp_path / "custom-data"))
    monkeypatch.setenv("CPROXY_STATE_DIR", str(tmp_path / "custom-state"))
    p = default_paths()
    assert p.config_dir == tmp_path / "custom-cfg"
    assert p.data_dir == tmp_path / "custom-data"
    assert p.state_dir == tmp_path / "custom-state"


def test_discover_and_init_clash_verge_config(tmp_path):
    verge_dir = tmp_path / "io.github.clash-verge-rev.clash-verge-rev"
    verge_dir.mkdir(parents=True)
    (verge_dir / "clash-verge.yaml").write_text(
        "mixed-port: 7897\nexternal-controller: 127.0.0.1:9097\nsecret: verge-secret-123\n",
        encoding="utf-8",
    )

    detected = discover_clash_verge_config(verge_dir)
    assert detected.controller == "127.0.0.1:9097"
    assert detected.secret == "verge-secret-123"
    assert detected.mixed_port == 7897
    assert detected.source_dir == verge_dir

    paths = _paths(tmp_path)
    cfg_path, final = init_verge_config(paths, verge_dir=verge_dir)
    assert cfg_path == config_file(paths)
    assert final.controller == "127.0.0.1:9097"
    saved = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    assert saved["verge-mode"] is True
    assert saved["external-controller"] == "127.0.0.1:9097"
    assert saved["secret"] == "verge-secret-123"
    assert saved["mixed-port"] == 7897


def test_export_verge_preprocess_script_and_node_execution(tmp_path):
    out_file = tmp_path / "clash-verge-script.js"
    content = export_verge_script(out_file)
    assert out_file.exists()
    assert "function main(config)" in content
    assert "AI-GEMINI" in content
    assert "AI-MANUAL" in content
    assert "PROCESS-NAME,agy.exe,AI-MANUAL" in content

    # 若当前环境安装了 node，实测 JS 脚本对订阅配置的转换结果
    try:
        node_check = subprocess.run(["node", "--version"], capture_output=True, text=True, timeout=3)
    except Exception:
        node_check = None
    if node_check and node_check.returncode == 0:
        harness = tmp_path / "run_script.js"
        harness.write_text(
            content
            + "\nconst input = {\n"
            + "  proxies: [\n"
            + "    { name: '🇺🇸美国 01 | IEPL 2X', type: 'vless', flow: 'xtls-rprx-vision', network: 'tcp' },\n"
            + "    { name: '🇺🇸美国 01 | 1X', type: 'vless', network: 'ws', port: 443,\n"
            + "      server: 'shared.example.com', servername: 'sub.example.com',\n"
            + "      'ws-opts': { headers: { Host: 'us1.sub.example.com' } } },\n"
            + "    { name: '🇰🇷韩国 01 | 1X', type: 'vless', network: 'ws' },\n"
            + "    { name: '🇰🇷韩国 01 | 1X', type: 'vless', network: 'ws' },\n"
            + "    { name: '剩余流量: 99GB', type: 'ss' }\n"
            + "  ],\n"
            + "  'proxy-groups': [{ name: 'Proxy', type: 'select', proxies: ['🇺🇸美国 01 | 1X', '剩余流量: 99GB'] }],\n"
            + "  rules: ['DOMAIN-SUFFIX,openai.com,Proxy', 'IP-CIDR,192.168.0.0/16,DIRECT', 'MATCH,Proxy']\n"
            + "};\n"
            + "console.log(JSON.stringify(main(input)));\n",
            encoding="utf-8",
        )
        res = subprocess.run(["node", str(harness)], capture_output=True, text=True, check=True, timeout=5)
        rendered = json.loads(res.stdout)
        groups = {g["name"]: g for g in rendered["proxy-groups"]}
        # 1X 非易断流节点必须排在 IEPL / 裸 vision 之前
        assert groups["AI-MANUAL"]["proxies"][0] == "🇺🇸美国 01 | 1X"
        assert groups["AI-MANUAL"]["proxies"][-1] == "AI-AUTO"
        # 重名节点已被自动消歧为 #2
        assert "🇰🇷韩国 01 | 1X #2" in groups["AI-GEMINI"]["proxies"]
        # 443 WS 节点的 server 已优选为独立子域 Host
        us_ws = next(p for p in rendered["proxies"] if p["name"] == "🇺🇸美国 01 | 1X")
        assert us_ws["server"] == "us1.sub.example.com"
        # 冲突的 openai.com->Proxy 已剔除，AI 规则置顶，IP-CIDR 补齐 no-resolve
        assert "DOMAIN-SUFFIX,openai.com,Proxy" not in rendered["rules"]
        assert rendered["rules"][0] == "DOMAIN-SUFFIX,google.com,AI-GEMINI"
        assert "IP-CIDR,192.168.0.0/16,DIRECT,no-resolve" in rendered["rules"]


def test_cross_platform_connection_activity_tracker_without_ss(tmp_path, monkeypatch):
    """验证在无 Linux `ss` 的 Windows 环境下，基于 `/connections` 字节差分的时序跟踪器：
    1. 首次观测到连接时绝不误杀；
    2. 连接完成传输并静默达到阈值（小包 >= 6s，大包 >= 35s）后被精准回收；
    3. 复用连接发起第 2 轮长思考（upload 增长、download 不变）时识别为 thinking，绝不误杀；
    4. 正在上传大包上下文时 `has_active_ai_transfer` 返回 True。
    """
    paths = _paths(tmp_path)
    # 模拟 Windows 环境：`ss` 不可用，返回空字典
    monkeypatch.setattr(ai_watch_mod, "get_local_proxy_socket_idle_ms", lambda _port=7890: {})

    conns = [
        {
            "id": "c-done-large",
            "chains": ["🇰🇷韩国 01 | 1X", "AI-GEMINI"],
            "upload": 240_000,
            "download": 42_000,
            "metadata": {"host": "daily-cloudcode-pa.googleapis.com", "sourcePort": "51001"},
        },
        {
            "id": "c-reused-thinking",
            "chains": ["🇰🇷韩国 01 | 1X", "AI-GEMINI"],
            "upload": 300_000,
            "download": 50_000,
            "metadata": {"host": "daily-cloudcode-pa.googleapis.com", "sourcePort": "51002"},
        },
        {
            "id": "c-uploading",
            "chains": ["🇺🇸美国 01 | 1X", "AI-MANUAL"],
            "upload": 120_000,
            "download": 2_000,
            "metadata": {"host": "chatgpt.com", "sourcePort": "51003"},
        },
    ]
    closed: list[str] = []

    class _FakeAPI:
        def get_connections(self):
            return {"connections": conns}

        def close_connection(self, cid: str):
            closed.append(cid)

    t0 = 1_700_000_000.0
    # 第 1 次采样 (t0)：建立基线，刚发现的连接 idle=0ms，绝不误杀，且正在上传的连接被识别为活跃
    monkeypatch.setattr(ai_watch_mod.time, "time", lambda: t0)
    assert reap_idle_gemini_connections(paths, _FakeAPI(), current_leaf="🇰🇷韩国 01 | 1X") == []
    assert has_active_ai_transfer(paths, _FakeAPI(), "AI-MANUAL", "🇺🇸美国 01 | 1X") is True

    # t0 + 10s：c-reused-thinking 复用了 HTTP/2 连接上传第二轮大 Prompt (upload 300KB -> 680KB)
    conns[1]["upload"] = 680_000
    track_connections_idle_ms(paths, conns, now=t0 + 10.0)

    # t0 + 45s：
    # - c-done-large 已静默 45s (>= 35s) -> idle_completed，应被回收；
    # - c-reused-thinking 虽然距上次发包已过 35s，但处于等待第二轮响应的 thinking 状态 -> 绝不误杀！
    monkeypatch.setattr(ai_watch_mod.time, "time", lambda: t0 + 45.0)
    reaped = reap_idle_gemini_connections(paths, _FakeAPI(), current_leaf="🇰🇷韩国 01 | 1X")
    assert reaped == ["c-done-large"]
    assert closed == ["c-done-large"]


def test_rebalance_external_only_switches_without_rendering_runtime(tmp_path, monkeypatch):
    """验证 Clash Verge 旁路模式下 rebalance：
    不读取/不渲染本地 runtime.yaml，但依然持久化排序并通过 API 切换不稳定出口。
    """
    paths = _paths(tmp_path)
    switched: list[tuple[str, str]] = []

    class _FakeProbe:
        def __init__(self, p):
            pass

        def probe(self, **kwargs):
            return SimpleNamespace(
                current="🇺🇸美国 IEPL",
                current_verdict=StabilityVerdict(False, "超时"),
                verdict=StabilityVerdict(True, "稳定"),
                summaries=[
                    ProbeSummary("🇺🇸美国 IEPL", (600,), failures=2),
                    ProbeSummary("🇺🇸美国 01 | 1X", (160, 170, 165), failures=0),
                ],
            )

    class _FakeQuery:
        def __init__(self, p):
            self.api = SimpleNamespace(
                get_groups=lambda: {
                    "AI-MANUAL": ProxyGroup("AI-MANUAL", "fallback", "🇺🇸美国 IEPL", ["🇺🇸美国 IEPL", "🇺🇸美国 01 | 1X"]),
                    "AI-GEMINI": ProxyGroup("AI-GEMINI", "fallback", "🇺🇸美国 IEPL", ["🇺🇸美国 IEPL", "🇺🇸美国 01 | 1X"]),
                },
                get_connections=lambda: {"connections": []},
            )

        def switch_group(self, grp: str, target: str, **kwargs):
            switched.append((grp, target))

    monkeypatch.setattr(rebalance_mod, "ProbeService", _FakeProbe)
    monkeypatch.setattr(rebalance_mod, "QueryService", _FakeQuery)

    report = RebalanceService(paths).rebalance(external_only=True)
    assert report.rerendered is False
    assert report.hot_reloaded is False
    assert report.reason == "external-only"
    assert report.switched_to == "🇺🇸美国 01 | 1X"
    assert report.gemini_switched_to == "🇺🇸美国 01 | 1X"
    assert ("AI-MANUAL", "🇺🇸美国 01 | 1X") in switched
    assert ("AI-GEMINI", "🇺🇸美国 01 | 1X") in switched
    assert json.loads(ai_manual_order_file(paths).read_text(encoding="utf-8"))[0] == "🇺🇸美国 01 | 1X"


def test_verge_daemon_service_run_once(tmp_path, monkeypatch):
    paths = _paths(tmp_path)
    init_verge_config(paths, controller="127.0.0.1:9097", secret="s", mixed_port=7897)

    monkeypatch.setattr(
        "cproxy.services.verge.sample_connections_activity",
        lambda p, api, now=None: {"50001": (100, 200)},
    )
    monkeypatch.setattr(
        "cproxy.services.verge.AIWatchService.watch",
        lambda self, dry_run=False: ai_watch_mod.WatchReport(
            results=[ai_watch_mod.WatchGroupResult(group="AI-GEMINI", current="KR-01", verdict="ok")]
        ),
    )
    svc = VergeDaemonService(paths)
    tick = svc.run_once(run_rebalance=False)
    assert tick.sampled_connections == 1
    assert tick.watch_report is not None
    assert tick.watch_report.results[0].current == "KR-01"


def test_install_verge_oneclick_auto_injects_script_and_enables_chain(tmp_path):
    verge_dir = tmp_path / "io.github.clash-verge-rev.clash-verge-rev"
    (verge_dir / "profiles").mkdir(parents=True)
    (verge_dir / "clash-verge.yaml").write_text(
        "mixed-port: 7897\nexternal-controller: 127.0.0.1:9097\nsecret: my-secret\n",
        encoding="utf-8",
    )
    (verge_dir / "profiles.yaml").write_text(
        "current: sub1\nchain:\n  - Merge\nitems:\n  - uid: Merge\n    type: merge\n    file: Merge.yaml\n"
        "  - uid: Script\n    type: script\n    file: Script.js\n",
        encoding="utf-8",
    )
    (verge_dir / "profiles" / "Script.js").write_text("// old script\nfunction main(c) { return c; }\n", encoding="utf-8")

    paths = _paths(tmp_path)
    rep = install_verge_oneclick(paths, verge_dir=verge_dir)
    assert rep.controller == "127.0.0.1:9097"
    assert rep.mixed_port == 7897
    assert rep.secret_configured is True
    assert rep.chain_enabled is True
    assert rep.injected_script_path == verge_dir / "profiles" / "Script.js"
    assert "AI-GEMINI" in rep.injected_script_path.read_text(encoding="utf-8")
    assert (verge_dir / "profiles" / "Script.js.bak").exists()

    updated_profiles = yaml.safe_load((verge_dir / "profiles.yaml").read_text(encoding="utf-8"))
    assert "Script" in updated_profiles["chain"]
    assert (verge_dir / "profiles.yaml.bak").exists()

