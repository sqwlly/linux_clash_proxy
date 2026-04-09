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
from urllib.parse import unquote

port_file = sys.argv[1]

proxies_payload = {
    "proxies": {
        "AI-MANUAL": {
            "type": "Selector",
            "now": "AI-AUTO",
            "alive": True,
            "all": ["AI-AUTO", "AI-US", "AI-SG", "🇺🇸 United States", "🇸🇬 Singapore"],
        },
        "AI-AUTO": {
            "type": "Fallback",
            "now": "AI-US",
            "alive": True,
            "all": ["AI-US", "AI-SG"],
        },
        "AI-US": {
            "type": "Fallback",
            "now": "🇺🇸 United States丨01",
            "alive": True,
            "all": ["🇺🇸 United States丨01"],
        },
        "AI-SG": {
            "type": "Fallback",
            "now": "🇸🇬 Singapore丨01",
            "alive": True,
            "all": ["🇸🇬 Singapore丨01"],
        },
        "🇺🇸 United States": {
            "type": "Selector",
            "now": "🇺🇸 United States丨01",
            "alive": True,
            "all": ["🇺🇸 United States丨01"],
        },
        "🇸🇬 Singapore": {
            "type": "Selector",
            "now": "🇸🇬 Singapore丨01",
            "alive": True,
            "all": ["🇸🇬 Singapore丨01"],
        },
    }
}

delay_map = {
    "AI-US": 320,
    "AI-SG": 92,
}


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/version":
            self._send({"version": "test"})
            return

        if self.path == "/proxies":
            self._send(proxies_payload)
            return

        if self.path.startswith("/proxies/") and self.path.endswith("/delay?url=https%3A%2F%2Fcp.cloudflare.com%2Fgenerate_204&timeout=5000"):
            target = self.path[len("/proxies/"):].split("/delay", 1)[0]
            target = unquote(target)
            if target in delay_map:
                self._send({"delay": delay_map[target]})
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
mixed-port: 7890
EOF

COMMON_ENV=(
    RUNTIME_CONFIG_FILE="$CONFIG_FILE"
    SOURCE_CONFIG_FILE="$CONFIG_FILE"
)

list_nodes_output="$(
    env "${COMMON_ENV[@]}" "$SCRIPT" list-nodes "AI-MANUAL"
)"

assert_contains "$list_nodes_output" "当前选择: AI-AUTO" "list-nodes 应显示当前选择"
assert_contains "$list_nodes_output" "候选列表" "list-nodes 应输出候选列表区块"
assert_contains "$list_nodes_output" "当前  AI-AUTO" "list-nodes 应标识当前候选项"
assert_contains "$list_nodes_output" "候选  United States" "list-nodes 应展示规整后的候选名称"

test_group_output="$(
    env "${COMMON_ENV[@]}" "$SCRIPT" test-group "AI-AUTO"
)"

assert_contains "$test_group_output" "检查摘要" "test-group 应输出检查摘要区块"
assert_contains "$test_group_output" "检查结果" "test-group 应输出检查结果区块"
assert_contains "$test_group_output" "可用: 2/2" "test-group 应展示可用统计"
assert_contains "$test_group_output" "最佳: AI-SG (92ms)" "test-group 应展示最佳节点"
assert_contains "$test_group_output" "最慢: AI-US (320ms)" "test-group 应展示最慢节点"

echo "list_nodes_test_group_test: PASS"
