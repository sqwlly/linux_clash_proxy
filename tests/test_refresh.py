import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Thread

import pytest
import yaml

from cproxy.backend.models import DelayCheckResult, GroupCheckReport, ProxyGroup


def _write_config(paths, text: str) -> None:
    paths.config_dir.mkdir(parents=True, exist_ok=True)
    (paths.config_dir / "config.yaml").write_text(text.strip() + "\n", encoding="utf-8")


LOCAL_CONFIG = """
mixed-port: 7890
external-controller: 127.0.0.1:9090
program-path: /usr/local/bin/mihomo
secret-file: /home/user/.config/cproxy/secret
proxy-groups:
  - name: SSRDOG
    type: select
    proxies:
      - OLD-NODE
  - name: 🇺🇸 United States
    type: select
    proxies:
      - 🇺🇸 United States丨01
  - name: 🇸🇬 Singapore
    type: select
    proxies:
      - 🇸🇬 Singapore丨01
rules:
  - MATCH,SSRDOG
"""

SUBSCRIPTION_CONFIG = """
mixed-port: 9999
external-controller: 0.0.0.0:9999
allow-lan: true
program-path: /tmp/evil
secret: evil-secret
proxies:
  - name: NEW-US
    type: ss
    server: us.example.com
  - name: NEW-SG
    type: ss
    server: sg.example.com
proxy-groups:
  - name: SSRDOG
    type: select
    proxies:
      - NEW-US
      - NEW-SG
rules:
  - MATCH,SSRDOG
"""


class _SubscriptionHandler(BaseHTTPRequestHandler):
    payload = SUBSCRIPTION_CONFIG

    def do_GET(self):
        body = self.payload.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/yaml")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        return


def _serve(handler_cls):
    server = HTTPServer(("127.0.0.1", 0), handler_cls)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def test_update_source_from_subscription_merges_local_keys(tmp_path: Path):
    from cproxy.config import default_paths, read_config
    from cproxy.services.refresh import update_source_from_subscription
    from cproxy.snapshots import list_snapshots

    paths = default_paths(tmp_path)
    _write_config(paths, LOCAL_CONFIG)
    old_config_text = (paths.config_dir / "config.yaml").read_text(encoding="utf-8")

    server, thread = _serve(_SubscriptionHandler)
    try:
        url = f"http://127.0.0.1:{server.server_port}/sub"
        update_source_from_subscription(paths, url)
    finally:
        server.shutdown()
        thread.join()

    merged = read_config(paths)
    # 订阅内容生效
    assert [p["name"] for p in merged["proxies"]] == ["NEW-US", "NEW-SG"]
    # 本地环境键保留，不被订阅覆盖
    assert merged["mixed-port"] == 7890
    assert merged["external-controller"] == "127.0.0.1:9090"
    assert merged["program-path"] == "/usr/local/bin/mihomo"
    assert merged["secret-file"] == "/home/user/.config/cproxy/secret"
    # 安全相关键即使订阅携带也一律剔除
    assert "allow-lan" not in merged
    assert "secret" not in merged
    assert merged["subscription-url"] == url
    # 覆盖前留下原始配置快照
    config_snapshots = list_snapshots(paths, "config")
    assert len(config_snapshots) == 1
    assert config_snapshots[0].read_text(encoding="utf-8") == old_config_text


def test_update_source_from_subscription_rejects_invalid_content(tmp_path: Path):
    from cproxy.config import default_paths, read_config
    from cproxy.services.refresh import update_source_from_subscription

    class BadHandler(_SubscriptionHandler):
        payload = "this is not a clash config"

    paths = default_paths(tmp_path)
    _write_config(paths, LOCAL_CONFIG)

    server, thread = _serve(BadHandler)
    try:
        url = f"http://127.0.0.1:{server.server_port}/sub"
        with pytest.raises(RuntimeError, match="不是有效的"):
            update_source_from_subscription(paths, url)
    finally:
        server.shutdown()
        thread.join()

    # 原始配置未被破坏
    assert read_config(paths)["program-path"] == "/usr/local/bin/mihomo"


def test_refresh_without_subscription_and_process(tmp_path: Path):
    from cproxy.config import default_paths, runtime_file
    from cproxy.services.refresh import RefreshService

    paths = default_paths(tmp_path)
    _write_config(paths, """
mixed-port: 7890
proxy-groups:
  - name: SSRDOG
    type: select
    proxies:
      - ProxyA
  - name: 🇺🇸 United States
    type: select
    proxies:
      - 🇺🇸 United States丨01
  - name: 🇸🇬 Singapore
    type: select
    proxies:
      - 🇸🇬 Singapore丨01
rules:
  - MATCH,SSRDOG
refresh-groups:
  - SSRDOG
""")

    report = RefreshService(paths).refresh()

    assert report.subscription == "跳过"
    assert report.runtime_path == runtime_file(paths)
    assert report.was_running is False
    assert report.restarted is False
    assert len(report.groups) == 1
    assert report.groups[0].action == "跳过"
    assert report.groups[0].detail == "代理未运行"


def test_refresh_subscription_failure_does_not_block_render(tmp_path: Path):
    from cproxy.config import default_paths, runtime_file
    from cproxy.services.refresh import RefreshService

    paths = default_paths(tmp_path)
    _write_config(paths, """
mixed-port: 7890
subscription-url: http://127.0.0.1:1/unreachable
proxy-groups:
  - name: SSRDOG
    type: select
    proxies:
      - ProxyA
  - name: 🇺🇸 United States
    type: select
    proxies:
      - 🇺🇸 United States丨01
  - name: 🇸🇬 Singapore
    type: select
    proxies:
      - 🇸🇬 Singapore丨01
rules:
  - MATCH,SSRDOG
""")

    report = RefreshService(paths).refresh()

    assert report.subscription == "失败"
    assert report.runtime_path == runtime_file(paths)
    assert runtime_file(paths).is_file()


class _FakeQuery:
    def __init__(self, group: ProxyGroup):
        self.group = group
        self.switched: list[tuple[str, str]] = []

    def get_group(self, name: str, require_api: bool = False) -> ProxyGroup:
        return self.group

    def switch_group(self, group_name: str, target_name: str) -> None:
        self.switched.append((group_name, target_name))


class _FakeDiagnostics:
    def __init__(self, report: GroupCheckReport):
        self.report = report

    def test_group(self, group_name: str) -> GroupCheckReport:
        return self.report


def _build_service(tmp_path: Path, group: ProxyGroup, check: GroupCheckReport):
    from cproxy.config import default_paths
    from cproxy.services.refresh import RefreshService

    paths = default_paths(tmp_path)
    _write_config(paths, "mixed-port: 7890\n")
    service = RefreshService(paths)
    service.query = _FakeQuery(group)
    service.diagnostics = _FakeDiagnostics(check)
    return service


def test_probe_and_switch_moves_dead_current_to_best(tmp_path: Path):
    group = ProxyGroup(name="SSRDOG", type="select", current="DEAD", candidates=["DEAD", "A", "B"])
    check = GroupCheckReport(
        group_name="SSRDOG",
        results=[
            DelayCheckResult(name="DEAD", ok=False, delay=None),
            DelayCheckResult(name="A", ok=True, delay=80),
            DelayCheckResult(name="B", ok=True, delay=50),
        ],
    )
    service = _build_service(tmp_path, group, check)

    result = service._probe_and_switch("SSRDOG")

    assert result.action == "已切换"
    assert result.target == "B"
    assert result.detail == "50ms"
    assert service.query.switched == [("SSRDOG", "B")]


def test_probe_and_switch_keeps_alive_current(tmp_path: Path):
    group = ProxyGroup(name="SSRDOG", type="select", current="A", candidates=["A", "B"])
    check = GroupCheckReport(
        group_name="SSRDOG",
        results=[
            DelayCheckResult(name="A", ok=True, delay=120),
            DelayCheckResult(name="B", ok=True, delay=50),
        ],
    )
    service = _build_service(tmp_path, group, check)

    result = service._probe_and_switch("SSRDOG")

    assert result.action == "保持不变"
    assert service.query.switched == []


def test_probe_and_switch_skips_fallback_group(tmp_path: Path):
    group = ProxyGroup(name="AI-US", type="fallback", current="A", candidates=["A", "B"])
    check = GroupCheckReport(group_name="AI-US", results=[])
    service = _build_service(tmp_path, group, check)

    result = service._probe_and_switch("AI-US")

    assert result.action == "保持不变"
    assert "自动选路" in result.detail
    assert service.query.switched == []


RENDERABLE_CONFIG = """
mixed-port: 7890
proxy-groups:
  - name: SSRDOG
    type: select
    proxies:
      - Auto
      - DIRECT
  - name: Auto
    type: fallback
    proxies:
      - ProxyA
  - name: 🇺🇸 United States
    type: select
    proxies:
      - 🇺🇸 United States丨01
  - name: 🇸🇬 Singapore
    type: select
    proxies:
      - 🇸🇬 Singapore丨01
rules:
  - RULE-SET,ChinaMax,DIRECT
  - MATCH,SSRDOG
"""

RENDERABLE_SUBSCRIPTION = """
proxies:
  - {name: "🇺🇸 United States丨01", type: ss, server: us.example.com}
  - {name: "🇸🇬 Singapore丨01", type: ss, server: sg.example.com}
proxy-groups:
  - {name: SSRDOG, type: select, proxies: [Auto, DIRECT]}
  - {name: Auto, type: fallback, proxies: ["🇺🇸 United States丨01"]}
  - {name: 🇺🇸 United States, type: select, proxies: ["🇺🇸 United States丨01"]}
  - {name: 🇸🇬 Singapore, type: select, proxies: ["🇸🇬 Singapore丨01"]}
rules:
  - RULE-SET,ChinaMax,DIRECT
  - MATCH,SSRDOG
"""


def test_update_source_strips_security_keys_injected_by_subscription(tmp_path: Path):
    from cproxy.config import default_paths, read_config
    from cproxy.services.refresh import update_source_from_subscription

    paths = default_paths(tmp_path)
    # 本地配置缺少这些键时，恶意订阅也不能注入它们
    _write_config(paths, "mode: rule\n")

    class EvilHandler(_SubscriptionHandler):
        payload = """
program-path: /tmp/evil
secret: evil-secret
external-controller: 0.0.0.0:9999
allow-lan: true
bind-address: "*"
mixed-port: 9999
proxies:
  - name: NEW-US
    type: ss
    server: us.example.com
proxy-groups:
  - name: SSRDOG
    type: select
    proxies:
      - NEW-US
rules:
  - MATCH,SSRDOG
"""

    server, thread = _serve(EvilHandler)
    try:
        update_source_from_subscription(paths, f"http://127.0.0.1:{server.server_port}/sub")
    finally:
        server.shutdown()
        thread.join()

    merged = read_config(paths)
    assert "program-path" not in merged
    assert "secret" not in merged
    assert "external-controller" not in merged
    assert "allow-lan" not in merged
    assert "bind-address" not in merged
    # 本地缺失的本地优先键则接受订阅值
    assert merged["mixed-port"] == 9999
    assert [p["name"] for p in merged["proxies"]] == ["NEW-US"]


def test_refresh_redacts_subscription_url_token(tmp_path: Path):
    from cproxy.config import default_paths
    from cproxy.services.refresh import RefreshService

    paths = default_paths(tmp_path)
    _write_config(paths, RENDERABLE_CONFIG)

    class TokenHandler(_SubscriptionHandler):
        payload = RENDERABLE_SUBSCRIPTION

    server, thread = _serve(TokenHandler)
    try:
        url = f"http://127.0.0.1:{server.server_port}/sub?token=SECRET123"
        report = RefreshService(paths).refresh(subscription_url=url)
    finally:
        server.shutdown()
        thread.join()

    assert report.subscription == "已更新"
    assert "SECRET123" not in report.subscription_detail
    assert report.subscription_detail.endswith("?...")


def test_refresh_accepts_scalar_refresh_groups(tmp_path: Path):
    from cproxy.config import default_paths
    from cproxy.services.refresh import RefreshService

    paths = default_paths(tmp_path)
    _write_config(paths, RENDERABLE_CONFIG + "refresh-groups: SSRDOG\n")

    report = RefreshService(paths).refresh()

    assert len(report.groups) == 1
    assert report.groups[0].group == "SSRDOG"
    assert report.groups[0].action == "跳过"


class _FakeAPI:
    def __init__(self, failures_before_ready: int = 0):
        self.failures_before_ready = failures_before_ready
        self.calls = 0

    def get_groups(self):
        from cproxy.backend.api import APIUnavailableError

        self.calls += 1
        if self.calls <= self.failures_before_ready:
            raise APIUnavailableError("connection refused")
        return {}


class _FakeRoutingQuery:
    def __init__(self, groups: dict, api):
        self.groups = groups
        self.api = api
        self.switched: list[tuple[str, str]] = []

    def get_group(self, name: str, require_api: bool = False) -> ProxyGroup:
        group = self.groups.get(name)
        if group is None:
            raise SystemExit(f"错误: 未找到代理组: {name}")
        return group

    def switch_group(self, group_name: str, target_name: str) -> None:
        self.switched.append((group_name, target_name))


class _FakeRunningProcess:
    def __init__(self):
        self.restart_calls = 0

    def is_running(self) -> bool:
        return True

    def restart(self) -> int:
        self.restart_calls += 1
        return 4321


def test_refresh_waits_for_api_and_continues_after_unknown_group(tmp_path: Path):
    from cproxy.config import default_paths
    from cproxy.services.refresh import RefreshService

    paths = default_paths(tmp_path)
    _write_config(paths, RENDERABLE_CONFIG)

    api = _FakeAPI(failures_before_ready=2)
    group = ProxyGroup(name="SSRDOG", type="select", current="A", candidates=["A", "B"])
    query = _FakeRoutingQuery({"SSRDOG": group}, api)
    check = GroupCheckReport(
        group_name="SSRDOG",
        results=[
            DelayCheckResult(name="A", ok=True, delay=120),
            DelayCheckResult(name="B", ok=True, delay=50),
        ],
    )
    service = RefreshService(paths)
    service.process = _FakeRunningProcess()
    service.query = query
    service.diagnostics = _FakeDiagnostics(check)

    report = service.refresh(groups=["NOPE", "SSRDOG"])

    assert service.process.restart_calls == 1
    assert api.calls >= 3  # 前两次未就绪，之后就绪
    assert report.groups[0].group == "NOPE"
    assert report.groups[0].action == "失败"
    assert "未找到代理组" in report.groups[0].detail
    assert report.groups[1].action == "保持不变"
    assert query.switched == []


def test_refresh_raises_when_api_never_ready(tmp_path: Path, monkeypatch):
    import cproxy.services.refresh as refresh_module
    from cproxy.config import default_paths
    from cproxy.services.refresh import RefreshService

    paths = default_paths(tmp_path)
    _write_config(paths, RENDERABLE_CONFIG)

    monkeypatch.setattr(refresh_module, "API_READY_TIMEOUT", 0.3)
    monkeypatch.setattr(refresh_module, "API_READY_INTERVAL", 0.05)

    service = RefreshService(paths)
    service.process = _FakeRunningProcess()
    service.query = _FakeRoutingQuery({}, _FakeAPI(failures_before_ready=10**9))
    service.diagnostics = _FakeDiagnostics(GroupCheckReport(group_name="SSRDOG", results=[]))

    with pytest.raises(RuntimeError, match="未就绪"):
        service.refresh(groups=["SSRDOG"])
