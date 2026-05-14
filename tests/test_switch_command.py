import json
import os
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Thread
from urllib.parse import unquote


def test_switch_updates_remote_selection(tmp_path: Path):
    state = {
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
        "AI-US": {
            "type": "Fallback",
            "now": "🇺🇸 United States丨01",
            "alive": True,
            "all": ["🇺🇸 United States丨01"],
            "history": [{"delay": 95}],
        },
        "AI-SG": {
            "type": "Fallback",
            "now": "🇸🇬 Singapore丨01",
            "alive": True,
            "all": ["🇸🇬 Singapore丨01"],
            "history": [{"delay": 99}],
        },
    }

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/proxies":
                self._send({"proxies": state})
                return
            if self.path == "/version":
                self._send({"version": "test"})
                return
            self.send_response(404)
            self.end_headers()

        def do_PUT(self):
            if not self.path.startswith("/proxies/"):
                self.send_response(404)
                self.end_headers()
                return
            group_name = unquote(self.path[len("/proxies/"):])
            payload = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
            state[group_name]["now"] = payload["name"]
            self._send({"ok": True})

        def _send(self, data):
            body = json.dumps(data).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):
            return

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        config_dir = tmp_path / ".config" / "cproxy"
        config_dir.mkdir(parents=True)
        (config_dir / "config.yaml").write_text(
            f"external-controller: 127.0.0.1:{server.server_port}\n"
            "mixed-port: 7890\n"
            "output-icons: true\n",
            encoding="utf-8",
        )

        env = os.environ.copy()
        env["PYTHONPATH"] = "/root/clash_proxy/src"
        env["HOME"] = str(tmp_path)
        env.pop("NO_COLOR", None)

        switch_result = subprocess.run(
            [sys.executable, "-m", "cproxy.cli", "switch", "AI-MANUAL", "AI-SG"],
            capture_output=True,
            text=True,
            cwd="/root/clash_proxy",
            env=env,
        )

        assert switch_result.returncode == 0
        assert "结果" in switch_result.stdout
        assert "代理组: AI-MANUAL" in switch_result.stdout
        assert "当前选择:" in switch_result.stdout
        assert "AI-SG" in switch_result.stdout
        assert "\x1b[" in switch_result.stdout

        color_env = env.copy()
        color_env["FORCE_COLOR"] = "1"
        color_switch_result = subprocess.run(
            [sys.executable, "-m", "cproxy.cli", "switch", "AI-MANUAL", "AI-US"],
            capture_output=True,
            text=True,
            cwd="/root/clash_proxy",
            env=color_env,
        )
        assert color_switch_result.returncode == 0
        assert "\x1b[" in color_switch_result.stdout

        no_color_env = color_env.copy()
        no_color_env["CPROXY_COLOR"] = "never"
        no_color_switch_result = subprocess.run(
            [sys.executable, "-m", "cproxy.cli", "switch", "AI-MANUAL", "AI-SG"],
            capture_output=True,
            text=True,
            cwd="/root/clash_proxy",
            env=no_color_env,
        )
        assert no_color_switch_result.returncode == 0
        assert "\x1b[" not in no_color_switch_result.stdout

        current_result = subprocess.run(
            [sys.executable, "-m", "cproxy.cli", "current", "AI-MANUAL"],
            capture_output=True,
            text=True,
            cwd="/root/clash_proxy",
            env=env,
        )

        assert current_result.returncode == 0
        assert "摘要" in current_result.stdout
        assert "当前选择:" in current_result.stdout
        assert "AI-SG" in current_result.stdout

        status_env = env.copy()
        status_env["CPROXY_COLOR"] = "never"
        status_result = subprocess.run(
            [sys.executable, "-m", "cproxy.cli", "status"],
            capture_output=True,
            text=True,
            cwd="/root/clash_proxy",
            env=status_env,
        )
        assert status_result.returncode == 0
        assert "状态: ○ 未运行" in status_result.stdout
        assert "API: ✓ 可访问" in status_result.stdout
        assert "API 可能来自其它 Mihomo 实例" in status_result.stdout
        assert "clash-proxy status" in status_result.stdout
        assert "cproxy render" in status_result.stdout

        no_icons_env = status_env.copy()
        no_icons_env["CPROXY_ICONS"] = "0"
        no_icons_status_result = subprocess.run(
            [sys.executable, "-m", "cproxy.cli", "status"],
            capture_output=True,
            text=True,
            cwd="/root/clash_proxy",
            env=no_icons_env,
        )
        assert no_icons_status_result.returncode == 0
        assert "状态: 未运行" in no_icons_status_result.stdout
        assert "○ 未运行" not in no_icons_status_result.stdout

        raw_status_result = subprocess.run(
            [sys.executable, "-m", "cproxy.cli", "status", "--raw"],
            capture_output=True,
            text=True,
            cwd="/root/clash_proxy",
            env=env,
        )
        assert raw_status_result.returncode == 0
        assert "○ 未运行" not in raw_status_result.stdout
        assert "✓ 可访问" not in raw_status_result.stdout
        assert "API 可能来自其它 Mihomo 实例" not in raw_status_result.stdout
    finally:
        server.shutdown()
        thread.join()
