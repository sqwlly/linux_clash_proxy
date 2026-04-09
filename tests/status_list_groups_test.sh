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
if [ "${1:-}" = "-tln" ]; then
    cat <<OUT
State  Recv-Q Send-Q Local Address:Port Peer Address:Port
LISTEN 0      128    127.0.0.1:7890   0.0.0.0:*
OUT
    exit 0
fi

if [ "${1:-}" = "-tn" ]; then
    cat <<OUT
ESTAB 0 0 127.0.0.1:7890 127.0.0.1:51001
ESTAB 0 0 127.0.0.1:7890 127.0.0.1:51002
ESTAB 0 0 127.0.0.1:7890 127.0.0.1:51003
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

python3 - "$PORT_FILE" >"$SERVER_LOG" 2>&1 <<'PY' &
import json
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

port_file = sys.argv[1]
payload = {
    "proxies": {
        "AI-MANUAL": {"type": "Selector", "now": "AI-AUTO", "alive": True, "all": ["AI-AUTO", "AI-US", "AI-SG"]},
        "AI-AUTO": {"type": "Fallback", "now": "AI-US", "alive": True, "all": ["AI-US", "AI-SG"]},
        "AI-US": {"type": "Fallback", "now": "🇺🇸 United States丨01", "alive": True, "all": ["🇺🇸 United States丨01"]},
        "AI-SG": {"type": "Fallback", "now": "🇸🇬 Singapore丨01", "alive": True, "all": ["🇸🇬 Singapore丨01"]},
        "SSRDOG": {"type": "Selector", "now": "Auto", "alive": True, "all": ["Auto", "DIRECT"]},
        "Auto": {"type": "Fallback", "now": "🇭🇰 Hong Kong丨01", "alive": True, "all": ["🇭🇰 Hong Kong丨01"]},
        "🇺🇸 United States": {"type": "Selector", "now": "🇺🇸 United States丨01", "alive": True, "all": ["🇺🇸 United States丨01"]},
        "🇸🇬 Singapore": {"type": "Selector", "now": "🇸🇬 Singapore丨01", "alive": True, "all": ["🇸🇬 Singapore丨01"]},
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
    proxies: [AI-AUTO, AI-US, AI-SG]
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
    PROG_NAME="bash"
    SOURCE_CONFIG_FILE="$CONFIG_FILE"
    RUNTIME_CONFIG_FILE="$CONFIG_FILE"
    LOG_FILE="$LOG_FILE"
    PID_FILE="$PID_FILE"
)

status_output="$(
    env "${COMMON_ENV[@]}" "$SCRIPT" status
)"

assert_contains "$status_output" "运行摘要" "status 应输出运行摘要区块"
assert_contains "$status_output" "连接与资源" "status 应输出连接与资源区块"
assert_contains "$status_output" "配置路径" "status 应输出配置路径区块"
assert_contains "$status_output" "AI 当前出口: AI-US -> United States 01" "status 应展示 AI 当前实际出口"
assert_contains "$status_output" "连接数: 3" "status 应展示连接数"
assert_contains "$status_output" "内存: 59MB" "status 应展示规整后的内存值"

list_groups_output="$(
    env "${COMMON_ENV[@]}" "$SCRIPT" list-groups
)"

assert_contains "$list_groups_output" "组名" "list-groups 应输出表头"
assert_contains "$list_groups_output" "类型" "list-groups 应输出类型列"
assert_contains "$list_groups_output" "当前选择" "list-groups 应输出当前选择列"
assert_contains "$list_groups_output" "AI-MANUAL" "list-groups 应包含 AI-MANUAL"
assert_contains "$list_groups_output" "United States 01" "list-groups 应展示规整后的当前节点名称"

echo "status_list_groups_test: PASS"
