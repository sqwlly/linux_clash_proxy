#!/bin/bash

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPT="${PROJECT_DIR}/proxy.sh"
TMP_DIR="$(mktemp -d)"
PORT_FILE="${TMP_DIR}/port"
CONFIG_FILE="${TMP_DIR}/runtime.yaml"
STATE_FILE="${TMP_DIR}/state.json"
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

assert_not_contains() {
    local haystack="$1"
    local needle="$2"
    local message="$3"

    if [[ "$haystack" == *"$needle"* ]]; then
        echo "ASSERTION FAILED: $message" >&2
        echo "Did not expect to find: $needle" >&2
        echo "Actual output:" >&2
        printf '%s\n' "$haystack" >&2
        exit 1
    fi
}

cat >"$STATE_FILE" <<'EOF'
{
  "AI-MANUAL": {
    "type": "Selector",
    "now": "AI-AUTO",
    "alive": true,
    "all": ["AI-AUTO", "AI-US", "AI-SG"]
  },
  "AI-AUTO": {
    "type": "Fallback",
    "now": "AI-US",
    "alive": true,
    "all": ["AI-US", "AI-SG"]
  },
  "AI-US": {
    "type": "Fallback",
    "now": "🇺🇸 United States丨01",
    "alive": true,
    "all": ["🇺🇸 United States丨01"]
  },
  "AI-SG": {
    "type": "Fallback",
    "now": "🇸🇬 Singapore丨01",
    "alive": true,
    "all": ["🇸🇬 Singapore丨01"]
  }
}
EOF

python3 - "$PORT_FILE" "$STATE_FILE" >"$SERVER_LOG" 2>&1 <<'PY' &
import json
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import unquote

port_file, state_file = sys.argv[1:3]


def load_state():
    with open(state_file, "r", encoding="utf-8") as fh:
        return json.load(fh)


def save_state(state):
    with open(state_file, "w", encoding="utf-8") as fh:
        json.dump(state, fh)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/version":
            self._send({"version": "test"})
            return
        if self.path == "/proxies":
            self._send({"proxies": load_state()})
            return

        self.send_response(404)
        self.end_headers()

    def do_PUT(self):
        if not self.path.startswith("/proxies/"):
            self.send_response(404)
            self.end_headers()
            return

        group_name = unquote(self.path[len("/proxies/"):])
        state = load_state()
        group = state.get(group_name)
        if not group:
            self.send_response(404)
            self.end_headers()
            return

        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length) or b"{}")
        group["now"] = payload.get("name", group.get("now"))
        state[group_name] = group
        save_state(state)
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

help_output="$(
    env "${COMMON_ENV[@]}" "$SCRIPT" --help
)"
assert_not_contains "$help_output" $'\033[' "--help 非 TTY 输出不应包含 ANSI 转义"
assert_not_contains "$help_output" '\033[' "--help 非 TTY 输出不应包含字面量颜色转义"

current_output="$(
    env "${COMMON_ENV[@]}" "$SCRIPT" current "AI-MANUAL"
)"
assert_contains "$current_output" "当前选择: AI-AUTO" "current 应输出摘要式当前选择"

switch_output="$(
    env "${COMMON_ENV[@]}" "$SCRIPT" switch "AI-MANUAL" "AI-SG"
)"
assert_contains "$switch_output" "切换结果" "switch 应输出切换结果区块"
assert_contains "$switch_output" "代理组: AI-MANUAL" "switch 应展示目标代理组"
assert_contains "$switch_output" "当前选择: AI-SG" "switch 应展示切换后的当前选择"

current_after_switch="$(
    env "${COMMON_ENV[@]}" "$SCRIPT" current "AI-MANUAL"
)"
assert_contains "$current_after_switch" "当前选择: AI-SG" "切换后 current 应反映新状态"

echo "current_switch_color_test: PASS"
