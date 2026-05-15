import copy
import json
import os
import subprocess
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Thread
from urllib.parse import unquote, urlsplit


class _ProbeHandler(BaseHTTPRequestHandler):
    payload = {}
    delays = {}
    puts = []

    def do_GET(self):
        parsed = urlsplit(self.path)
        if parsed.path == "/version":
            self._send({"version": "test"})
            return

        if parsed.path == "/proxies":
            self._send(self.payload)
            return

        if parsed.path.startswith("/proxies/") and parsed.path.endswith("/delay"):
            target = parsed.path[len("/proxies/") : -len("/delay")]
            target = unquote(target)
            if target in self.delays:
                self._send({"delay": self.delays[target]})
                return

        self.send_response(404)
        self.end_headers()

    def do_PUT(self):
        parsed = urlsplit(self.path)
        if parsed.path.startswith("/proxies/"):
            group = unquote(parsed.path[len("/proxies/") :])
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length).decode("utf-8")
            target = json.loads(body).get("name")
            self.puts.append((group, target))
            group_state = self.payload["proxies"].get(group)
            if isinstance(group_state, dict):
                group_state["now"] = target
            self._send({})
            return

        self.send_response(404)
        self.end_headers()

    def _send(self, data):
        body = json.dumps(data).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        return


def test_probe_stable_node_defaults_to_all_ai_leaf_nodes_and_switches_path(tmp_path: Path):
    payload = {
        "proxies": {
            "AI-MANUAL": {
                "type": "Selector",
                "now": "🇯🇵 Japan",
                "all": ["AI-AUTO", "AI-US", "AI-SG", "🇯🇵 Japan", "🇺🇸 United States", "🇸🇬 Singapore"],
            },
            "AI-AUTO": {
                "type": "Fallback",
                "now": "AI-SG",
                "all": ["AI-US", "AI-SG"],
            },
            "AI-US": {
                "type": "Fallback",
                "now": "🇺🇸 United States丨01",
                "all": ["🇺🇸 United States丨01", "🇺🇸 United States丨02"],
            },
            "AI-SG": {
                "type": "Fallback",
                "now": "🇸🇬 Singapore丨01",
                "all": ["🇸🇬 Singapore丨01"],
            },
            "🇯🇵 Japan": {
                "type": "Selector",
                "now": "🇯🇵 Japan丨01",
                "all": ["🇯🇵 Japan丨01"],
            },
            "🇺🇸 United States": {
                "type": "Selector",
                "now": "🇺🇸 United States丨01",
                "all": ["🇺🇸 United States丨01", "🇺🇸 United States丨02"],
            },
            "🇸🇬 Singapore": {
                "type": "Selector",
                "now": "🇸🇬 Singapore丨01",
                "all": ["🇸🇬 Singapore丨01"],
            },
        }
    }

    _ProbeHandler.payload = copy.deepcopy(payload)
    _ProbeHandler.delays = {
        "🇺🇸 United States丨01": 300,
        "🇺🇸 United States丨02": 80,
        "🇸🇬 Singapore丨01": 120,
        "🇯🇵 Japan丨01": 200,
    }
    _ProbeHandler.puts = []

    server = HTTPServer(("127.0.0.1", 0), _ProbeHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        config_file = tmp_path / "runtime.yaml"
        config_file.write_text(
            f"external-controller: 127.0.0.1:{server.server_port}\n"
            "mixed-port: 7890\n",
            encoding="utf-8",
        )

        env = os.environ.copy()
        env["RUNTIME_CONFIG_FILE"] = str(config_file)
        env["SOURCE_CONFIG_FILE"] = str(config_file)
        env["HOME"] = str(tmp_path)

        script = Path(__file__).resolve().parents[1] / "proxy.sh"
        result = subprocess.run(
            [
                str(script),
                "probe-stable-node",
                "--profile",
                "chatgpt",
                "--strategy",
                "aggressive",
                "--rounds",
                "3",
                "--timeout",
                "1000",
                "--url",
                "http://probe.local/ping",
                "--raw",
                "--switch",
            ],
            capture_output=True,
            text=True,
            cwd=script.parent,
            env=env,
        )

        assert result.returncode == 0, result.stderr
        assert "GROUP\tAI-MANUAL" in result.stdout
        assert "CURRENT\t🇯🇵 Japan丨01" in result.stdout
        assert "BEST\t🇺🇸 United States丨02" in result.stdout
        assert "NODE\t🇺🇸 United States丨01\tsuccess=1/1" in result.stdout
        assert "NODE\t🇺🇸 United States丨02" in result.stdout
        assert "NODE\t🇸🇬 Singapore丨01" in result.stdout
        assert "NODE\t🇯🇵 Japan丨01" in result.stdout
        assert "NODE\tAI-AUTO" not in result.stdout
        assert "NODE\tAI-US" not in result.stdout
        assert "NODE\tAI-SG" not in result.stdout
        assert _ProbeHandler.puts == [
            ("AI-US", "🇺🇸 United States丨02"),
            ("AI-AUTO", "AI-US"),
            ("AI-MANUAL", "AI-AUTO"),
        ]
    finally:
        server.shutdown()
        thread.join()
