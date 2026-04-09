import json
import os
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Thread
from urllib.parse import unquote, urlparse


class _ApiHandler(BaseHTTPRequestHandler):
    payload = {
        "proxies": {
            "AI-AUTO": {
                "type": "Fallback",
                "now": "AI-SG",
                "alive": True,
                "all": ["AI-US", "AI-SG"],
                "history": [],
            },
            "AI-US": {
                "type": "Fallback",
                "now": "🇺🇸 United States丨01",
                "alive": True,
                "all": ["🇺🇸 United States丨01"],
                "history": [{"delay": 96}],
            },
            "AI-SG": {
                "type": "Fallback",
                "now": "🇸🇬 Singapore丨01",
                "alive": True,
                "all": ["🇸🇬 Singapore丨01"],
                "history": [{"delay": 89}],
            },
        }
    }

    def do_GET(self):
        if self.path == "/version":
            self._send_json({"version": "test"})
            return
        if self.path == "/proxies":
            self._send_json(self.payload)
            return
        if self.path.startswith("/proxies/") and self.path.endswith("/delay?url=http%3A%2F%2F127.0.0.1%2F204&timeout=5000"):
            target = unquote(self.path.split("/")[2])
            delay = 96 if target == "AI-US" else 89
            self._send_json({"delay": delay})
            return
        self.send_response(404)
        self.end_headers()

    def _send_json(self, data):
        body = json.dumps(data).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        return


class _ProxyHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/google":
            self._send_text("ok")
            return
        if parsed.path == "/github":
            self._send_text("ok")
            return
        if parsed.path == "/ip":
            self._send_text("203.0.113.7\n")
            return
        self.send_response(404)
        self.end_headers()

    def _send_text(self, text):
        body = text.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        return


def _run(env, *args):
    return subprocess.run(
        [sys.executable, "-m", "cproxy.cli", *args],
        capture_output=True,
        text=True,
        cwd="/root/clash_proxy",
        env=env,
    )


def _start_server(handler):
    server = HTTPServer(("127.0.0.1", 0), handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def test_diagnostics_and_proxy_commands(tmp_path: Path):
    api_server, api_thread = _start_server(_ApiHandler)
    proxy_server, proxy_thread = _start_server(_ProxyHandler)

    try:
        config_dir = tmp_path / ".config" / "cproxy"
        data_dir = tmp_path / ".local" / "share" / "cproxy"
        state_dir = tmp_path / ".local" / "state" / "cproxy"
        config_dir.mkdir(parents=True)
        data_dir.mkdir(parents=True)
        state_dir.mkdir(parents=True)
        (data_dir / "country.mmdb").write_bytes(b"test")

        runtime_path = data_dir / "runtime.yaml"
        runtime_path.write_text("port: 7890\n", encoding="utf-8")
        owned_proc = tmp_path / "owned-proc.sh"
        owned_proc.write_text(
            """#!/bin/bash
trap 'exit 0' TERM INT
while true; do
  sleep 1
done
""",
            encoding="utf-8",
        )
        owned_proc.chmod(0o755)
        managed_process = subprocess.Popen([str(owned_proc), str(runtime_path)])
        (state_dir / "cproxy.pid").write_text(f"{managed_process.pid}\n", encoding="utf-8")
        (state_dir / "cproxy-process.json").write_text(
            json.dumps({"pid": managed_process.pid, "program": str(owned_proc), "runtime": str(runtime_path)}) + "\n",
            encoding="utf-8",
        )

        (config_dir / "config.yaml").write_text(
            f"""
external-controller: 127.0.0.1:{api_server.server_port}
mixed-port: {proxy_server.server_port}
test-url: http://127.0.0.1/204
test-timeout: 5000
connectivity-test-urls:
  - http://probe.local/google
  - http://probe.local/github
ip-check-urls:
  - http://probe.local/ip
            """.strip()
            + "\n",
            encoding="utf-8",
        )

        env = os.environ.copy()
        env["PYTHONPATH"] = "/root/clash_proxy/src"
        env["HOME"] = str(tmp_path)
        env["SHELL"] = "/bin/sh"

        test_group_result = _run(env, "test-group", "AI-AUTO")
        assert test_group_result.returncode == 0
        assert "检查摘要" in test_group_result.stdout
        assert "目标组: AI-AUTO" in test_group_result.stdout
        assert "最佳: AI-SG (89ms)" in test_group_result.stdout
        assert "正常  AI-US  96ms" in test_group_result.stdout

        test_group_raw = _run(env, "test-group", "AI-AUTO", "--raw")
        assert test_group_raw.returncode == 0
        assert "AI-US: 96ms" in test_group_raw.stdout
        assert "AI-SG: 89ms" in test_group_raw.stdout

        proxy_env_result = _run(env, "proxy-env")
        assert proxy_env_result.returncode == 0
        assert "HTTP_PROXY=http://127.0.0.1:" in proxy_env_result.stdout
        assert "ALL_PROXY=socks5h://127.0.0.1:" in proxy_env_result.stdout
        assert "NO_PROXY=127.0.0.1,localhost" in proxy_env_result.stdout

        with_proxy_result = _run(env, "with-proxy", "env")
        assert with_proxy_result.returncode == 0
        assert "HTTP_PROXY=http://127.0.0.1:" in with_proxy_result.stdout
        assert "ALL_PROXY=socks5h://127.0.0.1:" in with_proxy_result.stdout

        proxy_shell_result = _run(env, "proxy-shell", "--", "-c", "env")
        assert proxy_shell_result.returncode == 0
        assert "HTTP_PROXY=http://127.0.0.1:" in proxy_shell_result.stdout
        assert "进入临时代理 shell" in proxy_shell_result.stdout

        test_result = _run(env, "test")
        assert test_result.returncode == 0
        assert "检查摘要" in test_result.stdout
        assert "目标: 代理连通性" in test_result.stdout
        assert "可用: 4/4" in test_result.stdout
        assert "出口 IP: 203.0.113.7" in test_result.stdout
        assert "正常  GeoIP 数据" in test_result.stdout
    finally:
        if "managed_process" in locals():
            managed_process.terminate()
            managed_process.wait(timeout=5)
        api_server.shutdown()
        api_thread.join()
        proxy_server.shutdown()
        proxy_thread.join()


def test_diagnostics_report_missing_geodata(tmp_path: Path):
    api_server, api_thread = _start_server(_ApiHandler)
    proxy_server, proxy_thread = _start_server(_ProxyHandler)

    try:
        config_dir = tmp_path / ".config" / "cproxy"
        data_dir = tmp_path / ".local" / "share" / "cproxy"
        state_dir = tmp_path / ".local" / "state" / "cproxy"
        config_dir.mkdir(parents=True)
        data_dir.mkdir(parents=True)
        state_dir.mkdir(parents=True)

        runtime_path = data_dir / "runtime.yaml"
        runtime_path.write_text("port: 7890\n", encoding="utf-8")
        owned_proc = tmp_path / "owned-proc.sh"
        owned_proc.write_text(
            """#!/bin/bash
trap 'exit 0' TERM INT
while true; do
  sleep 1
done
""",
            encoding="utf-8",
        )
        owned_proc.chmod(0o755)
        managed_process = subprocess.Popen([str(owned_proc), str(runtime_path)])
        (state_dir / "cproxy.pid").write_text(f"{managed_process.pid}\n", encoding="utf-8")
        (state_dir / "cproxy-process.json").write_text(
            json.dumps({"pid": managed_process.pid, "program": str(owned_proc), "runtime": str(runtime_path)}) + "\n",
            encoding="utf-8",
        )

        (config_dir / "config.yaml").write_text(
            f"""
external-controller: 127.0.0.1:{api_server.server_port}
mixed-port: {proxy_server.server_port}
connectivity-test-urls:
  - http://probe.local/google
ip-check-urls:
  - http://probe.local/ip
            """.strip()
            + "\n",
            encoding="utf-8",
        )

        env = os.environ.copy()
        env["PYTHONPATH"] = "/root/clash_proxy/src"
        env["HOME"] = str(tmp_path)

        test_result = _run(env, "test")
        assert test_result.returncode == 1
        assert "失败  GeoIP 数据" in test_result.stdout
        assert "country.mmdb" in test_result.stdout
        assert str(data_dir / "country.mmdb") in test_result.stdout
    finally:
        if "managed_process" in locals():
            managed_process.terminate()
            managed_process.wait(timeout=5)
        api_server.shutdown()
        api_thread.join()
        proxy_server.shutdown()
        proxy_thread.join()
