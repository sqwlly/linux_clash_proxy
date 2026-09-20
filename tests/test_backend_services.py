import json
import os
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Thread

import yaml

ROOT_DIR = Path(__file__).resolve().parents[1]
SRC_DIR = ROOT_DIR / "src"

sys.path.insert(0, str(SRC_DIR))


class _Handler(BaseHTTPRequestHandler):
    payload = {
        "proxies": {
            "AI-MANUAL": {
                "type": "Selector",
                "now": "AI-AUTO",
                "alive": True,
                "all": ["AI-AUTO", "AI-US", "AI-SG"],
                "history": [],
            },
            "AI-AUTO": {
                "type": "Fallback",
                "now": "AI-US",
                "alive": True,
                "all": ["AI-US", "AI-SG"],
                "history": [],
            },
        }
    }

    def do_GET(self):
        if self.path == "/proxies":
            body = json.dumps(self.payload).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(404)
        self.end_headers()

    def log_message(self, format, *args):
        return


def test_query_service_uses_api_models(tmp_path: Path):
    from cproxy.backend.models import ProxyGroup
    from cproxy.config import default_paths
    from cproxy.services.query import QueryService

    server = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        config_dir = tmp_path / ".config" / "cproxy"
        config_dir.mkdir(parents=True)
        (config_dir / "config.yaml").write_text(
            f"external-controller: 127.0.0.1:{server.server_port}\n"
            "mixed-port: 7890\n",
            encoding="utf-8",
        )

        paths = default_paths(tmp_path)
        service = QueryService(paths)
        group = service.get_group("AI-MANUAL")

        assert isinstance(group, ProxyGroup)
        assert group.name == "AI-MANUAL"
        assert group.current == "AI-AUTO"
        assert group.candidates == ["AI-AUTO", "AI-US", "AI-SG"]
        assert group.source == "api"
    finally:
        server.shutdown()
        thread.join()


def test_query_service_falls_back_to_runtime_models(tmp_path: Path):
    from cproxy.backend.models import ProxyGroup
    from cproxy.config import default_paths
    from cproxy.services.query import QueryService

    config_dir = tmp_path / ".config" / "cproxy"
    data_dir = tmp_path / ".local" / "share" / "cproxy"
    config_dir.mkdir(parents=True)
    data_dir.mkdir(parents=True)
    (config_dir / "config.yaml").write_text(
        "external-controller: 127.0.0.1:9\n"
        "mixed-port: 7890\n",
        encoding="utf-8",
    )
    runtime = {
        "proxy-groups": [
            {"name": "AI-MANUAL", "type": "select", "proxies": ["AI-AUTO", "AI-SG"]},
            {"name": "AI-AUTO", "type": "fallback", "proxies": ["AI-US", "AI-SG"]},
        ]
    }
    (data_dir / "runtime.yaml").write_text(yaml.safe_dump(runtime, allow_unicode=True, sort_keys=False), encoding="utf-8")

    paths = default_paths(tmp_path)
    service = QueryService(paths)
    group = service.get_group("AI-MANUAL")

    assert isinstance(group, ProxyGroup)
    assert group.name == "AI-MANUAL"
    assert group.current == "AI-AUTO"
    assert group.candidates == ["AI-AUTO", "AI-SG"]
    assert group.source == "runtime"


def test_query_service_maps_connections_and_providers(tmp_path: Path):
    from cproxy.config import default_paths
    from cproxy.services.query import QueryService

    class FakeAPI:
        def get_connections(self):
            return {
                "connections": [
                    {
                        "id": "abc",
                        "metadata": {"host": "example.test", "process": "curl"},
                        "rule": "DOMAIN-SUFFIX",
                        "chains": ["Proxy", "Node A"],
                        "upload": 1024,
                        "download": "2048",
                    }
                ]
            }

        def get_proxy_providers(self):
            return {
                "providers": {
                    "corp": {
                        "type": "HTTP",
                        "vehicleType": "HTTP",
                        "updatedAt": "2026-05-31T12:00:00Z",
                        "proxies": [{"name": "Node A"}, {"name": "Node B"}],
                    }
                }
            }

    service = QueryService(default_paths(tmp_path))
    service.api = FakeAPI()

    connections = service.list_connections()
    providers = service.list_proxy_providers()

    assert len(connections) == 1
    assert connections[0].id == "abc"
    assert connections[0].host == "example.test"
    assert connections[0].proxy_chain == ["Proxy", "Node A"]
    assert connections[0].upload == 1024
    assert connections[0].download == 2048

    assert len(providers) == 1
    assert providers[0].name == "corp"
    assert providers[0].proxy_count == 2


def test_query_service_writes_audit_for_mutations(tmp_path: Path):
    from cproxy.audit import audit_log_file
    from cproxy.backend.models import ProxyGroup
    from cproxy.config import default_paths
    from cproxy.services.query import QueryService

    class FakeAPI:
        def __init__(self):
            self.calls = []

        def get_groups(self):
            return {
                "AI-MANUAL": ProxyGroup(
                    name="AI-MANUAL",
                    type="select",
                    current="Node A",
                    candidates=["Node A", "Node B"],
                )
            }

        def get_connections(self):
            return {"connections": []}

        def switch_group(self, group_name, target_name):
            self.calls.append(("switch", group_name, target_name))

        def close_connection(self, connection_id):
            self.calls.append(("close", connection_id))

        def close_all_connections(self):
            self.calls.append(("close_all",))

        def update_proxy_provider(self, name):
            self.calls.append(("update_provider", name))

    paths = default_paths(tmp_path)
    service = QueryService(paths)
    fake_api = FakeAPI()
    service.api = fake_api

    service.switch_group("AI-MANUAL", "Node B")
    service.close_connection("conn-1")
    service.close_all_connections()
    service.update_proxy_provider("corp")

    audit_lines = audit_log_file(paths).read_text(encoding="utf-8").splitlines()
    events = [json.loads(line) for line in audit_lines]

    assert [event["action"] for event in events] == [
        "switch_group",
        "close_connection",
        "close_all_connections",
        "update_proxy_provider",
    ]
    assert events[0]["detail"] == {"selected": "Node B", "dropped": 0}


def test_query_service_writes_audit_for_failed_mutation(tmp_path: Path):
    from cproxy.audit import audit_log_file
    from cproxy.backend.models import ProxyGroup
    from cproxy.config import default_paths
    from cproxy.services.query import QueryService

    class FakeAPI:
        def get_groups(self):
            return {
                "AI-MANUAL": ProxyGroup(
                    name="AI-MANUAL",
                    type="select",
                    current="Node A",
                    candidates=["Node A", "Node B"],
                )
            }

        def switch_group(self, group_name, target_name):
            raise RuntimeError("boom")

    paths = default_paths(tmp_path)
    service = QueryService(paths)
    service.api = FakeAPI()

    try:
        service.switch_group("AI-MANUAL", "Node B")
    except RuntimeError:
        pass
    else:
        raise AssertionError("expected RuntimeError")

    event = json.loads(audit_log_file(paths).read_text(encoding="utf-8"))
    assert event["action"] == "switch_group"
    assert event["result"] == "error"
    assert event["detail"]["selected"] == "Node B"


def test_switch_drops_agy_and_ai_manual_connections(tmp_path: Path):
    from cproxy.backend.models import ProxyGroup
    from cproxy.config import default_paths
    from cproxy.services.query import QueryService

    class FakeAPI:
        def __init__(self):
            self.closed: list[str] = []

        def get_groups(self):
            return {
                "AI-MANUAL": ProxyGroup(
                    name="AI-MANUAL",
                    type="select",
                    current="Node A",
                    candidates=["Node A", "Node B"],
                ),
                "CyberGuard": ProxyGroup(
                    name="CyberGuard",
                    type="select",
                    current="JP",
                    candidates=["JP", "US"],
                ),
            }

        def get_connections(self):
            return {
                "connections": [
                    {
                        "id": "ai-1",
                        "chains": ["🇺🇸美国 01 | 1X", "AI-MANUAL"],
                        "metadata": {"process": "agy", "host": "daily-cloudcode-pa.googleapis.com"},
                    },
                    {
                        "id": "agy-leak",
                        "chains": ["🇯🇵日本 02", "CyberGuard"],
                        "metadata": {"process": "agy", "host": "github.com"},
                    },
                    {
                        "id": "cursor",
                        "chains": ["🇯🇵日本 02", "CyberGuard"],
                        "metadata": {"process": "node", "host": "api2.cursor.sh"},
                    },
                ]
            }

        def switch_group(self, group_name, target_name):
            return None

        def close_connection(self, connection_id):
            self.closed.append(connection_id)

    paths = default_paths(tmp_path)
    service = QueryService(paths)
    fake = FakeAPI()
    service.api = fake
    service.switch_group("AI-MANUAL", "Node B")
    assert fake.closed == ["ai-1", "agy-leak"]
    assert service.last_switch_from == "Node A"

    fake.closed.clear()
    service.switch_group("CyberGuard", "US")
    assert fake.closed == []


def test_node_delays_uses_groups_snapshot(tmp_path: Path):
    """同一次会话已有 /proxies 快照时，延迟不再打第二遍 API。"""
    from cproxy.backend.models import ProxyGroup
    from cproxy.config import default_paths
    from cproxy.services.query import QueryService

    class FakeAPI:
        def get_delays(self):
            raise AssertionError("should use snapshot")

    paths = default_paths(tmp_path)
    service = QueryService(paths)
    service.api = FakeAPI()
    service.groups_snapshot = {
        "n": ProxyGroup(name="n", type="ss", current="-", candidates=[], delay=42),
    }
    assert service.node_delays() == {"n": 42}


def test_reload_config_restores_selectors(tmp_path: Path, monkeypatch):
    from cproxy.backend.api import APIBackend
    from cproxy.config import default_paths

    config_dir = tmp_path / ".config" / "cproxy"
    config_dir.mkdir(parents=True)
    (config_dir / "config.yaml").write_text(
        "external-controller: 127.0.0.1:9090\nmixed-port: 7890\n",
        encoding="utf-8",
    )
    paths = default_paths(tmp_path)
    api = APIBackend(paths)
    state = {"now": "🇺🇸 United States"}
    puts: list[tuple[str, dict | None]] = []

    def request(method, path, payload=None, request_timeout=None):
        if method == "GET" and path == "/proxies":
            return {
                "proxies": {
                    "AI-MANUAL": {
                        "type": "Selector",
                        "now": state["now"],
                        "all": ["🇺🇸 United States", "🇯🇵 Japan"],
                        "history": [],
                    }
                }
            }
        if method == "PUT" and path.startswith("/configs"):
            puts.append((path, payload))
            state["now"] = "🇯🇵 Japan"
            return {}
        if method == "PUT" and path.startswith("/proxies/"):
            state["now"] = payload["name"]
            return {}
        if method == "GET" and path == "/connections":
            return {"connections": []}
        return {}

    monkeypatch.setattr(api, "request", request)
    api.reload_config("/tmp/runtime.yaml")
    assert puts == [("/configs?force=true", {"path": "/tmp/runtime.yaml"})]
    assert state["now"] == "🇺🇸 United States"
