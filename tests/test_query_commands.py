import json
import os
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Thread

import yaml

ROOT_DIR = Path(__file__).resolve().parents[1]
SRC_DIR = ROOT_DIR / "src"


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
            "AI-GEMINI": {
                "type": "Fallback",
                "now": "🇰🇷 Korea丨01",
                "alive": True,
                "all": ["🇰🇷 Korea丨01"],
                "history": [{"delay": 88}],
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
        if self.path == "http://probe.local/gemini":
            self._send(403, "need key")
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
        cwd=ROOT_DIR,
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
            "ai-openai-api-url: http://probe.local/openai-api\n"
            "ai-gemini-api-url: http://probe.local/gemini\n",
            encoding="utf-8",
        )

        env = os.environ.copy()
        env["PYTHONPATH"] = str(SRC_DIR)
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
        assert "AI 出口" in groups_result.stdout

        alias_result = _run(env, "group")
        assert alias_result.returncode == 0
        assert "AI 出口" in alias_result.stdout

        nodes_result = _run(env, "list-nodes", "AI-MANUAL")
        assert nodes_result.returncode == 0
        assert "摘要" in nodes_result.stdout
        assert "列表" in nodes_result.stdout
        assert "当前选择: AI-AUTO" in nodes_result.stdout

        display_nodes = _run(env, "list-nodes", "AI 出口")
        assert display_nodes.returncode == 0
        assert "目标组: AI 出口" in display_nodes.stdout
        assert "内部组: AI-MANUAL" in display_nodes.stdout

        untitled = _run(env, "list-nodes")
        assert untitled.returncode == 0
        assert "AI 出口" in untitled.stdout
        assert "cproxy list-nodes 'AI 出口'" in untitled.stdout

        ai_status_result = _run(env, "ai-status")
        assert ai_status_result.returncode == 0
        assert "摘要" in ai_status_result.stdout
        # 出口必须是解析后的**真实节点**：旧实现在 AI-MANUAL 直接钉住节点时会退化成 "-"
        assert "出口      United States 01" in ai_status_result.stdout
        assert "自动切换 · 池 AI-US" in ai_status_result.stdout
        assert "延迟      95ms" in ai_status_result.stdout
        assert "备用      AI-SG · Singapore 01 · 99ms · " in ai_status_result.stdout
        assert "Gemini    Korea 01" in ai_status_result.stdout
        assert "连通性" in ai_status_result.stdout
        assert "正常  ChatGPT Web  http://probe.local/chatgpt" in ai_status_result.stdout
        assert "失败  OpenAI API  http://probe.local/openai-api" in ai_status_result.stdout
        assert "正常  Gemini API  http://probe.local/gemini" in ai_status_result.stdout
        assert "部分异常" in ai_status_result.stdout
        # 旧「链路」区块已移除，链路信息并入「模式」(池) 与「出口」两行
        assert "链路" not in ai_status_result.stdout
        # 「候选池」取代旧「分组」：自动池要标出它的当前项是区域组指针、不是真实出口
        assert "候选池" in ai_status_result.stdout
        assert "自动池 AI-US → United States 01" in ai_status_result.stdout

        ai_status_raw = _run(env, "ai-status", "--raw")
        assert ai_status_raw.returncode == 0
        assert "AI-MANUAL: type=Selector now=AI-AUTO" in ai_status_raw.stdout
        assert "AI-GEMINI: type=Fallback now=🇰🇷 Korea丨01" in ai_status_raw.stdout
        # delay 取出口节点的真实延迟：fixed fallback 组自身 history 为空，旧实现打成 None
        assert "delay=88" in ai_status_raw.stdout
        assert "AI-MODE: auto 池=AI-US" in ai_status_raw.stdout
        assert "AI-EXIT: node=🇺🇸 United States丨01 delay=95 alive=True" in ai_status_raw.stdout
        assert "AI-STANDBY: group=AI-SG node=🇸🇬 Singapore丨01 delay=99 alive=True" in ai_status_raw.stdout
        assert "AI-PROBE: 部分异常" in ai_status_raw.stdout

        ai_status_json = _run(env, "ai-status", "--json")
        # 夹具里有一个探测失败 ⇒ 机器可读模式以退出码 1 + ok=false 上报（人类面板恒返回 0）
        assert ai_status_json.returncode == 1
        payload = json.loads(ai_status_json.stdout)
        assert payload["schema_version"] == 1
        assert payload["ok"] is False
        assert payload["recommended_actions"] == ["cproxy incident"]
        route = payload["data"]["route"]
        assert route["auto_mode"] is True
        assert route["mode_label"] == "自动切换"
        assert route["active_group"] == "AI-US"
        assert route["active_node"] == "🇺🇸 United States丨01"
        assert route["active_delay"] == 95
        assert route["standby_group"] == "AI-SG"
        assert route["gemini_present"] is True
        assert route["gemini_node"] == "🇰🇷 Korea丨01"
        assert route["gemini_delay"] == 88
        assert payload["data"]["probe_status"] == "部分异常"
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
    env["PYTHONPATH"] = str(SRC_DIR)
    env["HOME"] = str(tmp_path)

    groups_result = _run(env, "list-groups")
    assert groups_result.returncode == 0
    assert "摘要" in groups_result.stdout
    assert "AI-MANUAL" in groups_result.stdout
    assert "AI-AUTO" in groups_result.stdout

    nodes_result = _run(env, "list-nodes", "AI-MANUAL")
    assert nodes_result.returncode == 0
    assert "列表" in nodes_result.stdout
    assert "当前选择: AI-AUTO" in nodes_result.stdout

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
    env["PYTHONPATH"] = str(SRC_DIR)
    env["HOME"] = str(tmp_path)

    ai_status_result = _run(env, "ai-status")
    assert ai_status_result.returncode != 0
    assert "错误: Mihomo API 不可访问" in ai_status_result.stderr

    test_group_result = _run(env, "test-group", "AI-AUTO")
    assert test_group_result.returncode != 0
    assert "错误: Mihomo API 不可访问" in test_group_result.stderr
