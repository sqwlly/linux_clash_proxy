#!/bin/bash

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPT="${PROJECT_DIR}/proxy.sh"
TMP_DIR="$(mktemp -d)"
PORT_FILE="${TMP_DIR}/port"
CONFIG_FILE="${TMP_DIR}/runtime.yaml"
SERVER_LOG="${TMP_DIR}/server.log"
SERVER_PID=""

cleanup() {
    if [ -n "$SERVER_PID" ] && kill -0 "$SERVER_PID" 2>/dev/null; then
        kill "$SERVER_PID" 2>/dev/null || true
        wait "$SERVER_PID" 2>/dev/null || true
    fi

    rm -rf "$TMP_DIR"
}

trap cleanup EXIT

assert_contains() {
    local haystack="$1"
    local needle="$2"
    local message="$3"

    if [[ "$haystack" != *"$needle"* ]]; then
        echo "ASSERTION FAILED: $message" >&2
        echo "Expected to find: $needle" >&2
        echo "Actual output:" >&2
        printf '%s\n' "$haystack" >&2
        exit 1
    fi
}

python3 - "$PORT_FILE" >"$SERVER_LOG" 2>&1 <<'PY' &
import json
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

port_file = sys.argv[1]
payload = {
    "proxies": {
        "AI-MANUAL": {
            "type": "Selector",
            "now": "AI-AUTO",
            "alive": True,
            "history": [],
        },
        "AI-AUTO": {
            "type": "Fallback",
            "now": "AI-US",
            "alive": True,
            "history": [],
        },
        "AI-US": {
            "type": "Fallback",
            "now": "🇺🇸 United States丨01",
            "alive": True,
            "history": [{"delay": 95}],
        },
        "AI-SG": {
            "type": "Fallback",
            "now": "🇸🇬 Singapore丨01",
            "alive": True,
            "history": [{"delay": 99}],
        },
        "🇺🇸 United States": {
            "type": "Selector",
            "now": "🇺🇸 United States丨01",
            "alive": True,
            "history": [{"delay": 96}],
        },
        "🇸🇬 Singapore": {
            "type": "Selector",
            "now": "🇸🇬 Singapore丨01",
            "alive": True,
            "history": [{"delay": 97}],
        },
    }
}


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/version":
            self._send({"version": "test"})
            return
        if self.path == "/proxies":
            self._send(payload)
            return
        if self.path == "http://probe.local/chatgpt":
            self._send_text(200, "ok")
            return
        if self.path == "http://probe.local/openai-api":
            self._send_text(502, "bad gateway")
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

    def _send_text(self, status, text):
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        return


server = HTTPServer(("127.0.0.1", 0), Handler)
with open(port_file, "w", encoding="utf-8") as fh:
    fh.write(str(server.server_port))

server.serve_forever()
PY
SERVER_PID="$!"

for _ in $(seq 1 50); do
    if [ -s "$PORT_FILE" ]; then
        break
    fi
    sleep 0.1
done

if [ ! -s "$PORT_FILE" ]; then
    echo "ASSERTION FAILED: 测试 API 服务未成功启动" >&2
    exit 1
fi

PORT="$(cat "$PORT_FILE")"

cat >"$CONFIG_FILE" <<EOF
external-controller: 127.0.0.1:${PORT}
mixed-port: ${PORT}
ai-chatgpt-url: http://probe.local/chatgpt
ai-openai-api-url: http://probe.local/openai-api
EOF

ai_status_output="$(
    RUNTIME_CONFIG_FILE="$CONFIG_FILE" \
    SOURCE_CONFIG_FILE="$CONFIG_FILE" \
    "$SCRIPT" ai-status
)"

assert_contains "$ai_status_output" "摘要" "ai-status 应输出摘要区块"
assert_contains "$ai_status_output" "AI 路由:" "ai-status 应输出顶部摘要"
assert_contains "$ai_status_output" "AI 探测: 部分异常" "ai-status 应输出 OpenAI 探测汇总"
assert_contains "$ai_status_output" "连通性" "ai-status 应输出 OpenAI 连通性区块"
assert_contains "$ai_status_output" "正常  ChatGPT Web  http://probe.local/chatgpt" "ai-status 应展示 ChatGPT Web 探测结果"
assert_contains "$ai_status_output" "失败  OpenAI API  http://probe.local/openai-api" "ai-status 应展示 OpenAI API 探测结果"
assert_contains "$ai_status_output" "链路" "ai-status 应输出当前链路区块"
assert_contains "$ai_status_output" "备用" "ai-status 应输出备用路径区块"
assert_contains "$ai_status_output" "分组" "ai-status 应输出分组状态区块"
assert_contains "$ai_status_output" "自动切换" "ai-status 应明确展示当前是否处于自动模式"
assert_contains "$ai_status_output" "当前出口=United States 01" "ai-status 应将当前节点名称规整为自然空格分隔"
assert_contains "$ai_status_output" "AI-SG -> Singapore 01" "ai-status 应展示备用路径的实际节点"
assert_contains "$ai_status_output" "当前: United States 01" "ai-status 应在分组状态中展示规整后的节点名"

echo "ai_status_test: PASS"
