"""Windows / Clash Verge (Rev) 旁路调度模式与跨平台连接时序跟踪测试。"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

ROOT_DIR = Path(__file__).resolve().parents[1]
SRC_DIR = ROOT_DIR / "src"
sys.path.insert(0, str(SRC_DIR))

from cproxy.backend.models import ProxyGroup
from cproxy.cli import _print_verge_status
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
    VergeEgressStatus,
    VergeStatusReport,
    check_verge_effectiveness,
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
    if sys.platform == "win32":
        assert default_paths().config_dir == tmp_path / "appdata" / "cproxy"
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
        res = subprocess.run(
            ["node", str(harness)], capture_output=True, text=True, encoding="utf-8", check=True, timeout=5
        )
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


def test_install_verge_oneclick_missing_directory_preserves_config(tmp_path):
    paths = _paths(tmp_path)

    with pytest.raises(RuntimeError, match="未检测到 Clash Verge"):
        install_verge_oneclick(paths, verge_dir=tmp_path / "missing")

    assert not config_file(paths).exists()


def test_ai_watch_reports_missing_group_when_topology_not_mounted(tmp_path, monkeypatch):
    """回归保护：当 Clash Verge 尚未刷新订阅、内存中不存在 AI-MANUAL / AI-GEMINI 时，
    ai-watch 必须返回 verdict='missing_group'，而不能误测默认代理报出虚假的 ok(-)。
    """
    paths = _paths(tmp_path)

    class _FakeQuery:
        def __init__(self, p):
            self.api = SimpleNamespace(
                get_groups=lambda: {"PROXY": ProxyGroup("PROXY", "select", "DIRECT", ["DIRECT"])},
                get_connections=lambda: {"connections": []},
            )

    monkeypatch.setattr(ai_watch_mod, "QueryService", _FakeQuery)
    report = ai_watch_mod.AIWatchService(paths).watch()
    assert [r.verdict for r in report.results] == ["missing_group", "missing_group"]


def test_check_verge_effectiveness_control_plane_and_health(tmp_path, monkeypatch):
    """验证控制器、策略拓扑、分流规则、守护心跳、AI 看护和切流风暴核验。"""
    paths = _paths(tmp_path)
    init_verge_config(paths, controller="127.0.0.1:9097", secret="s", mixed_port=7897)

    # 先模拟执行一轮守护心跳记录
    monkeypatch.setattr(
        "cproxy.services.verge.sample_connections_activity",
        lambda p, api, now=None: {"50001": (100, 200)},
    )
    monkeypatch.setattr(
        "cproxy.services.verge.AIWatchService.watch",
        lambda self, dry_run=False: ai_watch_mod.WatchReport(
            results=[
                ai_watch_mod.WatchGroupResult(group="AI-MANUAL", current="🇺🇸美国-自动", verdict="ok", switched=True),
                ai_watch_mod.WatchGroupResult(group="AI-GEMINI", current="🇰🇷韩国-自动", verdict="ok"),
            ],
            reaped_connections=["c-1", "c-2"],
        ),
    )
    VergeDaemonService(paths).run_once(run_rebalance=False)

    # 场景 A：内核已挂载 AI-MANUAL/AI-GEMINI/AI-AUTO 与分流规则 -> overall_effective = True
    class _MountedAPI:
        def __init__(self, p):
            pass

        def version(self):
            return {"version": "v1.19.16"}

        def get_groups(self):
            return {
                "AI-MANUAL": ProxyGroup("AI-MANUAL", "fallback", "🇺🇸美国-自动", ["🇺🇸美国-自动", "AI-AUTO"]),
                "AI-GEMINI": ProxyGroup("AI-GEMINI", "fallback", "🇰🇷韩国-自动", ["🇰🇷韩国-自动", "AI-AUTO"]),
                "AI-AUTO": ProxyGroup("AI-AUTO", "url-test", "🇺🇸美国 01 | 1X", ["🇺🇸美国 01 | 1X"]),
                "🇺🇸美国-自动": ProxyGroup("🇺🇸美国-自动", "url-test", "🇺🇸美国 01 | 1X", ["🇺🇸美国 01 | 1X"]),
                "🇰🇷韩国-自动": ProxyGroup("🇰🇷韩国-自动", "url-test", "🇰🇷韩国 01 | 1X", ["🇰🇷韩国 01 | 1X"]),
                "🇺🇸美国 01 | 1X": ProxyGroup("🇺🇸美国 01 | 1X", "ss", None, [], delay=142, alive=True),
                "🇰🇷韩国 01 | 1X": ProxyGroup("🇰🇷韩国 01 | 1X", "ss", None, [], delay=68, alive=True),
            }

        def get_rules(self):
            return {
                "rules": [
                    {"type": "DomainSuffix", "payload": "googleapis.com", "proxy": "AI-GEMINI"},
                    {"type": "DomainSuffix", "payload": "openai.com", "proxy": "AI-MANUAL"},
                    {"type": "DomainSuffix", "payload": "claude.ai", "proxy": "AI-MANUAL"},
                ]
            }

        def get_connections(self):
            return {
                "connections": [
                    {"id": "c1", "chains": ["🇰🇷韩国 01 | 1X", "🇰🇷韩国-自动", "AI-GEMINI"]},
                ]
            }

    monkeypatch.setattr("cproxy.services.verge.APIBackend", _MountedAPI)
    status_ok = check_verge_effectiveness(paths)
    assert status_ok.overall_effective is True
    assert status_ok.controller_ok is True
    assert status_ok.topology_ok is True
    assert status_ok.rules_ok is True
    assert status_ok.daemon_ok is True
    assert status_ok.ai_manual_rules_count == 2
    assert status_ok.ai_gemini_rules_count == 1
    assert status_ok.watch_runs == 1
    assert status_ok.watch_ok is True
    assert status_ok.last_watch_age_sec is not None
    assert status_ok.total_switches == 1
    assert status_ok.switches_last_10m == 1
    assert status_ok.switch_storm is False
    assert status_ok.total_reaped_connections == 2
    assert status_ok.remedies == []
    gemini_egress = next(e for e in status_ok.egress if e.group == "AI-GEMINI")
    assert gemini_egress.leaf_node == "🇰🇷韩国 01 | 1X"
    assert gemini_egress.is_iepl is False
    assert gemini_egress.active_connections == 1

    state_file = paths.state_dir / "verge-daemon.json"
    daemon_state = json.loads(state_file.read_text(encoding="utf-8"))
    daemon_state["last_verdicts"]["AI-MANUAL"] = "dead"
    state_file.write_text(json.dumps(daemon_state), encoding="utf-8")
    status_unhealthy = check_verge_effectiveness(paths)
    assert status_unhealthy.overall_effective is False
    assert status_unhealthy.watch_ok is False
    assert any("看护未通过" in remedy for remedy in status_unhealthy.remedies)

    daemon_state["last_verdicts"]["AI-MANUAL"] = "ok"
    daemon_state["switch_timestamps"] = [daemon_state["last_watch_ts"]] * 6
    state_file.write_text(json.dumps(daemon_state), encoding="utf-8")
    status_storm = check_verge_effectiveness(paths)
    assert status_storm.overall_effective is False
    assert status_storm.switch_storm is True
    assert status_storm.switches_last_10m == 6
    assert any("切流风暴" in remedy for remedy in status_storm.remedies)

    # 场景 B：Clash Verge 还没点「订阅 -> 刷新」，内存中无 AI 组和规则 -> overall_effective = False 且给出明确修复建议
    class _UnmountedAPI(_MountedAPI):
        def get_groups(self):
            return {"PROXY": ProxyGroup("PROXY", "select", "DIRECT", ["DIRECT"])}

        def get_rules(self):
            return {"rules": [{"type": "Match", "payload": "", "proxy": "PROXY"}]}

    monkeypatch.setattr("cproxy.services.verge.APIBackend", _UnmountedAPI)
    status_unmounted = check_verge_effectiveness(paths)
    assert status_unmounted.overall_effective is False
    assert status_unmounted.controller_ok is True
    assert status_unmounted.topology_ok is False
    assert status_unmounted.rules_ok is False
    assert any("订阅" in r and "刷新" in r for r in status_unmounted.remedies)


def test_verge_status_rendering_is_operational_and_raw_compatible(capsys):
    rep = VergeStatusReport(
        overall_effective=False,
        controller="127.0.0.1:9097",
        mixed_port=7897,
        controller_ok=True,
        kernel_version="v1.19.32",
        topology_ok=True,
        groups_present=["AI-MANUAL", "AI-GEMINI", "AI-AUTO"],
        rules_ok=True,
        ai_manual_rules_count=20,
        ai_gemini_rules_count=10,
        daemon_ok=True,
        daemon_pid=7980,
        heartbeat_age_sec=1.2,
        watch_ok=False,
        last_watch_age_sec=20.0,
        watch_runs=100,
        total_switches=20,
        switches_last_10m=6,
        switch_storm=True,
        total_reaped_connections=3,
        egress=[
            VergeEgressStatus(
                group="AI-MANUAL",
                present=True,
                current_group="US 01",
                leaf_node="US 01",
                delay_ms=194,
                active_connections=2,
                last_verdict="dead",
            )
        ],
        remedies=["检查探测链路"],
    )

    _print_verge_status(rep)
    output = capsys.readouterr().out
    assert "Clash Verge 旁路控制面 · 降级" in output
    for section in ("AI 出口", "调度运行", "配置检查", "建议操作"):
        assert section in output
    assert "近 10 分钟 6 次" in output
    assert "切流风暴" in output
    assert "异常 · 延迟 194 ms" in output

    _print_verge_status(rep, raw=True)
    raw = capsys.readouterr().out
    assert raw.splitlines()[0] == "EFFECTIVE\tfalse\tcontroller=ok\ttopology=ok\trules=ok\tdaemon=ok"
    assert raw.splitlines()[1] == "EGRESS\tAI-MANUAL\tUS 01\tUS 01\t194\tnon_iepl\tconns=2"


def test_windows_daemon_uses_dedicated_process_name():
    script = (ROOT_DIR / "scripts" / "windows" / "install-verge-task.ps1").read_text(encoding="utf-8")

    assert '$DaemonExe = Join-Path $PythonDir "cproxy-daemon.exe"' in script
    assert "Copy-Item -Path $PythonwExe -Destination $DaemonExe -Force" in script
    assert "New-ScheduledTaskAction -Execute $DaemonExe" in script
    assert 'WshShell.Run """$DaemonExe"" -m cproxy.cli verge daemon' in script
    assert "Start-Process -FilePath $DaemonExe" in script
    assert "& $DaemonExe -m cproxy.cli verge once --no-rebalance" in script
    assert "Name LIKE 'python%.exe' OR Name = 'cproxy-daemon.exe'" in script
    assert "Stop-ScheduledTask -TaskName $TaskName" in script
    assert '$env:PYTHONIOENCODING = "utf-8"' in script
    assert 'Assert-NativeSuccess "cproxy 安装"' in script
    assert 'Assert-NativeSuccess "Clash Verge 旁路集成"' in script
    assert 'Assert-NativeSuccess "首轮 AI 看护"' in script
    assert 'throw "Verge 状态核验在 30 秒内未通过"' in script
    assert 'Assert-NativeSuccess "Verge 状态展示"' in script
    assert "sys.version_info[:2]" in script
    assert "Set-Content -Path $StartupVbs -Value $VbsContent -Encoding Unicode" in script


def test_windows_reused_http2_thinking_with_response_id_and_pythonw_null_stderr(tmp_path, monkeypatch):
    """回归保护：
    1. 复用 HTTP/2 连接发起新轮次（upload 增长）后，即便 1~3 秒内收到 HTTP 200 ResponseID
       或初始思考块（delta_down = 1.5KB~8KB），在 35~75 秒长思考静默期内仍必须识别为 thinking，绝不误杀断流；
    2. 小包监控连接（0 < upload < 18KB, download >= 4KB）在 Windows 下未满 75s 不误杀（保护 sir_flash_lite）；
    3. pythonw.exe / cproxy-daemon.exe 下 sys.stderr 为 None 时，ProgressLine/ProbeService 不抛 AttributeError；
    4. Antigravity 桌面端 language_server.log（含 ERROR: logging before google.Init: 前缀及连续 attempt 4 断流）可被精准识别；
    5. 流量面板假节点（如 '99.35 GB | 200 GB'）绝不被误判为英国 (GB) 节点注入 AI-GEMINI / AI-MANUAL。
    """
    import os
    import time
    from datetime import datetime

    from cproxy.feedback import ProgressLine
    from cproxy.services.ai_watch import detect_recent_agy_retry_failure
    from cproxy.services.nodelist import is_panel_info_node, match_region

    # 1 & 2. Windows 跨平台时序跟踪器在收到 ResponseID / 初始思考块后保护长思考连接
    paths = _paths(tmp_path)
    monkeypatch.setattr(ai_watch_mod, "get_local_proxy_socket_idle_ms", lambda _port=7890: {})
    conns = [
        {
            "id": "c-h2-thinking",
            "chains": ["🇨🇳 Taiwan 05", "AI-GEMINI"],
            "upload": 350_000,
            "download": 60_000,
            "metadata": {"host": "daily-cloudcode-pa.googleapis.com", "sourcePort": "52001"},
        },
        {
            "id": "c-flash-lite-monitor",
            "chains": ["🇨🇳 Taiwan 05", "AI-GEMINI"],
            "upload": 8_500,
            "download": 6_200,
            "metadata": {"host": "daily-cloudcode-pa.googleapis.com", "sourcePort": "52002"},
        },
    ]
    closed: list[str] = []

    class _API:
        def get_connections(self):
            return {"connections": conns}

        def close_connection(self, cid: str):
            closed.append(cid)

    t0 = 1_750_000_000.0
    monkeypatch.setattr(ai_watch_mod.time, "time", lambda: t0)
    track_connections_idle_ms(paths, conns, now=t0)

    # t0 + 3s: 用户发起新一轮请求，upload 增加 80KB
    conns[0]["upload"] = 430_000
    track_connections_idle_ms(paths, conns, now=t0 + 3.0)

    # t0 + 6s: 收到 Google Cloud Code 的 HTTP 200 ResponseID 响应头与初始思考块 (+2.5KB)
    conns[0]["download"] = 62_500
    track_connections_idle_ms(paths, conns, now=t0 + 6.0)

    # t0 + 55s (距收到 ResponseID 已静默 49s > 35s): 仍处于长思考期，且小包连接未满 75s，两者均绝不误杀！
    monkeypatch.setattr(ai_watch_mod.time, "time", lambda: t0 + 55.0)
    assert reap_idle_gemini_connections(paths, _API(), current_leaf="🇨🇳 Taiwan 05") == []
    assert closed == []

    # t0 + 80s: 小包监控连接已静默 80s (>= 75s) 被安全回收，而长思考连接 (74s < 90s) 仍受保护
    monkeypatch.setattr(ai_watch_mod.time, "time", lambda: t0 + 80.0)
    reaped_80 = reap_idle_gemini_connections(paths, _API(), current_leaf="🇨🇳 Taiwan 05")
    assert reaped_80 == ["c-flash-lite-monitor"]

    # 3. pythonw.exe / cproxy-daemon.exe 下 sys.stderr 为 None 时 ProgressLine 零异常
    monkeypatch.setattr(sys, "stderr", None)
    pl = ProgressLine(enabled=False)
    pl.update("轮 1/5")
    pl.commit("完成")

    # 4. Antigravity IDE language_server.log 前缀与连续断流识别
    log_dir = tmp_path / "ide-logs"
    log_dir.mkdir()
    now_ts = time.time()
    ts_4 = datetime.fromtimestamp(now_ts - 25).strftime("%m%d %H:%M:%S")
    ts_5 = datetime.fromtimestamp(now_ts - 5).strftime("%m%d %H:%M:%S")
    ls_log = log_dir / "language_server.log"
    ls_log.write_text(
        f"ERROR: logging before google.Init: I{ts_4}.008253 18944 run.go:395] "
        "Run: attempt 4 failed (stream reading error: unexpected EOF), retrying in 22.8s...\n"
        f"ERROR: logging before google.Init: I{ts_5}.100000 18944 http_helpers.go:349] "
        "URL: https://daily-cloudcode-pa.googleapis.com/v1internal:streamGenerateContent?alt=sse "
        "Trace: 0x11 ResponseID: resp_attempt_5\n"
        f"ERROR: logging before google.Init: I{ts_5}.900000 18944 run.go:395] "
        "Run: attempt 5 failed (stream reading error: unexpected EOF), retrying in 39.2s...\n",
        encoding="utf-8",
    )
    os.utime(ls_log, (now_ts - 5, now_ts - 5))
    hit_ls, _, reason_ls = detect_recent_agy_retry_failure(log_dir, now=now_ts, window_seconds=60.0)
    assert hit_ls is True
    assert "attempt 4" in reason_ls

    # 5. 流量配额面板节点绝不误匹配为英国 (GB)
    assert is_panel_info_node("99.35 GB | 200 GB") is True
    assert match_region("99.35 GB | 200 GB") == "OTHER"
    assert match_region("🇬🇧 英国 London 01") == "GB"


def test_scheduler_telemetry_perceives_ai_watch_and_systemd_timers(tmp_path, monkeypatch):
    """验证 Linux systemd timer 直接运行 cproxy ai-watch（未跑 verge daemon）时，
    check_verge_effectiveness / collect_scheduler_telemetry / doctor 仍能感知调度看护存活。"""
    from cproxy.config import ai_manual_order_file, ai_watch_state_file
    from cproxy.doctor import _scheduler_doctor_check
    from cproxy.services.verge import collect_scheduler_telemetry

    paths = _paths(tmp_path)
    ai_manual_order_file(paths).parent.mkdir(parents=True, exist_ok=True)
    ai_manual_order_file(paths).write_text(json.dumps(["🇹🇼台湾 01 | 1X"]), encoding="utf-8")

    groups = {
        "AI-MANUAL": ProxyGroup("AI-MANUAL", "fallback", "🇹🇼台湾 01 | 1X", ["🇹🇼台湾 01 | 1X", "AI-AUTO"]),
        "AI-GEMINI": ProxyGroup("AI-GEMINI", "fallback", "🇬🇧英国 01 | 1X", ["🇬🇧英国 01 | 1X", "AI-AUTO"]),
        "AI-AUTO": ProxyGroup("AI-AUTO", "url-test", "🇹🇼台湾 01 | 1X", ["🇹🇼台湾 01 | 1X"]),
        "🇹🇼台湾 01 | 1X": ProxyGroup("🇹🇼台湾 01 | 1X", "ss", None, [], delay=180, alive=True),
        "🇬🇧英国 01 | 1X": ProxyGroup("🇬🇧英国 01 | 1X", "ss", None, [], delay=210, alive=True),
    }
    monkeypatch.setattr(ai_watch_mod, "ai_target_verdict", lambda p, url: "ok")
    monkeypatch.setattr(ai_watch_mod, "reap_idle_gemini_connections", lambda *a, **kw: ["c-99"])
    monkeypatch.setattr(ai_watch_mod, "reap_stale_agy_processes", lambda *a, **kw: [])

    svc = ai_watch_mod.AIWatchService(paths)
    svc.query = type("_Q", (), {"api": type("_A", (), {"get_groups": lambda _s: groups})()})()
    svc.watch()

    assert ai_watch_state_file(paths).exists()
    monkeypatch.setattr(
        "cproxy.services.verge._probe_user_systemd_timers",
        lambda _p: (
            ["cproxy-ai-watch.timer", "cproxy-ai-rebalance.timer"],
            ["cproxy-ai-watch.timer", "cproxy-ai-rebalance.timer"],
        ),
    )

    telemetry = collect_scheduler_telemetry(paths)
    assert telemetry["configured"] is True
    assert telemetry["scheduler_mode"] == "systemd-timer"
    assert telemetry["daemon_ok"] is True
    assert telemetry["watch_runs"] == 1
    assert telemetry["total_reaped_connections"] == 1
    assert telemetry["last_verdicts"] == {"AI-MANUAL": "ok", "AI-GEMINI": "ok"}

    doc_check = _scheduler_doctor_check(paths)
    assert doc_check is not None
    assert doc_check.name == "调度看护"
    assert doc_check.status == "正常"
    assert "systemd 定时器 2/2 活跃" in doc_check.detail


def test_windows_agy_exe_old_process_matching_and_python_direct_rule_warning(tmp_path, monkeypatch):
    """验证 Windows 自更新后的 `agy.exe.<ns>.old` 在规则、流量、进程识别中均被正确覆盖，
    且当订阅局部脚本置顶 `PROCESS-NAME,python.exe,DIRECT` 时，`check_verge_effectiveness`
    保持 `overall_effective=True` 并给出明确提示。"""
    from cproxy.services.ai_watch import _STALE_AGY_EXE_RE
    from cproxy.services.ops import _is_known_ai_process, _process_basename
    from cproxy.services.query import QueryService

    script_content = export_verge_script()
    assert r"PROCESS-NAME-REGEX,^agy\\.exe\\.[0-9]+\\.old,AI-MANUAL" in script_content
    assert (ROOT_DIR / "scripts" / "clash-verge-script.js").read_text(encoding="utf-8") == script_content

    win_old_path = r"C:\Users\14767\AppData\Local\agy\bin\agy.exe.1793269750355348300.old"
    assert _STALE_AGY_EXE_RE.search(win_old_path) is not None
    assert _process_basename({"processPath": win_old_path}) == "agy"
    assert _is_known_ai_process(_process_basename({"processPath": r"C:\Users\14767\AppData\Local\agy\bin\agy.exe"}))
    assert QueryService._connection_follows_ai_switch(
        {"chains": ["DIRECT"], "metadata": {"processPath": win_old_path}},
        "AI-MANUAL",
    )

    paths = _paths(tmp_path)
    init_verge_config(paths, controller="127.0.0.1:9097", secret="s", mixed_port=7897)
    monkeypatch.setattr(
        "cproxy.services.verge.sample_connections_activity",
        lambda p, api, now=None: {},
    )
    monkeypatch.setattr(
        "cproxy.services.verge.AIWatchService.watch",
        lambda self, dry_run=False: ai_watch_mod.WatchReport(
            results=[
                ai_watch_mod.WatchGroupResult(group="AI-MANUAL", current="US-01", verdict="ok"),
                ai_watch_mod.WatchGroupResult(group="AI-GEMINI", current="TW-01", verdict="ok"),
            ]
        ),
    )
    VergeDaemonService(paths).run_once(run_rebalance=False)

    class _PreemptedRulesAPI:
        def __init__(self, p):
            pass

        def version(self):
            return {"version": "v1.19.23"}

        def get_groups(self):
            return {
                "AI-MANUAL": ProxyGroup("AI-MANUAL", "fallback", "US-01", ["US-01", "AI-AUTO"]),
                "AI-GEMINI": ProxyGroup("AI-GEMINI", "fallback", "TW-01", ["TW-01"]),
                "AI-AUTO": ProxyGroup("AI-AUTO", "fallback", "US-01", ["US-01"]),
                "US-01": ProxyGroup("US-01", "anytls", None, [], delay=150, alive=True),
                "TW-01": ProxyGroup("TW-01", "anytls", None, [], delay=95, alive=True),
            }

        def get_rules(self):
            return {
                "rules": [
                    {"type": "ProcessName", "payload": "python.exe", "proxy": "DIRECT"},
                    {"type": "DomainSuffix", "payload": "googleapis.com", "proxy": "AI-GEMINI"},
                    {"type": "DomainSuffix", "payload": "openai.com", "proxy": "AI-MANUAL"},
                ]
            }

        def get_connections(self):
            return {"connections": []}

    monkeypatch.setattr("cproxy.services.verge.APIBackend", _PreemptedRulesAPI)
    rep = check_verge_effectiveness(paths)
    assert rep.overall_effective is True
    assert any("python.exe,DIRECT" in item for item in rep.remedies)


def test_ws_keepalive_ping_injection_pong_absorption_and_large_frame_streaming():
    """验证 HTTP/1.1 Upgrade: websocket -> 101 Switching Protocols 的 RFC 6455 帧级 Ping 保活：
    1. >64KB (70KB) 掩码大包客户端帧流式直传且绝不发生帧内插 Ping 破坏；
    2. 思考静默期自动按 ping_interval 发送掩码 Ping (0x89, cproxy01/02) 并吸收对应 Pong (0x8A)；
    3. 非 cproxy 前缀的业务 Pong 帧原样透传给客户端；
    4. 收到 response.completed 终态帧后立即退出高频轮次保活；
    5. 普通非 WebSocket HTTP/1.1 POST 请求保持零改动直通。
    """
    import asyncio
    import os

    from cproxy.services.h2_keepalive import _forward_http1_or_ws_with_ping

    def _encode_client_ws_frame(opcode: int, payload: bytes) -> bytes:
        mask = os.urandom(4)
        plen = len(payload)
        if plen < 126:
            hdr = bytes([0x80 | opcode, 0x80 | plen])
        elif plen < 65536:
            hdr = bytes([0x80 | opcode, 0x80 | 126]) + plen.to_bytes(2, "big")
        else:
            hdr = bytes([0x80 | opcode, 0x80 | 127]) + plen.to_bytes(8, "big")
        masked = bytes(b ^ mask[i & 3] for i, b in enumerate(payload))
        return hdr + mask + masked

    def _encode_server_ws_frame(opcode: int, payload: bytes) -> bytes:
        plen = len(payload)
        if plen < 126:
            hdr = bytes([0x80 | opcode, plen])
        elif plen < 65536:
            hdr = bytes([0x80 | opcode, 126]) + plen.to_bytes(2, "big")
        else:
            hdr = bytes([0x80 | opcode, 127]) + plen.to_bytes(8, "big")
        return hdr + payload

    async def _read_ws_frame(r: asyncio.StreamReader) -> tuple[int, bytes]:
        h2 = await r.readexactly(2)
        opcode = h2[0] & 0x0F
        masked = bool(h2[1] & 0x80)
        l7 = h2[1] & 0x7F
        if l7 == 126:
            plen = int.from_bytes(await r.readexactly(2), "big")
        elif l7 == 127:
            plen = int.from_bytes(await r.readexactly(8), "big")
        else:
            plen = l7
        mkey = await r.readexactly(4) if masked else b""
        data = await r.readexactly(plen) if plen else b""
        if masked:
            data = bytes(b ^ mkey[i & 3] for i, b in enumerate(data))
        return opcode, data

    async def _run():
        pings_seen: list[bytes] = []
        large_prompt = b'{"type":"response.create","input":"' + (b"A" * 70_000) + b'"}'

        async def handle_upstream(up_r: asyncio.StreamReader, up_w: asyncio.StreamWriter):
            req = await up_r.readuntil(b"\r\n\r\n")
            assert b"Upgrade: websocket" in req
            up_w.write(
                b"HTTP/1.1 101 Switching Protocols\r\n"
                b"Upgrade: websocket\r\n"
                b"Connection: Upgrade\r\n\r\n"
            )
            await up_w.drain()

            op, body = await _read_ws_frame(up_r)
            assert op == 0x01
            assert body == large_prompt

            # 静默思考期：接收 2 个由代理注入的掩码 Ping 并回复 Pong
            for _ in range(2):
                p_op, p_body = await _read_ws_frame(up_r)
                assert p_op == 0x09
                pings_seen.append(p_body)
                up_w.write(_encode_server_ws_frame(0x0A, p_body))
                await up_w.drain()

            # 发送一个非 cproxy 的业务 Pong，随后发送 response.completed 终态帧
            up_w.write(_encode_server_ws_frame(0x0A, b"userpong"))
            completed_json = b'{"type":"response.completed","response":{"id":"resp_ok"}}'
            up_w.write(_encode_server_ws_frame(0x01, completed_json))
            await up_w.drain()

            # 终态帧之后进入长空闲阈值 (idle_ping_interval=10s)，短时间内不应再收到高频 Ping
            await asyncio.sleep(0.18)
            assert len(pings_seen) == 2
            up_w.close()

        up_srv = await asyncio.start_server(handle_upstream, "127.0.0.1", 0)
        up_port = up_srv.sockets[0].getsockname()[1]

        async def handle_proxy_client(c_r: asyncio.StreamReader, c_w: asyncio.StreamWriter):
            u_r, u_w = await asyncio.open_connection("127.0.0.1", up_port)
            try:
                await _forward_http1_or_ws_with_ping(
                    c_r, c_w, u_r, u_w, ping_interval=0.06, idle_ping_interval=10.0
                )
            finally:
                u_w.close()
                c_w.close()

        p_srv = await asyncio.start_server(handle_proxy_client, "127.0.0.1", 0)
        p_port = p_srv.sockets[0].getsockname()[1]

        cr, cw = await asyncio.open_connection("127.0.0.1", p_port)
        cw.write(
            b"GET /backend-api/codex/responses HTTP/1.1\r\n"
            b"Host: chatgpt.com\r\n"
            b"Upgrade: websocket\r\n"
            b"Connection: Upgrade\r\n\r\n"
        )
        await cw.drain()
        resp = await cr.readuntil(b"\r\n\r\n")
        assert b"101 Switching Protocols" in resp

        cw.write(_encode_client_ws_frame(0x01, large_prompt))
        await cw.drain()

        # 客户端只应收到 userpong 与 response.completed，cproxy01/02 的 Pong 已被透明吸收
        f1_op, f1_body = await _read_ws_frame(cr)
        f2_op, f2_body = await _read_ws_frame(cr)
        assert (f1_op, f1_body) == (0x0A, b"userpong")
        assert f2_op == 0x01 and b'"response.completed"' in f2_body
        assert pings_seen == [b"cproxy01", b"cproxy02"]

        cw.close()
        p_srv.close()
        up_srv.close()

        # 同时验证普通 HTTP/1.1 POST 直通不受影响
        async def handle_http_upstream(up_r: asyncio.StreamReader, up_w: asyncio.StreamWriter):
            hdr = await up_r.readuntil(b"\r\n\r\n")
            assert b"POST /backend-api/codex/responses" in hdr
            body = await up_r.readexactly(11)
            assert body == b"hello-codex"
            up_w.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nOK")
            await up_w.drain()
            up_w.close()

        h_up = await asyncio.start_server(handle_http_upstream, "127.0.0.1", 0)
        h_up_port = h_up.sockets[0].getsockname()[1]

        async def handle_http_proxy(c_r: asyncio.StreamReader, c_w: asyncio.StreamWriter):
            u_r, u_w = await asyncio.open_connection("127.0.0.1", h_up_port)
            try:
                await _forward_http1_or_ws_with_ping(c_r, c_w, u_r, u_w, ping_interval=0.05)
            finally:
                u_w.close()
                c_w.close()

        h_px = await asyncio.start_server(handle_http_proxy, "127.0.0.1", 0)
        h_px_port = h_px.sockets[0].getsockname()[1]
        hr, hw = await asyncio.open_connection("127.0.0.1", h_px_port)
        hw.write(b"POST /backend-api/codex/responses HTTP/1.1\r\nHost: chatgpt.com\r\nContent-Length: 11\r\n\r\nhello-codex")
        await hw.drain()
        h_resp = await hr.readuntil(b"\r\n\r\n")
        h_body = await hr.readexactly(2)
        assert b"200 OK" in h_resp and h_body == b"OK"
        hw.close()
        h_px.close()
        h_up.close()

    asyncio.run(_run())


def test_h2_keepalive_proxy_e2e_tls_chatgpt_ws_and_generate204(tmp_path):
    """端到端验证 H2KeepaliveProxy：
    1. 多域名 SAN 证书覆盖 chatgpt.com / ws.chatgpt.com / daily-cloudcode-pa.googleapis.com；
    2. HTTP generate_204 探针返回 204（供 Clash Verge AI-MANUAL-KEEPALIVE fallback 组毫秒级探活）；
    3. CONNECT chatgpt.com:443 -> TLS 终结 -> SOCKS5 上游 -> TLS -> WebSocket 101 + Ping 保活全链路闭环。
    """
    import asyncio
    import os
    import ssl

    from cproxy.services.h2_keepalive import H2KeepaliveProxy

    proxy = H2KeepaliveProxy(
        tmp_path / "tls",
        install_system_trust=False,
        require_system_trust=False,
        ping_interval=0.06,
        idle_ping_interval=10.0,
    )
    proxy.client_ssl = ssl.create_default_context(cafile=str(proxy.ca_cert_path))
    proxy.client_ssl.set_alpn_protocols(["h2", "http/1.1"])
    proxy.client_ssl_h1 = ssl.create_default_context(cafile=str(proxy.ca_cert_path))
    proxy.client_ssl_h1.set_alpn_protocols(["http/1.1"])

    upstream_tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    upstream_tls.load_cert_chain(
        certfile=str(tmp_path / "tls" / "cloudcode.crt"),
        keyfile=str(tmp_path / "tls" / "cloudcode.key"),
    )
    upstream_tls.set_alpn_protocols(["http/1.1"])

    async def _run():
        ws_pings: list[bytes] = []

        async def mock_socks5_upstream(r: asyncio.StreamReader, w: asyncio.StreamWriter):
            # SOCKS5 greeting & CONNECT chatgpt.com:443
            assert await r.readexactly(3) == b"\x05\x01\x00"
            w.write(b"\x05\x00")
            await w.drain()
            hdr = await r.readexactly(4)
            assert hdr[:4] == b"\x05\x01\x00\x03"
            dlen = (await r.readexactly(1))[0]
            host_bytes = await r.readexactly(dlen)
            port_bytes = await r.readexactly(2)
            assert host_bytes == b"chatgpt.com" and int.from_bytes(port_bytes, "big") == 443
            w.write(b"\x05\x00\x00\x01\x7f\x00\x00\x01\x01\xbb")
            await w.drain()

            loop = asyncio.get_running_loop()
            tls_t = await loop.start_tls(w.transport, w._protocol, upstream_tls, server_side=True)
            w._transport = tls_t
            r._transport = tls_t
            w._protocol._over_ssl = True

            req = await r.readuntil(b"\r\n\r\n")
            assert b"Upgrade: websocket" in req
            w.write(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n\r\n")
            await w.drain()

            # Read client text frame: {"type":"response.create"}
            ch2 = await r.readexactly(2)
            assert (ch2[0] & 0x0F) == 0x01 and (ch2[1] & 0x80)
            clen = ch2[1] & 0x7F
            cmask = await r.readexactly(4)
            cdata = bytes(b ^ cmask[i & 3] for i, b in enumerate(await r.readexactly(clen)))
            assert cdata == b'{"type":"response.create"}'

            # Receive injected WebSocket Ping & reply Pong
            ph2 = await r.readexactly(2)
            assert ph2[0] == 0x89 and (ph2[1] & 0x80)
            plen = ph2[1] & 0x7F
            pmask = await r.readexactly(4)
            pdata = bytes(b ^ pmask[i & 3] for i, b in enumerate(await r.readexactly(plen)))
            ws_pings.append(pdata)
            w.write(bytes([0x8A, len(pdata)]) + pdata)

            reply = b'{"type":"response.completed"}'
            w.write(bytes([0x81, len(reply)]) + reply)
            await w.drain()
            await asyncio.sleep(0.05)
            w.close()

        socks_srv = await asyncio.start_server(mock_socks5_upstream, "127.0.0.1", 0)
        proxy.upstream_port = socks_srv.sockets[0].getsockname()[1]

        px_srv = await asyncio.start_server(proxy.handle_client, "127.0.0.1", 0)
        px_port = px_srv.sockets[0].getsockname()[1]

        # 1. 验证 generate_204 本地毫秒探活
        gr, gw = await asyncio.open_connection("127.0.0.1", px_port)
        gw.write(b"GET http://cp.cloudflare.com/generate_204 HTTP/1.1\r\nHost: cp.cloudflare.com\r\n\r\n")
        await gw.drain()
        g_resp = await gr.readuntil(b"\r\n\r\n")
        assert b"204 No Content" in g_resp
        gw.close()

        # 2. 验证 CONNECT chatgpt.com:443 + ws.chatgpt.com SAN 校验 + WebSocket Ping 保活
        cli_ctx = ssl.create_default_context(cafile=str(proxy.ca_cert_path))
        cli_ctx.set_alpn_protocols(["http/1.1"])

        cr, cw = await asyncio.open_connection("127.0.0.1", px_port)
        cw.write(b"CONNECT chatgpt.com:443 HTTP/1.1\r\nHost: chatgpt.com:443\r\n\r\n")
        await cw.drain()
        c_est = await cr.readuntil(b"\r\n\r\n")
        assert b"200 Connection Established" in c_est

        loop = asyncio.get_running_loop()
        c_tls = await loop.start_tls(
            cw.transport, cw._protocol, cli_ctx, server_side=False, server_hostname="chatgpt.com"
        )
        cw._transport = c_tls
        cr._transport = c_tls
        cw._protocol._over_ssl = True

        cw.write(
            b"GET /backend-api/codex/responses HTTP/1.1\r\n"
            b"Host: chatgpt.com\r\n"
            b"Upgrade: websocket\r\n"
            b"Connection: Upgrade\r\n\r\n"
        )
        await cw.drain()
        assert b"101 Switching Protocols" in (await cr.readuntil(b"\r\n\r\n"))

        msg = b'{"type":"response.create"}'
        mkey = os.urandom(4)
        cw.write(bytes([0x81, 0x80 | len(msg)]) + mkey + bytes(b ^ mkey[i & 3] for i, b in enumerate(msg)))
        await cw.drain()

        rh2 = await cr.readexactly(2)
        rbody = await cr.readexactly(rh2[1] & 0x7F)
        assert (rh2[0] & 0x0F) == 0x01 and rbody == b'{"type":"response.completed"}'
        assert ws_pings == [b"cproxy01"]

        cw.close()
        px_srv.close()
        socks_srv.close()

    asyncio.run(_run())


def test_codex_80s_silent_ws_10054_exemption_and_verge_keepalive_topology(tmp_path):
    """回归保护：
    1. Codex WebSocket 握手成功 (status=101) 后等待 72~81s 因超大上下文思考或 remote compaction v2
       触发 os error 10054 / websocket closed by server 时，detect_recent_codex_retry_failure 豁免误切；
    2. 真实死节点快速断连（如握手后 6s 断连）仍正常触发故障检测；
    3. 35s 轮次间空闲保活微包 (<=128B) 不会误锁住 has_active_ai_transfer；
    4. clash-verge-script.js 注入 H2-KEEPALIVE、AI-MANUAL-KEEPALIVE、codex.exe / ChatGPT.exe 规则。
    """
    import sqlite3
    import time

    from cproxy.services.ai_watch import detect_recent_codex_retry_failure

    codex_dir = tmp_path / "codex-10054"
    codex_dir.mkdir()
    db_file = codex_dir / "logs_2.sqlite"
    now_epoch = int(time.time())
    conn = sqlite3.connect(str(db_file))
    conn.execute(
        "CREATE TABLE logs ("
        "id INTEGER PRIMARY KEY, ts INTEGER, ts_nanos INTEGER, level TEXT, target TEXT, feedback_log_body TEXT)"
    )
    # 模拟真实的 81.005s 长上下文静默断连 (status=101 -> 81s -> os error 10054)
    conn.execute(
        "INSERT INTO logs VALUES (1, ?, 100000000, 'INFO', 'codex_api::endpoint::responses_websocket', "
        "'successfully connected to Responses WebSocket status=101 Switching Protocols')",
        (now_epoch - 83,),
    )
    conn.execute(
        "INSERT INTO logs VALUES (2, ?, 105000000, 'WARN', 'codex_core::responses_retry', "
        "'stream disconnected - retrying sampling request (5/5 in 3.77s)... retries=5 max_retries=5 "
        "sampling_error=stream disconnected before completion: IO error: 远程主机强迫关闭了一个现有的连接。 (os error 10054)')",
        (now_epoch - 2,),
    )
    conn.commit()
    conn.close()

    failed_81s, _, _ = detect_recent_codex_retry_failure(codex_dir, now=float(now_epoch))
    assert failed_81s is False

    # 若仅过 6s 即断连（真实死节点/阻断），仍立即触发自愈切流
    conn = sqlite3.connect(str(db_file))
    conn.execute(
        "INSERT INTO logs VALUES (3, ?, 100000000, 'INFO', 'codex_api::endpoint::responses_websocket', "
        "'successfully connected to Responses WebSocket status=101 Switching Protocols')",
        (now_epoch - 7,),
    )
    conn.execute(
        "INSERT INTO logs VALUES (4, ?, 200000000, 'WARN', 'codex_core::responses_retry', "
        "'stream disconnected - retrying sampling request (3/5 in 1.2s)... retries=3 max_retries=5 "
        "sampling_error=stream disconnected before completion: connection reset')",
        (now_epoch - 1,),
    )
    conn.commit()
    conn.close()

    failed_fast, _, reason_fast = detect_recent_codex_retry_failure(codex_dir, now=float(now_epoch))
    assert failed_fast is True
    assert "connection reset" in reason_fast

    # 验证 35s 轮次间 <=128B 空闲保活心跳不会刷新已闲置连接的活跃时间戳
    paths = _paths(tmp_path)
    conns = [
        {
            "id": "ws-idle-codex",
            "chains": ["🇺🇸 United States 02", "AI-MANUAL"],
            "upload": 150_000,
            "download": 45_000,
            "metadata": {"host": "chatgpt.com", "sourcePort": "54321"},
        }
    ]
    t0 = 1_800_000_000.0
    track_connections_idle_ms(paths, conns, now=t0)
    # t0 + 40s：已完成轮次进入静默 (>= 35s)
    idle_40 = track_connections_idle_ms(paths, conns, now=t0 + 40.0)
    assert min(idle_40["54321"]) >= 39_000
    # t0 + 43s：发生一次 36B Ping + 32B Pong 空闲保活心跳，连接仍应保持 >= 42s 闲置，不误判为活跃传输
    conns[0]["upload"] += 36
    conns[0]["download"] += 32
    idle_43 = track_connections_idle_ms(paths, conns, now=t0 + 43.0)
    assert min(idle_43["54321"]) >= 42_000

    # 验证 clash-verge-script.js 注入保活策略组与 Codex 进程/域名规则
    js = export_verge_script()
    assert '"AI-MANUAL-KEEPALIVE"' in js
    assert '"AI-GEMINI-KEEPALIVE"' in js
    assert '"H2-KEEPALIVE"' in js
    assert '"PROCESS-NAME,codex.exe,AI-MANUAL"' in js
    assert '"PROCESS-NAME,ChatGPT.exe,AI-MANUAL"' in js


def test_try_hot_reload_verge_runtime_compiles_scripts_and_preserves_egress(tmp_path, monkeypatch):
    """验证 try_hot_reload_verge_runtime 能用 node 串联执行全局 Script.js 与订阅局部脚本、
    保持当前 AI-MANUAL/AI-GEMINI 出口首位不变并通过 PUT /configs?force=true 热挂载。
    """
    import shutil

    import yaml

    from cproxy.backend.api import APIBackend, ProxyGroup
    from cproxy.services.verge import inject_verge_global_script, try_hot_reload_verge_runtime

    if not shutil.which("node"):
        pytest.skip("node not installed")

    paths = _paths(tmp_path)
    verge_dir = tmp_path / "verge"
    (verge_dir / "profiles").mkdir(parents=True)
    inject_verge_global_script(verge_dir)

    # 模拟一个订阅局部脚本（注入局部规则与本地回环代理）
    (verge_dir / "profiles" / "local_sub.js").write_text(
        "function main(cfg) {\n"
        "  cfg.proxies = cfg.proxies || [];\n"
        "  cfg.proxies.push({name: 'MITM-LOCAL', type: 'socks5', server: '127.0.0.1', port: 7893});\n"
        "  cfg.rules = ['PROCESS-NAME,steam.exe,DIRECT', ...(cfg.rules || [])];\n"
        "  return cfg;\n"
        "}\n",
        encoding="utf-8",
    )
    (verge_dir / "profiles.yaml").write_text(
        yaml.safe_dump(
            {
                "current": "Sub1",
                "items": [
                    {"uid": "Script", "type": "script", "file": "Script.js"},
                    {"uid": "sLocal", "type": "script", "file": "local_sub.js"},
                    {"uid": "Sub1", "type": "remote", "name": "TestSub", "option": {"script": "sLocal"}},
                ],
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    (verge_dir / "clash-verge.yaml").write_text(
        yaml.safe_dump(
            {
                "mixed-port": 7897,
                "proxies": [
                    {"name": "🇺🇸 US 01", "type": "ss", "server": "1.1.1.1", "port": 443},
                    {"name": "🇺🇸 US 02", "type": "ss", "server": "1.1.1.2", "port": 443},
                    {"name": "🇨🇳 TW 01", "type": "ss", "server": "2.2.2.1", "port": 443},
                ],
                "proxy-groups": [
                    {"name": "Proxy", "type": "select", "proxies": ["🇺🇸 US 01", "🇺🇸 US 02"]},
                ],
                "rule-providers": {
                    "Apple": {"type": "http", "path": "./ruleset/Apple.yaml"},
                },
                "rules": ["MATCH,Proxy"],
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )

    captured: dict[str, object] = {}

    monkeypatch.setattr(
        APIBackend,
        "get_groups",
        lambda self: {
            "AI-MANUAL": ProxyGroup(
                name="AI-MANUAL", type="Fallback", current="🇺🇸 US 02", candidates=["🇺🇸 US 01", "🇺🇸 US 02"]
            ),
            "AI-GEMINI": ProxyGroup(
                name="AI-GEMINI", type="Fallback", current="🇨🇳 TW 01", candidates=["🇨🇳 TW 01"]
            ),
            "Proxy": ProxyGroup(
                name="Proxy", type="Selector", current="🇺🇸 US 02", candidates=["🇺🇸 US 01", "🇺🇸 US 02"]
            ),
        },
    )
    monkeypatch.setattr(APIBackend, "selector_now", lambda self: {"Proxy": "🇺🇸 US 02"})

    def _fake_request(self, method, path, payload=None, **kwargs):
        if method == "PUT" and path == "/configs?force=true":
            captured["payload"] = payload
        return {}

    def _fake_restore(self, snapshot):
        captured["restored"] = snapshot

    monkeypatch.setattr(APIBackend, "request", _fake_request)
    monkeypatch.setattr(APIBackend, "restore_selectors", _fake_restore)
    monkeypatch.setattr(APIBackend, "delay_test", lambda self, *a, **kw: {"delay": 1})

    ok = try_hot_reload_verge_runtime(paths, verge_dir)
    assert ok is True
    assert captured.get("restored") == {"Proxy": "🇺🇸 US 02"}

    raw_payload = captured["payload"]
    assert isinstance(raw_payload, dict)
    loaded = yaml.safe_load(raw_payload["payload"])
    groups = {g["name"]: g for g in loaded["proxy-groups"]}
    assert "AI-MANUAL-KEEPALIVE" in groups
    assert "AI-GEMINI-KEEPALIVE" in groups
    # 当前正在使用的 🇺🇸 US 02 必须保持在 AI-MANUAL 首位，且本地回环代理 MITM-LOCAL 不混入 AI 候选池
    assert groups["AI-MANUAL"]["proxies"][0] == "🇺🇸 US 02"
    assert "MITM-LOCAL" not in groups["AI-MANUAL"]["proxies"]
    assert loaded["rule-providers"]["Apple"]["path"] == "ruleset/Apple.yaml"
    assert loaded["rules"][0] == "PROCESS-NAME,steam.exe,DIRECT"


def test_keepalive_multi_protocol_telemetry_and_summary(tmp_path, monkeypatch):
    """验证保活网关对 WS / HTTP1.1 / 旧进程直通的多维活跃与累计统计及摘要展示。"""
    import asyncio
    import ssl

    from cproxy.services import h2_keepalive as h2_mod
    from cproxy.services.h2_keepalive import (
        H2KeepaliveProxy,
        h2_keepalive_stats_file,
        load_h2_keepalive_stats,
    )
    from cproxy.services.verge import format_h2_keepalive_summary

    stats_path = h2_keepalive_stats_file(tmp_path)
    proxy = H2KeepaliveProxy(
        tmp_path / "tls",
        install_system_trust=False,
        require_system_trust=False,
        ping_interval=0.05,
        idle_ping_interval=10.0,
        stats_path=stats_path,
    )
    proxy.client_ssl = ssl.create_default_context(cafile=str(proxy.ca_cert_path))
    proxy.client_ssl.set_alpn_protocols(["h2", "http/1.1"])
    proxy.client_ssl_h1 = ssl.create_default_context(cafile=str(proxy.ca_cert_path))
    proxy.client_ssl_h1.set_alpn_protocols(["http/1.1"])

    upstream_tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    upstream_tls.load_cert_chain(
        certfile=str(tmp_path / "tls" / "cloudcode.crt"),
        keyfile=str(tmp_path / "tls" / "cloudcode.key"),
    )
    upstream_tls.set_alpn_protocols(["http/1.1"])

    prefer_passthrough = {"val": False}
    monkeypatch.setattr(h2_mod, "client_prefers_passthrough", lambda *_a, **_kw: prefer_passthrough["val"])

    async def _run():
        hold_pass = asyncio.Event()
        pass_entered = asyncio.Event()

        async def mock_socks5_upstream(r: asyncio.StreamReader, w: asyncio.StreamWriter):
            assert await r.readexactly(3) == b"\x05\x01\x00"
            w.write(b"\x05\x00")
            await w.drain()
            await r.readexactly(4)
            dlen = (await r.readexactly(1))[0]
            host = (await r.readexactly(dlen)).decode("ascii")
            await r.readexactly(2)
            w.write(b"\x05\x00\x00\x01\x7f\x00\x00\x01\x01\xbb")
            await w.drain()

            if prefer_passthrough["val"]:
                data = await r.readexactly(5)
                assert data == b"hello"
                pass_entered.set()
                await hold_pass.wait()
                w.close()
                return

            loop = asyncio.get_running_loop()
            tls_t = await loop.start_tls(w.transport, w._protocol, upstream_tls, server_side=True)
            w._transport = tls_t
            r._transport = tls_t
            w._protocol._over_ssl = True

            req = await r.readuntil(b"\r\n\r\n")
            if host == "chatgpt.com" and b"Upgrade: websocket" in req:
                w.write(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n\r\n")
                await w.drain()
                # Read client frame & wait for injected WS Ping
                ch2 = await r.readexactly(2)
                clen = ch2[1] & 0x7F
                await r.readexactly(4 + clen)
                ph2 = await r.readexactly(2)
                assert ph2[0] == 0x89
                plen = ph2[1] & 0x7F
                pmask = await r.readexactly(4)
                pdata = bytes(b ^ pmask[i & 3] for i, b in enumerate(await r.readexactly(plen)))
                w.write(bytes([0x8A, len(pdata)]) + pdata)
                reply = b'{"type":"response.completed"}'
                w.write(bytes([0x81, len(reply)]) + reply)
                await w.drain()
                await asyncio.sleep(0.03)
                w.close()
            else:
                w.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")
                await w.drain()
                await asyncio.sleep(0.02)
                w.close()

        socks_srv = await asyncio.start_server(mock_socks5_upstream, "127.0.0.1", 0)
        proxy.upstream_port = socks_srv.sockets[0].getsockname()[1]
        px_srv = await asyncio.start_server(proxy.handle_client, "127.0.0.1", 0)
        px_port = px_srv.sockets[0].getsockname()[1]

        cctx = ssl.create_default_context(cafile=str(proxy.ca_cert_path))
        cctx.set_alpn_protocols(["http/1.1"])
        loop = asyncio.get_running_loop()

        # 1) HTTP/1.1 TLS 终结会话 (例如新 agy 会话)
        cr, cw = await asyncio.open_connection("127.0.0.1", px_port)
        cw.write(b"CONNECT daily-cloudcode-pa.googleapis.com:443 HTTP/1.1\r\nHost: daily-cloudcode-pa.googleapis.com:443\r\n\r\n")
        await cw.drain()
        await cr.readuntil(b"\r\n\r\n")
        ct = await loop.start_tls(
            cw.transport, cw._protocol, cctx, server_side=False, server_hostname="daily-cloudcode-pa.googleapis.com"
        )
        cw._transport = ct
        cr._transport = ct
        cw._protocol._over_ssl = True
        cw.write(b"GET /v1/ping HTTP/1.1\r\nHost: daily-cloudcode-pa.googleapis.com\r\n\r\n")
        await cw.drain()
        await cr.readuntil(b"\r\n\r\n")
        assert await cr.readexactly(2) == b"ok"
        cw.close()
        await asyncio.sleep(0.05)

        # 2) WebSocket 101 保活会话 + Ping 计数 (例如 Codex)
        wr, ww = await asyncio.open_connection("127.0.0.1", px_port)
        ww.write(b"CONNECT chatgpt.com:443 HTTP/1.1\r\nHost: chatgpt.com:443\r\n\r\n")
        await ww.drain()
        await wr.readuntil(b"\r\n\r\n")
        wt = await loop.start_tls(ww.transport, ww._protocol, cctx, server_side=False, server_hostname="chatgpt.com")
        ww._transport = wt
        wr._transport = wt
        ww._protocol._over_ssl = True
        ww.write(
            b"GET /backend-api/codex/responses HTTP/1.1\r\n"
            b"Host: chatgpt.com\r\n"
            b"Upgrade: websocket\r\n"
            b"Connection: Upgrade\r\n\r\n"
        )
        await ww.drain()
        await wr.readuntil(b"\r\n\r\n")
        msg = b'{"type":"response.create"}'
        mkey = b"\x01\x02\x03\x04"
        masked = bytes(b ^ mkey[i & 3] for i, b in enumerate(msg))
        ww.write(bytes([0x81, 0x80 | len(msg)]) + mkey + masked)
        await ww.drain()
        rh = await wr.readexactly(2)
        await wr.readexactly(rh[1] & 0x7F)
        ww.close()
        await asyncio.sleep(0.05)

        # 3) 旧进程直通：连接存续期间 active_passthrough == 1，断开后归零但保留历史累计
        prefer_passthrough["val"] = True
        pr, pw = await asyncio.open_connection("127.0.0.1", px_port)
        pw.write(b"CONNECT daily-cloudcode-pa.googleapis.com:443 HTTP/1.1\r\nHost: daily-cloudcode-pa.googleapis.com:443\r\n\r\n")
        await pw.drain()
        await pr.readuntil(b"\r\n\r\n")
        pw.write(b"hello")
        await pw.drain()
        await asyncio.wait_for(pass_entered.wait(), timeout=2.0)

        live_stats = load_h2_keepalive_stats(tmp_path)
        assert live_stats["http1_sessions"] == 1
        assert live_stats["ws_sessions"] == 1
        assert live_stats["ws_pings_sent"] >= 1
        assert live_stats["pings_sent"] >= 1
        assert live_stats["active_passthrough"] == 1
        assert live_stats["passthrough_sessions"] == 1
        live_summary = format_h2_keepalive_summary(7895, live_stats)
        assert "TLS接管 累计2 (WS:1/H1:1)" in live_summary
        assert "旧进程直通 当前1/累计1" in live_summary

        hold_pass.set()
        pw.close()
        await asyncio.sleep(0.05)

        after_stats = load_h2_keepalive_stats(tmp_path)
        assert after_stats["active_sessions"] == 0
        assert after_stats["active_passthrough"] == 0
        assert after_stats["passthrough_sessions"] == 1
        after_summary = format_h2_keepalive_summary(7895, after_stats)
        assert "旧进程历史直通 1" in after_summary

        px_srv.close()
        socks_srv.close()

    asyncio.run(_run())


def test_h1_to_h2_upstream_bridge_sse_ping_flow_control_and_socks5_reap_guard(tmp_path, monkeypatch):
    """验证 agy (HTTP/1.1 over CONNECT) -> 保活网关 (:7895) -> 上游 Google HTTP/2 自动升级桥接与防误杀：
    1. 客户端仅协商 ALPN http/1.1 时，网关自动对 daily-cloudcode-pa.googleapis.com 发起上游 h2 握手；
    2. >64KB (120KB) 大包请求体受 H2 流控窗口约束分片上传，收到 WINDOW_UPDATE 后完整送达；
    3. 收到 HTTP 200 ResponseID 头后的模型长思考静默期内，网关持续发送 HTTP/2 PING 帧 (cproxy01/02...)；
    4. 上游 H2 SSE 分片零缓冲转为 HTTP/1.1 chunked 回传，且同一客户端连接可继续复用发送第 2 个 chunked 请求；
    5. ai_watch.reap_idle_gemini_connections 在保活网关在线时绝不误杀当前活跃叶子上的 Socks5 出站长连接，但仍清理旧叶子残留。
    """
    import asyncio
    import ssl

    from h2.config import H2Configuration
    from h2.connection import H2Connection
    from h2.events import DataReceived, PingReceived, RequestReceived, StreamEnded

    from cproxy.services import h2_keepalive as h2_mod
    from cproxy.services.h2_keepalive import (
        H2KeepaliveProxy,
        h2_keepalive_stats_file,
        load_h2_keepalive_stats,
    )

    stats_path = h2_keepalive_stats_file(tmp_path)
    proxy = H2KeepaliveProxy(
        tmp_path / "tls",
        install_system_trust=False,
        require_system_trust=False,
        ping_interval=0.045,
        idle_ping_interval=10.0,
        stats_path=stats_path,
    )
    proxy.client_ssl = ssl.create_default_context(cafile=str(proxy.ca_cert_path))
    proxy.client_ssl.set_alpn_protocols(["h2", "http/1.1"])
    proxy.client_ssl_h1 = ssl.create_default_context(cafile=str(proxy.ca_cert_path))
    proxy.client_ssl_h1.set_alpn_protocols(["http/1.1"])

    upstream_tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    upstream_tls.load_cert_chain(
        certfile=str(tmp_path / "tls" / "cloudcode.crt"),
        keyfile=str(tmp_path / "tls" / "cloudcode.key"),
    )
    upstream_tls.set_alpn_protocols(["h2", "http/1.1"])
    monkeypatch.setattr(h2_mod, "client_prefers_passthrough", lambda *_a, **_kw: False)

    pings_received: list[bytes] = []
    streams_seen: dict[int, dict[str, object]] = {}
    large_body = b'{"contents":[{"text":"' + (b"X" * 120_000) + b'"}]}'

    async def _run():
        async def mock_socks5_h2_upstream(r: asyncio.StreamReader, w: asyncio.StreamWriter):
            assert await r.readexactly(3) == b"\x05\x01\x00"
            w.write(b"\x05\x00")
            await w.drain()
            await r.readexactly(4)
            dlen = (await r.readexactly(1))[0]
            host = (await r.readexactly(dlen)).decode("ascii")
            assert host == "daily-cloudcode-pa.googleapis.com"
            await r.readexactly(2)
            w.write(b"\x05\x00\x00\x01\x7f\x00\x00\x01\x01\xbb")
            await w.drain()

            loop = asyncio.get_running_loop()
            tls_t = await loop.start_tls(w.transport, w._protocol, upstream_tls, server_side=True)
            w._transport = tls_t
            r._transport = tls_t
            w._protocol._over_ssl = True
            ssl_obj = tls_t.get_extra_info("ssl_object")
            assert ssl_obj.selected_alpn_protocol() == "h2"

            srv_h2 = H2Connection(config=H2Configuration(client_side=False, header_encoding=None))
            srv_h2.initiate_connection()
            w.write(srv_h2.data_to_send())
            await w.drain()
            srv_lock = asyncio.Lock()

            async def _respond_sse_stream_1(sid: int):
                # 先返回 HTTP 200 头与 ResponseID，然后进入长思考静默期，等待网关注入 >= 2 个 H2 PING
                async with srv_lock:
                    srv_h2.send_headers(
                        sid,
                        [
                            (b":status", b"200"),
                            (b"content-type", b"text/event-stream"),
                            (b"x-goog-ext-response-id", b"resp_h2_ok"),
                        ],
                        end_stream=False,
                    )
                    w.write(srv_h2.data_to_send())
                    await w.drain()

                for _ in range(40):
                    if len(pings_received) >= 2:
                        break
                    await asyncio.sleep(0.01)

                for chunk in (b'data: {"delta":"hello "}\n\n', b'data: {"delta":"world"}\n\n'):
                    async with srv_lock:
                        srv_h2.send_data(sid, chunk, end_stream=False)
                        w.write(srv_h2.data_to_send())
                        await w.drain()
                    await asyncio.sleep(0.01)

                async with srv_lock:
                    srv_h2.end_stream(sid)
                    w.write(srv_h2.data_to_send())
                    await w.drain()

            bg_tasks: list[asyncio.Task[None]] = []
            try:
                while True:
                    raw = await r.read(65536)
                    if not raw:
                        break
                    async with srv_lock:
                        evs = srv_h2.receive_data(raw)
                        for ev in evs:
                            if isinstance(ev, RequestReceived):
                                streams_seen[ev.stream_id] = {
                                    "headers": dict(ev.headers),
                                    "body": bytearray(),
                                }
                            elif isinstance(ev, DataReceived):
                                buf = streams_seen[ev.stream_id]["body"]
                                assert isinstance(buf, bytearray)
                                buf.extend(ev.data)
                                if ev.flow_controlled_length > 0:
                                    srv_h2.acknowledge_received_data(ev.flow_controlled_length, ev.stream_id)
                            elif isinstance(ev, PingReceived):
                                pings_received.append(ev.ping_data)
                            elif isinstance(ev, StreamEnded):
                                if ev.stream_id == 1:
                                    bg_tasks.append(asyncio.create_task(_respond_sse_stream_1(1)))
                                else:
                                    body_json = b'{"totalTokens":42}'
                                    srv_h2.send_headers(
                                        ev.stream_id,
                                        [
                                            (b":status", b"200"),
                                            (b"content-type", b"application/json"),
                                            (b"content-length", str(len(body_json)).encode("ascii")),
                                        ],
                                        end_stream=False,
                                    )
                                    srv_h2.send_data(ev.stream_id, body_json, end_stream=True)
                        out = srv_h2.data_to_send()
                        if out:
                            w.write(out)
                            await w.drain()
            finally:
                for bt in bg_tasks:
                    await asyncio.gather(bt, return_exceptions=True)
                w.close()

        socks_srv = await asyncio.start_server(mock_socks5_h2_upstream, "127.0.0.1", 0)
        proxy.upstream_port = socks_srv.sockets[0].getsockname()[1]
        px_srv = await asyncio.start_server(proxy.handle_client, "127.0.0.1", 0, limit=262144)
        px_port = px_srv.sockets[0].getsockname()[1]

        # 客户端模拟 Go net/http over HTTPS_PROXY CONNECT：仅协商 http/1.1
        cctx = ssl.create_default_context(cafile=str(proxy.ca_cert_path))
        cctx.set_alpn_protocols(["http/1.1"])
        loop = asyncio.get_running_loop()

        cr, cw = await asyncio.open_connection("127.0.0.1", px_port)
        cw.write(
            b"CONNECT daily-cloudcode-pa.googleapis.com:443 HTTP/1.1\r\n"
            b"Host: daily-cloudcode-pa.googleapis.com:443\r\n\r\n"
        )
        await cw.drain()
        connect_resp = await cr.readuntil(b"\r\n\r\n")
        assert b"200 Connection Established" in connect_resp

        ct = await loop.start_tls(
            cw.transport,
            cw._protocol,
            cctx,
            server_side=False,
            server_hostname="daily-cloudcode-pa.googleapis.com",
        )
        cw._transport = ct
        cr._transport = ct
        cw._protocol._over_ssl = True
        assert ct.get_extra_info("ssl_object").selected_alpn_protocol() == "http/1.1"

        # 请求 1: 120KB POST + SSE 流式响应 (触发 H2 WINDOW_UPDATE 流控 + 思考期 H2 PING)
        req1 = (
            b"POST /v1internal:streamGenerateContent?alt=sse HTTP/1.1\r\n"
            b"Host: daily-cloudcode-pa.googleapis.com\r\n"
            b"Connection: keep-alive\r\n"
            b"Content-Type: application/json\r\n"
            + f"Content-Length: {len(large_body)}\r\n\r\n".encode("ascii")
            + large_body
        )
        cw.write(req1)
        await cw.drain()

        resp1_hdr = await cr.readuntil(b"\r\n\r\n")
        assert resp1_hdr.startswith(b"HTTP/1.1 200 OK\r\n")
        assert b"transfer-encoding: chunked\r\n" in resp1_hdr.lower()
        assert b"x-goog-ext-response-id: resp_h2_ok\r\n" in resp1_hdr.lower()
        assert b"connection: keep-alive\r\n" in resp1_hdr.lower()

        sse_acc = bytearray()
        while True:
            sz_line = await cr.readline()
            sz = int(sz_line.strip(), 16)
            if sz == 0:
                assert await cr.readexactly(2) == b"\r\n"
                break
            sse_acc.extend(await cr.readexactly(sz))
            assert await cr.readexactly(2) == b"\r\n"
        assert bytes(sse_acc) == b'data: {"delta":"hello "}\n\ndata: {"delta":"world"}\n\n'

        # 请求 2: 在同一条 HTTP/1.1 TLS 连接上顺序复用，发送 chunked 请求体并读取定长响应
        chunk_part = b'{"prompt":"hi"}'
        req2 = (
            b"POST /v1internal:countTokens HTTP/1.1\r\n"
            b"Host: daily-cloudcode-pa.googleapis.com\r\n"
            b"Transfer-Encoding: chunked\r\n"
            b"Connection: close\r\n\r\n"
            + f"{len(chunk_part):x}\r\n".encode("ascii")
            + chunk_part
            + b"\r\n0\r\n\r\n"
        )
        cw.write(req2)
        await cw.drain()

        resp2_hdr = await cr.readuntil(b"\r\n\r\n")
        assert resp2_hdr.startswith(b"HTTP/1.1 200 OK\r\n")
        assert b"content-length: 18\r\n" in resp2_hdr.lower()
        assert b"connection: close\r\n" in resp2_hdr.lower()
        resp2_body = await cr.readexactly(18)
        assert resp2_body == b'{"totalTokens":42}'

        cw.close()
        await asyncio.sleep(0.05)
        px_srv.close()
        socks_srv.close()

    asyncio.run(_run())

    assert bytes(streams_seen[1]["body"]) == large_body  # type: ignore[arg-type]
    assert streams_seen[1]["headers"][b":path"] == b"/v1internal:streamGenerateContent?alt=sse"  # type: ignore[index]
    assert bytes(streams_seen[3]["body"]) == b'{"prompt":"hi"}'  # type: ignore[arg-type]
    assert len(pings_received) >= 2
    assert pings_received[0] == b"cproxy01"
    assert pings_received[1] == b"cproxy02"

    final_stats = load_h2_keepalive_stats(tmp_path)
    assert final_stats["h2_sessions"] == 1
    assert final_stats["http1_sessions"] == 0
    assert final_stats["pings_sent"] >= 2
    assert final_stats["active_sessions"] == 0

    # 验证 ai_watch 在保活网关在线时保护活跃叶子上的 Socks5 出站长连接，同时正常回收旧叶子 Socks5 与直连闲置连接
    paths = _paths(tmp_path)
    monkeypatch.setattr("cproxy.services.h2_keepalive.is_h2_keepalive_running", lambda _sd, **_kw: True)
    monkeypatch.setattr(
        ai_watch_mod,
        "get_local_proxy_socket_idle_ms",
        lambda _port=7890: {
            "61001": (45_000, 45_000),
            "61002": (45_000, 45_000),
            "61003": (45_000, 45_000),
        },
    )
    mock_conns = [
        {
            "id": "socks5-active-leaf",
            "chains": ["🇬🇧英国 01 | 1X", "AI-GEMINI"],
            "upload": 220_000,
            "download": 95_000,
            "metadata": {
                "type": "Socks5",
                "host": "daily-cloudcode-pa.googleapis.com",
                "sourcePort": "61001",
            },
        },
        {
            "id": "socks5-stale-leaf",
            "chains": ["🇺🇸美国 02 | 1X", "AI-GEMINI"],
            "upload": 220_000,
            "download": 95_000,
            "metadata": {
                "type": "Socks5",
                "host": "daily-cloudcode-pa.googleapis.com",
                "sourcePort": "61002",
            },
        },
        {
            "id": "https-direct-idle",
            "chains": ["🇬🇧英国 01 | 1X", "AI-GEMINI"],
            "upload": 220_000,
            "download": 95_000,
            "metadata": {
                "type": "HTTPS",
                "host": "daily-cloudcode-pa.googleapis.com",
                "sourcePort": "61003",
            },
        },
    ]
    closed_ids: list[str] = []

    class _GuardAPI:
        def get_connections(self):
            return {"connections": mock_conns}

        def close_connection(self, cid: str):
            closed_ids.append(cid)

    reaped = reap_idle_gemini_connections(paths, _GuardAPI(), current_leaf="🇬🇧英国 01 | 1X")
    assert reaped == ["socks5-stale-leaf", "https-direct-idle"]
    assert "socks5-active-leaf" not in closed_ids


def test_h2_keepalive_default_intervals_strike_cap_and_sse_boundary_heartbeat(tmp_path):
    """验证：
    1. 生产默认参数将 WebSocket (4.5s) 与 HTTP/2 (25.0s) PING 周期解耦；
    2. 面对 Google GFE 连续第 23 个无数据 PING 触发 `GOAWAY(ENHANCE_YOUR_CALM, too_many_pings)` 的硬限制，
       网关严格在 `H2_MAX_PINGS_WITHOUT_DATA` (18) 封顶，收到 `DATA` 后自动清零预算继续保活；
    3. `PingAckReceived` 驱动的下游 SSE 空事件心跳仅在完整事件边界 (`\\n\\n`) 注入，半行事件绝不插字节破坏帧语法。
    """
    import asyncio

    from h2.config import H2Configuration
    from h2.connection import H2Connection
    from h2.errors import ErrorCodes
    from h2.events import DataReceived, PingReceived, RequestReceived, StreamEnded

    from cproxy.services.h2_keepalive import (
        H2_MAX_PINGS_WITHOUT_DATA,
        H2_PING_INTERVAL_SEC,
        PING_INTERVAL_SEC,
        SSE_DOWNSTREAM_KEEPALIVE_SEC,
        H2KeepaliveProxy,
        _bridge_http1_client_to_h2_upstream,
    )

    default_proxy = H2KeepaliveProxy(
        tmp_path / "tls",
        install_system_trust=False,
        require_system_trust=False,
    )
    assert default_proxy.ping_interval == PING_INTERVAL_SEC == 4.5
    assert default_proxy.h2_ping_interval == H2_PING_INTERVAL_SEC == 25.0
    assert default_proxy.sse_keepalive_interval == SSE_DOWNSTREAM_KEEPALIVE_SEC == 45.0

    pings_per_epoch: list[int] = [0]
    too_many_pings_triggered = False

    async def _run():
        nonlocal too_many_pings_triggered

        c2p_r, c2p_w = asyncio.StreamReader(), None
        # 使用真实本地 loopback TCP 管道连接 client <-> bridge <-> mock GFE H2 server
        bridge_tasks: list[asyncio.Task[None]] = []

        async def mock_gfe_h2(r: asyncio.StreamReader, w: asyncio.StreamWriter):
            nonlocal too_many_pings_triggered
            srv = H2Connection(config=H2Configuration(client_side=False, header_encoding=None))
            srv.initiate_connection()
            w.write(srv.data_to_send())
            await w.drain()
            srv_lock = asyncio.Lock()
            pings_since_data = 0

            async def _drive_sse(sid: int):
                nonlocal pings_since_data
                try:
                    # 阶段 1: 发送 200 OK SSE 头（处于事件边界），等待达到封顶次数并确认持续静默不再多发（兼容 Windows 15.6ms 时钟粒度）
                    async with srv_lock:
                        pings_since_data = 0
                        srv.send_headers(
                            sid,
                            [(b":status", b"200"), (b"content-type", b"text/event-stream; charset=utf-8")],
                            end_stream=False,
                        )
                        w.write(srv.data_to_send())
                        await w.drain()

                    deadline = asyncio.get_running_loop().time() + 2.0
                    while pings_per_epoch[-1] < H2_MAX_PINGS_WITHOUT_DATA and asyncio.get_running_loop().time() < deadline:
                        await asyncio.sleep(0.02)
                    # 再额外静默等待 6 个以上 ping 周期，确认封顶后绝不继续发送第 19 次 PING
                    await asyncio.sleep(0.20)
                    epoch1_pings = pings_per_epoch[-1]
                    assert epoch1_pings == H2_MAX_PINGS_WITHOUT_DATA

                    # 阶段 2: 发送半行 SSE 数据（非 \n\n 边界），等待若干 ping 周期：
                    # 预算必须因 DATA 清零重计，但下游绝不能在半行中间注入 `\n\n` 心跳！
                    pings_per_epoch.append(0)
                    async with srv_lock:
                        pings_since_data = 0
                        srv.send_data(sid, b'data: {"part":', end_stream=False)
                        w.write(srv.data_to_send())
                        await w.drain()

                    deadline2 = asyncio.get_running_loop().time() + 1.5
                    while pings_per_epoch[-1] < 2 and asyncio.get_running_loop().time() < deadline2:
                        await asyncio.sleep(0.02)

                    # 阶段 3: 补齐后半行并闭合流
                    async with srv_lock:
                        pings_since_data = 0
                        srv.send_data(sid, b"1}\n\n", end_stream=True)
                        w.write(srv.data_to_send())
                        await w.drain()
                except BaseException:
                    w.close()
                    raise

            bg: list[asyncio.Task[None]] = []
            try:
                while True:
                    raw = await r.read(65536)
                    if not raw:
                        break
                    async with srv_lock:
                        for ev in srv.receive_data(raw):
                            if isinstance(ev, (RequestReceived, DataReceived)):
                                pings_since_data = 0
                            elif isinstance(ev, PingReceived):
                                pings_since_data += 1
                                pings_per_epoch[-1] += 1
                                if pings_since_data >= 23:
                                    too_many_pings_triggered = True
                                    srv.close_connection(
                                        error_code=ErrorCodes.ENHANCE_YOUR_CALM,
                                        additional_data=b"too_many_pings",
                                    )
                            elif isinstance(ev, StreamEnded) and ev.stream_id == 1:
                                bg.append(asyncio.create_task(_drive_sse(1)))
                        out = srv.data_to_send()
                        if out:
                            w.write(out)
                            await w.drain()
                        if too_many_pings_triggered:
                            break
            finally:
                for t in bg:
                    await asyncio.gather(t, return_exceptions=True)
                w.close()

        gfe_srv = await asyncio.start_server(mock_gfe_h2, "127.0.0.1", 0)
        gfe_port = gfe_srv.sockets[0].getsockname()[1]

        async def bridge_handler(cr: asyncio.StreamReader, cw: asyncio.StreamWriter):
            ur, uw = await asyncio.open_connection("127.0.0.1", gfe_port)
            t = asyncio.current_task()
            if t is not None:
                bridge_tasks.append(t)
            try:
                await _bridge_http1_client_to_h2_upstream(
                    cr,
                    cw,
                    ur,
                    uw,
                    target_host="daily-cloudcode-pa.googleapis.com",
                    ping_interval=0.02,
                    idle_ping_interval=10.0,
                    sse_keepalive_interval=0.05,
                )
            finally:
                cw.close()
                uw.close()

        br_srv = await asyncio.start_server(bridge_handler, "127.0.0.1", 0)
        br_port = br_srv.sockets[0].getsockname()[1]

        cr, cw = await asyncio.open_connection("127.0.0.1", br_port)
        cw.write(
            b"POST /v1internal:streamGenerateContent?alt=sse HTTP/1.1\r\n"
            b"Host: daily-cloudcode-pa.googleapis.com\r\n"
            b"Connection: close\r\n"
            b"Content-Length: 2\r\n\r\n{}"
        )
        await cw.drain()

        hdr = await cr.readuntil(b"\r\n\r\n")
        assert hdr.startswith(b"HTTP/1.1 200 OK\r\n")

        chunks: list[bytes] = []
        while True:
            sz_line = await cr.readline()
            assert sz_line, "Expected clean chunked terminator 0\\r\\n\\r\\n, got unexpected EOF"
            sz = int(sz_line.strip(), 16)
            if sz == 0:
                assert await cr.readexactly(2) == b"\r\n"
                break
            chunks.append(await cr.readexactly(sz))
            assert await cr.readexactly(2) == b"\r\n"

        cw.close()
        br_srv.close()
        gfe_srv.close()
        if bridge_tasks:
            await asyncio.gather(*bridge_tasks, return_exceptions=True)

        assert not too_many_pings_triggered
        # 阶段 1 封顶 18 次；阶段 2 收到 DATA 后重置计数并继续发送新 PING
        assert pings_per_epoch[0] == H2_MAX_PINGS_WITHOUT_DATA
        assert pings_per_epoch[1] >= 2
        # 阶段 1 在事件边界至少注入过一次 `\n\n` 心跳；阶段 2 半行期间绝不插入 `\n\n` 打断 JSON 行
        assert b"\n\n" in chunks
        full_sse = b"".join(chunks)
        assert b'data: {"part":1}\n\n' in full_sse
        assert full_sse.strip(b"\n") == b'data: {"part":1}'

    asyncio.run(_run())


def test_h1_to_h2_graceful_goaway_drains_inflight_sse_without_eof():
    """验证上游在流式响应中途发送优雅 `GOAWAY(NO_ERROR, last_stream_id=1)`（独立包及与 DATA+END_STREAM 粘包两种情形）时：
    1. `pinger()` 立即停止发 PING 且不会取消正在收尾的 `u2c()`；
    2. `_GracefulClientH2Connection` 不会因 `ConnectionState.CLOSED` 抛 `ProtocolError` 丢弃在途 `DATA`；
    3. 下游客户端完整收到全部 SSE 事件与 `0\\r\\n\\r\\n`，零 `unexpected EOF`。
    """
    import asyncio

    from h2.config import H2Configuration
    from h2.connection import H2Connection
    from h2.events import PingReceived, StreamEnded
    from hyperframe.frame import DataFrame, GoAwayFrame

    from cproxy.services.h2_keepalive import _bridge_http1_client_to_h2_upstream

    async def _run_case(coalesced: bool) -> tuple[bytes, int]:
        pings_after_goaway = 0
        goaway_sent = False

        async def mock_upstream(r: asyncio.StreamReader, w: asyncio.StreamWriter):
            nonlocal pings_after_goaway, goaway_sent
            srv = H2Connection(config=H2Configuration(client_side=False, header_encoding=None))
            srv.initiate_connection()
            w.write(srv.data_to_send())
            await w.drain()
            srv_lock = asyncio.Lock()

            async def _respond(sid: int):
                nonlocal goaway_sent
                async with srv_lock:
                    srv.send_headers(
                        sid,
                        [(b":status", b"200"), (b"content-type", b"text/event-stream")],
                        end_stream=False,
                    )
                    srv.send_data(sid, b"data: first\n\n", end_stream=False)
                    w.write(srv.data_to_send())
                    await w.drain()

                gf = GoAwayFrame(0, last_stream_id=sid, error_code=0, additional_data=b"graceful_drain")
                df = DataFrame(sid, data=b"data: second\n\n")
                df.flags.add("END_STREAM")

                if coalesced:
                    await asyncio.sleep(0.02)
                    async with srv_lock:
                        goaway_sent = True
                        w.write(gf.serialize() + df.serialize())
                        await w.drain()
                else:
                    async with srv_lock:
                        goaway_sent = True
                        w.write(gf.serialize())
                        await w.drain()
                    # 等待超过 3 个 ping 周期，确认 pinger 在 GOAWAY 后不再发 PING 也不会杀死 u2c
                    await asyncio.sleep(0.12)
                    async with srv_lock:
                        w.write(df.serialize())
                        await w.drain()

            bg: list[asyncio.Task[None]] = []
            try:
                while True:
                    raw = await r.read(65536)
                    if not raw:
                        break
                    async with srv_lock:
                        for ev in srv.receive_data(raw):
                            if isinstance(ev, PingReceived) and goaway_sent:
                                pings_after_goaway += 1
                            elif isinstance(ev, StreamEnded) and ev.stream_id == 1:
                                bg.append(asyncio.create_task(_respond(1)))
                        out = srv.data_to_send()
                        if out:
                            w.write(out)
                            await w.drain()
            finally:
                for t in bg:
                    await asyncio.gather(t, return_exceptions=True)
                w.close()

        up_srv = await asyncio.start_server(mock_upstream, "127.0.0.1", 0)
        up_port = up_srv.sockets[0].getsockname()[1]

        async def bridge_handler(cr: asyncio.StreamReader, cw: asyncio.StreamWriter):
            ur, uw = await asyncio.open_connection("127.0.0.1", up_port)
            try:
                await _bridge_http1_client_to_h2_upstream(
                    cr,
                    cw,
                    ur,
                    uw,
                    target_host="daily-cloudcode-pa.googleapis.com",
                    ping_interval=0.06 if coalesced else 0.025,
                    idle_ping_interval=10.0,
                    sse_keepalive_interval=10.0,
                )
            finally:
                cw.close()
                uw.close()

        br_srv = await asyncio.start_server(bridge_handler, "127.0.0.1", 0)
        br_port = br_srv.sockets[0].getsockname()[1]

        cr, cw = await asyncio.open_connection("127.0.0.1", br_port)
        cw.write(
            b"POST /v1internal:streamGenerateContent?alt=sse HTTP/1.1\r\n"
            b"Host: daily-cloudcode-pa.googleapis.com\r\n"
            b"Connection: keep-alive\r\n"
            b"Content-Length: 2\r\n\r\n{}"
        )
        await cw.drain()

        hdr = await cr.readuntil(b"\r\n\r\n")
        assert hdr.startswith(b"HTTP/1.1 200 OK\r\n")
        acc = bytearray()
        while True:
            sz_line = await cr.readline()
            assert sz_line, "Unexpected EOF before 0\\r\\n\\r\\n"
            sz = int(sz_line.strip(), 16)
            if sz == 0:
                assert await cr.readexactly(2) == b"\r\n"
                break
            acc.extend(await cr.readexactly(sz))
            assert await cr.readexactly(2) == b"\r\n"

        cw.close()
        br_srv.close()
        up_srv.close()
        return bytes(acc), pings_after_goaway

    body_sep, pings_sep = asyncio.run(_run_case(coalesced=False))
    assert body_sep == b"data: first\n\ndata: second\n\n"
    assert pings_sep == 0

    body_coal, pings_coal = asyncio.run(_run_case(coalesced=True))
    assert body_coal == b"data: first\n\ndata: second\n\n"
    assert pings_coal == 0


def test_agy_log_geo_block_and_transport_crash_auto_bans_and_switches(tmp_path, monkeypatch):
    """回归保护（广播脏 IP / poll.go RU 地区封锁 / HTTP2 ClientConn.Close 根治）：
    1. 即使未鉴权 HTTP 探测全部返回 ok（如 `🇬🇧 United Kingdom 03` 公网显示 GB，但 Google Cloud 鉴权表判定为 `RU`）
       且末尾仍有部分非受限模型的 `ResponseID:`，`detect_recent_agy_geo_block` 仍能从 `poll.go` / `errorreport.go`
       捕获 `Detected location: RU` / `User location is not supported`；
    2. `AIWatchService.watch()` 首次命中地区封锁即立判 `geo_blocked`、立即强切冲洗连接池、写入 12h 隔离池
       (`gemini_geo_blocked` + `gemini_geo_log_blocked`) 并在 `ai-gemini-order.json` 沉底；且后续未鉴权 `PROBE_OK`
       绝不会误清由真实日志鉴权捕获的地区隔离；
    3. `errorreport.go` 的 `http2: client connection force closed via ClientConn.Close` 被识别为底层断流触发切流，
       而用户主动取消的 `context canceled` 绝不误触发。
    """
    import os
    import time
    from datetime import datetime

    from cproxy.config import ai_gemini_order_file
    from cproxy.services.ai_watch import (
        AIWatchService,
        detect_recent_agy_geo_block,
        detect_recent_agy_retry_failure,
    )
    from cproxy.services.probe import is_gemini_geo_blocked

    paths = _paths(tmp_path)
    ai_gemini_order_file(paths).parent.mkdir(parents=True, exist_ok=True)
    ai_gemini_order_file(paths).write_text(
        json.dumps(["🇬🇧 United Kingdom 03", "🇨🇳 Taiwan 01", "🇰🇷 South Korea 01"], ensure_ascii=False),
        encoding="utf-8",
    )

    log_dir = tmp_path / "antigravity-logs"
    log_dir.mkdir(parents=True)
    now_ts = time.time()
    ts_poll = datetime.fromtimestamp(now_ts - 12).strftime("%m%d %H:%M:%S")
    ts_ok = datetime.fromtimestamp(now_ts - 3).strftime("%m%d %H:%M:%S")

    # 场景 1：poll.go 报 Detected location: RU，且随后还有一条 streamGenerateContent ResponseID
    ls_log = log_dir / "language_server.log"
    ls_log.write_text(
        f"E{ts_poll}.120000 18944 poll.go:67] failed to fetch user status: "
        "rpc error: code = PermissionDenied desc = PERMISSION_DENIED (code 403): "
        "Your location is not eligible for this service at this time. Detected location: RU.\n"
        f"I{ts_ok}.500000 18944 http_helpers.go:349] "
        "URL: https://daily-cloudcode-pa.googleapis.com/v1internal:streamGenerateContent?alt=sse "
        "Trace: 0x99 ResponseID: resp_flash_lite_ok\n",
        encoding="utf-8",
    )
    os.utime(ls_log, (now_ts - 2, now_ts - 2))

    hit_geo, _, reason_geo = detect_recent_agy_geo_block(log_dir, now=now_ts, window_seconds=60.0)
    assert hit_geo is True
    assert "RU" in reason_geo

    # 场景 2：AIWatchService 即使主动探测全部返回 ok，仍凭借日志 geo_blocked 首轮切走并写入 12h 隔离 + 沉底
    switched: list[tuple[str, str, bool, bool]] = []
    groups = {
        "AI-MANUAL": ProxyGroup("AI-MANUAL", "fallback", "🇺🇸 United States 01", ["🇺🇸 United States 01"]),
        "AI-GEMINI": ProxyGroup(
            "AI-GEMINI",
            "fallback",
            "🇬🇧 United Kingdom 03",
            ["🇬🇧 United Kingdom 03", "🇨🇳 Taiwan 01", "🇰🇷 South Korea 01"],
        ),
        "🇺🇸 United States 01": ProxyGroup("🇺🇸 United States 01", "ss", None, [], delay=160, alive=True),
        "🇬🇧 United Kingdom 03": ProxyGroup("🇬🇧 United Kingdom 03", "ss", None, [], delay=240, alive=True),
        "🇨🇳 Taiwan 01": ProxyGroup("🇨🇳 Taiwan 01", "ss", None, [], delay=85, alive=True),
        "🇰🇷 South Korea 01": ProxyGroup("🇰🇷 South Korea 01", "ss", None, [], delay=95, alive=True),
    }

    class _FakeQuery:
        def __init__(self, _p):
            self.api = SimpleNamespace(
                get_groups=lambda: groups,
                get_connections=lambda: {"connections": []},
            )

        def switch_group(
            self,
            grp: str,
            target: str,
            *,
            drop_connections: bool = True,
            force_drop: bool = False,
        ):
            switched.append((grp, target, drop_connections, force_drop))
            groups[grp] = ProxyGroup(grp, "fallback", target, groups[grp].candidates)

    monkeypatch.setattr(ai_watch_mod, "QueryService", _FakeQuery)
    monkeypatch.setattr(ai_watch_mod, "_DEFAULT_AI_TARGET_VERDICT", lambda _p, _u: "ok")
    monkeypatch.setattr(ai_watch_mod, "ai_target_verdict", ai_watch_mod._DEFAULT_AI_TARGET_VERDICT)
    monkeypatch.setattr(ai_watch_mod, "reap_idle_gemini_connections", lambda *a, **kw: [])
    monkeypatch.setattr(ai_watch_mod, "reap_stale_agy_processes", lambda *a, **kw: [])

    watch_rep = AIWatchService(paths, agy_log_dir=log_dir).watch()
    gem_res = next(r for r in watch_rep.results if r.group == "AI-GEMINI")
    assert gem_res.verdict == "geo_blocked"
    assert gem_res.switched is True
    assert gem_res.target == "🇨🇳 Taiwan 01"
    assert ("AI-GEMINI", "🇨🇳 Taiwan 01", True, True) in switched

    assert is_gemini_geo_blocked("🇬🇧 United Kingdom 03", paths) is True
    new_order = json.loads(ai_gemini_order_file(paths).read_text(encoding="utf-8"))
    assert new_order[0] == "🇨🇳 Taiwan 01"
    assert new_order[-1] == "🇬🇧 United Kingdom 03"

    # 若外部手动切回该脏节点且未鉴权 HTTP 探测返回 ok，不得误清日志鉴权捕获的 12h 隔离态
    groups["AI-GEMINI"] = ProxyGroup(
        "AI-GEMINI",
        "fallback",
        "🇬🇧 United Kingdom 03",
        ["🇬🇧 United Kingdom 03", "🇨🇳 Taiwan 01", "🇰🇷 South Korea 01"],
    )
    AIWatchService(paths, agy_log_dir=tmp_path / "empty-logs").watch()
    assert is_gemini_geo_blocked("🇬🇧 United Kingdom 03", paths) is True

    # 场景 3：errorreport.go 底层 HTTP/2 ClientConn.Close 断流识别 vs context canceled 豁免
    ts_crash = datetime.fromtimestamp(now_ts - 5).strftime("%m%d %H:%M:%S")
    ls_log.write_text(
        f"E{ts_crash}.200000 18944 errorreport.go:273] agent executor error: "
        'request failed: Post "https://daily-cloudcode-pa.googleapis.com/v1internal:streamGenerateContent?alt=sse": '
        "http2: client connection force closed via ClientConn.Close\n",
        encoding="utf-8",
    )
    os.utime(ls_log, (now_ts - 1, now_ts - 1))
    hit_h2, _, reason_h2 = detect_recent_agy_retry_failure(log_dir, now=now_ts, window_seconds=60.0)
    assert hit_h2 is True
    assert "attempt 4" in reason_h2

    ls_log.write_text(
        f"E{ts_crash}.300000 18944 errorreport.go:273] agent executor error: "
        'request failed: Post "https://daily-cloudcode-pa.googleapis.com/v1internal:streamGenerateContent?alt=sse": '
        "context canceled\n",
        encoding="utf-8",
    )
    os.utime(ls_log, (now_ts, now_ts))
    hit_cancel, _, _ = detect_recent_agy_retry_failure(log_dir, now=now_ts, window_seconds=60.0)
    assert hit_cancel is False


def test_try_hot_reload_verge_runtime_strips_banned_nodes_and_applies_learned_order(tmp_path, monkeypatch):
    """验证 Clash Verge 热重载与深度状态面板会剔除已封禁脏节点并应用已学习的优选顺序。"""
    import shutil

    from cproxy.backend.api import APIBackend
    from cproxy.config import ai_gemini_order_file
    from cproxy.services.ban import add_ban
    from cproxy.services.verge import (
        collect_ai_egress_deep_status,
        inject_verge_global_script,
        try_hot_reload_verge_runtime,
    )

    if not shutil.which("node"):
        pytest.skip("node not installed")

    paths = _paths(tmp_path)
    add_ban(paths, "🇬🇧 United Kingdom 03", hours=24.0, reason="agy_geo_blocked_RU")
    ai_gemini_order_file(paths).parent.mkdir(parents=True, exist_ok=True)
    ai_gemini_order_file(paths).write_text(
        json.dumps(["🇰🇷 South Korea 02", "🇨🇳 Taiwan 01"], ensure_ascii=False),
        encoding="utf-8",
    )

    verge_dir = tmp_path / "verge-ban"
    (verge_dir / "profiles").mkdir(parents=True)
    inject_verge_global_script(verge_dir)
    (verge_dir / "clash-verge.yaml").write_text(
        yaml.safe_dump(
            {
                "mixed-port": 7897,
                "proxies": [
                    {"name": "🇬🇧 United Kingdom 03", "type": "ss", "server": "3.3.3.3", "port": 443},
                    {"name": "🇨🇳 Taiwan 01", "type": "ss", "server": "2.2.2.1", "port": 443},
                    {"name": "🇰🇷 South Korea 02", "type": "ss", "server": "4.4.4.2", "port": 443},
                ],
                "proxy-groups": [
                    {
                        "name": "Proxy",
                        "type": "select",
                        "proxies": ["🇬🇧 United Kingdom 03", "🇨🇳 Taiwan 01", "🇰🇷 South Korea 02"],
                    }
                ],
                "rules": ["MATCH,Proxy"],
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )

    captured: dict[str, object] = {}
    gmap = {
        "AI-MANUAL": ProxyGroup(
            "AI-MANUAL",
            "Fallback",
            "🇬🇧 United Kingdom 03",
            ["🇬🇧 United Kingdom 03", "🇨🇳 Taiwan 01", "🇰🇷 South Korea 02", "AI-AUTO"],
        ),
        "AI-GEMINI": ProxyGroup(
            "AI-GEMINI",
            "Fallback",
            "🇬🇧 United Kingdom 03",
            ["🇬🇧 United Kingdom 03", "🇨🇳 Taiwan 01", "🇰🇷 South Korea 02"],
        ),
    }
    monkeypatch.setattr(APIBackend, "get_groups", lambda self: gmap)
    monkeypatch.setattr(
        APIBackend,
        "selector_now",
        lambda self: {"AI-GEMINI": "🇬🇧 United Kingdom 03", "Proxy": "🇨🇳 Taiwan 01"},
    )
    monkeypatch.setattr(
        APIBackend,
        "request",
        lambda self, method, path, payload=None, **kw: captured.setdefault("payload", payload) or {},
    )
    monkeypatch.setattr(
        APIBackend,
        "restore_selectors",
        lambda self, snap: captured.setdefault("restored", snap),
    )
    monkeypatch.setattr(APIBackend, "delay_test", lambda self, *a, **kw: {"delay": 1})

    assert try_hot_reload_verge_runtime(paths, verge_dir, force=True) is True
    loaded = yaml.safe_load(captured["payload"]["payload"])
    groups = {g["name"]: g["proxies"] for g in loaded["proxy-groups"]}
    assert "🇬🇧 United Kingdom 03" not in groups["AI-GEMINI"]
    assert "🇬🇧 United Kingdom 03" not in groups["AI-MANUAL"]
    assert groups["AI-GEMINI"][:2] == ["🇰🇷 South Korea 02", "🇨🇳 Taiwan 01"]
    assert captured["restored"] == {"Proxy": "🇨🇳 Taiwan 01"}

    deep = collect_ai_egress_deep_status(paths, groups_map=gmap)
    assert "🇬🇧 United Kingdom 03" not in deep["AI-GEMINI"]["next_candidates"]


def test_verge_daemon_async_rebalance_does_not_block_watch_heartbeats(tmp_path, monkeypatch):
    """验证 Verge 守护进程异步执行耗时 rebalance 时，主循环连接采样与 ai-watch 看护零阻塞持续推进，
    并在后台线程完成后正确回收 rebalance_runs 计数。
    """
    import threading
    import time

    paths = _paths(tmp_path)
    init_verge_config(paths, controller="127.0.0.1:9097", secret="s", mixed_port=7897)

    reb_started = threading.Event()
    reb_release = threading.Event()
    reb_finished = threading.Event()
    watch_calls = 0

    monkeypatch.setattr(
        "cproxy.services.verge.sample_connections_activity",
        lambda p, api, now=None: {},
    )

    def _fast_watch(self, dry_run=False):
        nonlocal watch_calls
        watch_calls += 1
        return ai_watch_mod.WatchReport(
            results=[ai_watch_mod.WatchGroupResult(group="AI-GEMINI", current="TW-01", verdict="ok")]
        )

    def _slow_rebalance(self, dry_run=False, external_only=True):
        reb_started.set()
        reb_release.wait(timeout=2.0)
        rep = rebalance_mod.RebalanceReport(
            ordered=("TW-01",),
            order_path=str(ai_manual_order_file(paths)),
            rerendered=False,
            hot_reloaded=False,
            switched_to=None,
            reason="external-only",
            gemini_ordered=("TW-01",),
        )
        reb_finished.set()
        return rep

    monkeypatch.setattr("cproxy.services.verge.AIWatchService.watch", _fast_watch)
    monkeypatch.setattr("cproxy.services.verge.RebalanceService.rebalance", _slow_rebalance)

    clock = [2000.0]

    def _fake_time():
        return clock[0]

    def _fake_sleep(_sec: float):
        if clock[0] == 2000.0:
            assert reb_started.wait(timeout=1.0)
        elif clock[0] == 2040.0:
            reb_release.set()
            assert reb_finished.wait(timeout=1.0)
            time.sleep(0.03)
        clock[0] += 20.0

    monkeypatch.setattr("cproxy.services.verge.time.time", _fake_time)
    svc = VergeDaemonService(paths)
    svc.run_daemon(
        watch_interval=20.0,
        rebalance_interval=1800.0,
        sample_interval=3.0,
        max_ticks=4,
        enable_keepalive=False,
        async_rebalance=True,
        sleep_fn=_fake_sleep,
    )

    # rebalance 卡住期间 watch 依然每 20s 照常跑满 4 轮，且 rebalance 解锁后计数被回收为 1
    assert watch_calls == 4
    daemon_state = json.loads((paths.state_dir / "verge-daemon.json").read_text(encoding="utf-8"))
    assert daemon_state["watch_runs"] == 4
    assert daemon_state["rebalance_runs"] == 1


def test_probe_paths_context_is_thread_isolated(tmp_path):
    """验证并发线程（后台 rebalance 与主线程 ai-watch）各自持有独立的 ContextVar AppPaths，互不篡改。"""
    import threading

    from cproxy.services.probe import (
        is_dynamic_stream_fragile,
        probe_paths_context,
        record_stream_fragile_node,
    )

    paths_a = _paths(tmp_path / "a")
    paths_b = _paths(tmp_path / "b")
    record_stream_fragile_node(paths_a, "Node-Fragile-A")

    barrier = threading.Barrier(2)
    results: dict[str, bool] = {}

    def _worker_a():
        with probe_paths_context(paths_a):
            barrier.wait(timeout=2.0)
            barrier.wait(timeout=2.0)
            results["a"] = is_dynamic_stream_fragile("Node-Fragile-A")

    def _worker_b():
        with probe_paths_context(paths_b):
            barrier.wait(timeout=2.0)
            results["b"] = is_dynamic_stream_fragile("Node-Fragile-A")
            barrier.wait(timeout=2.0)

    ta = threading.Thread(target=_worker_a)
    tb = threading.Thread(target=_worker_b)
    ta.start()
    tb.start()
    ta.join(timeout=3.0)
    tb.join(timeout=3.0)

    assert results == {"a": True, "b": False}


