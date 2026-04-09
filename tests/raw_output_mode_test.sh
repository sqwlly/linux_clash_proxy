#!/bin/bash

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPT="${PROJECT_DIR}/proxy.sh"
TMP_DIR="$(mktemp -d)"
STUB_DIR="${TMP_DIR}/bin"
PORT_FILE="${TMP_DIR}/port"
CONFIG_FILE="${TMP_DIR}/runtime.yaml"
LOG_FILE="${TMP_DIR}/clash.log"
PID_FILE="${TMP_DIR}/mihomo.pid"
STATE_FILE="${TMP_DIR}/state.json"
SERVER_LOG="${TMP_DIR}/server.log"
SERVER_PID=""
DUMMY_PID=""

cleanup() {
    if [ -n "$DUMMY_PID" ] && kill -0 "$DUMMY_PID" 2>/dev/null; then
        kill "$DUMMY_PID" 2>/dev/null || true
        wait "$DUMMY_PID" 2>/dev/null || true
    fi

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

mkdir -p "$STUB_DIR"

cat >"$STUB_DIR/ps" <<'EOF'
#!/bin/bash
if [[ "$*" == *"etimes="* ]]; then
    echo "3661"
    exit 0
fi
if [[ "$*" == *"rss="* ]]; then
    echo "60416"
    exit 0
fi
exit 1
EOF

cat >"$STUB_DIR/ss" <<'EOF'
#!/bin/bash
PORT="${PROXY_PORT:-7890}"
if [ "${1:-}" = "-tln" ]; then
    cat <<OUT
State  Recv-Q Send-Q Local Address:Port Peer Address:Port
LISTEN 0      128    127.0.0.1:${PORT}   0.0.0.0:*
OUT
    exit 0
fi

if [ "${1:-}" = "-tn" ]; then
    cat <<OUT
ESTAB 0 0 127.0.0.1:${PORT} 127.0.0.1:51001
ESTAB 0 0 127.0.0.1:${PORT} 127.0.0.1:51002
OUT
    exit 0
fi

exit 1
EOF

cat >"$STUB_DIR/du" <<'EOF'
#!/bin/bash
printf '8.0K\t%s\n' "${2:-$1}"
EOF

chmod +x "$STUB_DIR/ps" "$STUB_DIR/ss" "$STUB_DIR/du"

cat >"$STATE_FILE" <<'EOF'
{
  "AI-MANUAL": {
    "type": "Selector",
    "now": "AI-AUTO",
    "alive": true,
    "all": ["AI-AUTO", "AI-US", "AI-SG", "🇺🇸 United States", "🇸🇬 Singapore"],
    "history": []
  },
  "AI-AUTO": {
    "type": "Fallback",
    "now": "AI-US",
    "alive": true,
    "all": ["AI-US", "AI-SG"],
    "history": []
  },
  "AI-US": {
    "type": "Fallback",
    "now": "🇺🇸 United States丨01",
    "alive": true,
    "all": ["🇺🇸 United States丨01"],
    "history": [{"delay": 95}]
  },
  "AI-SG": {
    "type": "Fallback",
    "now": "🇸🇬 Singapore丨01",
    "alive": true,
    "all": ["🇸🇬 Singapore丨01"],
    "history": [{"delay": 99}]
  },
  "SSRDOG": {
    "type": "Selector",
    "now": "Auto",
    "alive": true,
    "all": ["Auto", "DIRECT"],
    "history": []
  },
  "Auto": {
    "type": "Fallback",
    "now": "🇭🇰 Hong Kong丨01",
    "alive": true,
    "all": ["🇭🇰 Hong Kong丨01"],
    "history": []
  },
  "🇺🇸 United States": {
    "type": "Selector",
    "now": "🇺🇸 United States丨01",
    "alive": true,
    "all": ["🇺🇸 United States丨01"],
    "history": [{"delay": 96}]
  },
  "🇸🇬 Singapore": {
    "type": "Selector",
    "now": "🇸🇬 Singapore丨01",
    "alive": true,
    "all": ["🇸🇬 Singapore丨01"],
    "history": [{"delay": 97}]
  }
}
EOF

python3 - "$PORT_FILE" "$STATE_FILE" >"$SERVER_LOG" 2>&1 <<'PY' &
import json
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import unquote, urlparse

port_file, state_file = sys.argv[1:3]
delay_map = {"AI-US": 320, "AI-SG": 92}


def load_state():
    with open(state_file, "r", encoding="utf-8") as fh:
        return json.load(fh)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/version":
            self._send({"version": "test"})
            return
        if self.path == "/proxies":
            self._send({"proxies": load_state()})
            return
        if self.path == "http://probe.local/chatgpt":
            self._send_text(200, "ok")
            return
        if self.path == "http://probe.local/openai-api":
            self._send_text(502, "bad gateway")
            return
        if self.path.startswith("/proxies/") and "/delay" in self.path:
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
proxy-groups:
  - name: SSRDOG
    type: select
    proxies: [Auto, DIRECT]
  - name: Auto
    type: fallback
    proxies: [🇭🇰 Hong Kong丨01]
  - name: AI-US
    type: fallback
    proxies: [🇺🇸 United States丨01]
  - name: AI-SG
    type: fallback
    proxies: [🇸🇬 Singapore丨01]
  - name: AI-AUTO
    type: fallback
    proxies: [AI-US, AI-SG]
  - name: AI-MANUAL
    type: select
    proxies: [AI-AUTO, AI-US, AI-SG, 🇺🇸 United States, 🇸🇬 Singapore]
  - name: 🇺🇸 United States
    type: select
    proxies: [🇺🇸 United States丨01]
  - name: 🇸🇬 Singapore
    type: select
    proxies: [🇸🇬 Singapore丨01]
EOF

printf 'test\n' >"$LOG_FILE"
bash -c 'while true; do sleep 1; done' _ -f "$CONFIG_FILE" &
DUMMY_PID="$!"
printf '%s\n' "$DUMMY_PID" >"$PID_FILE"

COMMON_ENV=(
    PATH="$STUB_DIR:$PATH"
    PROXY_PORT="$PORT"
    PROG_NAME="bash"
    SOURCE_CONFIG_FILE="$CONFIG_FILE"
    RUNTIME_CONFIG_FILE="$CONFIG_FILE"
    LOG_FILE="$LOG_FILE"
    PID_FILE="$PID_FILE"
)

current_raw="$(
    env "${COMMON_ENV[@]}" "$SCRIPT" current "AI-MANUAL" --raw
)"
assert_contains "$current_raw" "AI-AUTO" "current --raw 应只输出当前值"
assert_not_contains "$current_raw" "当前选择:" "current --raw 不应输出摘要文案"

list_nodes_raw="$(
    env "${COMMON_ENV[@]}" "$SCRIPT" list-nodes "AI-MANUAL" --raw
)"
assert_contains "$list_nodes_raw" "AI-AUTO" "list-nodes --raw 应保留纯候选列表"
assert_not_contains "$list_nodes_raw" "候选列表" "list-nodes --raw 不应输出摘要区块"

list_groups_raw="$(
    env "${COMMON_ENV[@]}" "$SCRIPT" list-groups --raw
)"
assert_contains "$list_groups_raw" $'AI-MANUAL\tselect' "list-groups --raw 应输出制表分隔旧格式"
assert_not_contains "$list_groups_raw" "当前选择" "list-groups --raw 不应输出表头"
assert_not_contains "$list_groups_raw" "=== 可切换代理组 ===" "list-groups --raw 不应输出人类标题"

status_raw="$(
    env "${COMMON_ENV[@]}" "$SCRIPT" status --raw
)"
assert_contains "$status_raw" "版本: 1.2.0" "status --raw 应输出旧式字段"
assert_contains "$status_raw" "状态: 运行中" "status --raw 应输出旧式状态字段"
assert_not_contains "$status_raw" "运行摘要" "status --raw 不应输出新区块"
assert_not_contains "$status_raw" "=== Mihomo 代理状态 ===" "status --raw 不应输出人类标题"
assert_not_contains "$status_raw" "实际运行配置:" "status --raw 在路径相同时不应重复展示实际运行配置"

ai_status_raw="$(
    env "${COMMON_ENV[@]}" "$SCRIPT" ai-status --raw
)"
assert_contains "$ai_status_raw" "AI-MANUAL: type=Selector now=AI-AUTO" "ai-status --raw 应输出旧式平铺字段"
assert_contains "$ai_status_raw" "AI-PROBE: 部分异常" "ai-status --raw 应输出探测汇总"
assert_contains "$ai_status_raw" "AI-PROBE-ITEM: name=ChatGPT Web ok=True detail=HTTP 200 url=http://probe.local/chatgpt" "ai-status --raw 应输出 ChatGPT Web 探测结果"
assert_not_contains "$ai_status_raw" "当前链路" "ai-status --raw 不应输出新区块"
assert_not_contains "$ai_status_raw" "=== AI 路由状态 ===" "ai-status --raw 不应输出人类标题"

test_group_raw="$(
    env "${COMMON_ENV[@]}" "$SCRIPT" test-group "AI-AUTO" --raw
)"
assert_contains "$test_group_raw" "AI-US: 320ms" "test-group --raw 应输出旧式逐行结果"
assert_not_contains "$test_group_raw" "检查摘要" "test-group --raw 不应输出摘要区块"
assert_not_contains "$test_group_raw" "=== 组 [AI-AUTO] 健康检查 ===" "test-group --raw 不应输出人类标题"

echo "raw_output_mode_test: PASS"
