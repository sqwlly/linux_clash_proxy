import json
import os
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Thread

import yaml


class _Handler(BaseHTTPRequestHandler):
    payload = {
        "proxies": {
            "AI-MANUAL": {
                "type": "Selector",
                "now": "AI-AUTO",
                "alive": True,
                "all": ["AI-AUTO", "AI-US", "AI-SG", "🇺🇸 United States", "🇸🇬 Singapore"],
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
            "🇺🇸 United States": {
                "type": "Selector",
                "now": "🇺🇸 United States丨01",
                "alive": True,
                "all": ["🇺🇸 United States丨01"],
                "history": [{"delay": 96}],
            },
            "🇸🇬 Singapore": {
                "type": "Selector",
                "now": "🇸🇬 Singapore丨01",
                "alive": True,
                "all": ["🇸🇬 Singapore丨01"],
                "history": [{"delay": 97}],
            },
        }
    }

    def do_GET(self):
        if self.path == "/version":
            self._send({"version": "test"})
            return
        if self.path == "/proxies":
            self._send(self.payload)
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


class _ProxyHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "http://probe.local/chatgpt":
            self._send(200, "ok")
            return
        if self.path == "http://probe.local/openai-api":
            self._send(502, "bad gateway")
            return
        self.send_response(404)
        self.end_headers()

    def _send(self, status: int, body_text: str):
        body = body_text.encode("utf-8")
        self.send_response(status)
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


def test_query_commands_use_api_output(tmp_path: Path):
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    proxy_server = HTTPServer(("127.0.0.1", 0), _ProxyHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    proxy_thread = Thread(target=proxy_server.serve_forever, daemon=True)
    thread.start()
    proxy_thread.start()

    try:
        config_dir = tmp_path / ".config" / "cproxy"
        config_dir.mkdir(parents=True)
        (config_dir / "config.yaml").write_text(
            f"external-controller: 127.0.0.1:{server.server_port}\n"
            f"mixed-port: {proxy_server.server_port}\n"
            "ai-chatgpt-url: http://probe.local/chatgpt\n"
            "ai-openai-api-url: http://probe.local/openai-api\n",
            encoding="utf-8",
        )

        env = os.environ.copy()
        env["PYTHONPATH"] = "/root/clash_proxy/src"
        env["HOME"] = str(tmp_path)

        current_result = _run(env, "current", "AI-MANUAL")
        assert current_result.returncode == 0
        assert "当前选择: AI-AUTO" in current_result.stdout

        current_raw = _run(env, "current", "AI-MANUAL", "--raw")
        assert current_raw.returncode == 0
        assert current_raw.stdout.strip() == "AI-AUTO"

        groups_result = _run(env, "list-groups")
        assert groups_result.returncode == 0
        assert "摘要" in groups_result.stdout
        assert "列表" in groups_result.stdout
        assert "当前选择" in groups_result.stdout
        assert "AI-MANUAL" in groups_result.stdout

        nodes_result = _run(env, "list-nodes", "AI-MANUAL")
        assert nodes_result.returncode == 0
        assert "摘要" in nodes_result.stdout
        assert "列表" in nodes_result.stdout
        assert "当前选择: AI-AUTO" in nodes_result.stdout

        ai_status_result = _run(env, "ai-status")
        assert ai_status_result.returncode == 0
        assert "摘要" in ai_status_result.stdout
        assert "AI 路由:" in ai_status_result.stdout
        assert "AI 探测: 部分异常" in ai_status_result.stdout
        assert "连通性" in ai_status_result.stdout
        assert "正常  ChatGPT Web  http://probe.local/chatgpt" in ai_status_result.stdout
        assert "失败  OpenAI API  http://probe.local/openai-api" in ai_status_result.stdout
        assert "链路" in ai_status_result.stdout

        ai_status_raw = _run(env, "ai-status", "--raw")
        assert ai_status_raw.returncode == 0
        assert "AI-MANUAL: type=Selector now=AI-AUTO" in ai_status_raw.stdout
    finally:
        server.shutdown()
        thread.join()
        proxy_server.shutdown()
        proxy_thread.join()


def test_query_commands_fall_back_to_runtime_when_api_unavailable(tmp_path: Path):
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
            {
                "name": "AI-MANUAL",
                "type": "select",
                "proxies": ["AI-AUTO", "AI-US", "AI-SG"],
            },
            {
                "name": "AI-AUTO",
                "type": "fallback",
                "proxies": ["AI-US", "AI-SG"],
            },
            {
                "name": "AI-US",
                "type": "fallback",
                "proxies": ["🇺🇸 United States丨01"],
            },
            {
                "name": "AI-SG",
                "type": "fallback",
                "proxies": ["🇸🇬 Singapore丨01"],
            },
        ]
    }
    (data_dir / "runtime.yaml").write_text(yaml.safe_dump(runtime, allow_unicode=True, sort_keys=False), encoding="utf-8")

    env = os.environ.copy()
    env["PYTHONPATH"] = "/root/clash_proxy/src"
    env["HOME"] = str(tmp_path)

    groups_result = _run(env, "list-groups")
    assert groups_result.returncode == 0
    assert "摘要" in groups_result.stdout
    assert "AI-MANUAL" in groups_result.stdout
    assert "AI-AUTO" in groups_result.stdout

    nodes_result = _run(env, "list-nodes", "AI-MANUAL")
    assert nodes_result.returncode == 0
    assert "列表" in nodes_result.stdout
    assert "AI-US" in nodes_result.stdout

    current_result = _run(env, "current", "AI-MANUAL")
    assert current_result.returncode == 0
    assert "当前选择: AI-AUTO" in current_result.stdout


def test_ai_commands_report_friendly_error_when_api_unavailable(tmp_path: Path):
    config_dir = tmp_path / ".config" / "cproxy"
    config_dir.mkdir(parents=True)
    (config_dir / "config.yaml").write_text(
        "external-controller: 127.0.0.1:9\n"
        "mixed-port: 7890\n",
        encoding="utf-8",
    )

    env = os.environ.copy()
    env["PYTHONPATH"] = "/root/clash_proxy/src"
    env["HOME"] = str(tmp_path)

    ai_status_result = _run(env, "ai-status")
    assert ai_status_result.returncode != 0
    assert "错误: Mihomo API 不可访问" in ai_status_result.stderr

    test_group_result = _run(env, "test-group", "AI-AUTO")
    assert test_group_result.returncode != 0
    assert "错误: Mihomo API 不可访问" in test_group_result.stderr
