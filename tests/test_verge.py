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



