import json
import os
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Thread

import yaml

sys.path.insert(0, "/root/clash_proxy/src")


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
