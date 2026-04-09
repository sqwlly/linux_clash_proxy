#!/bin/bash

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICE_FILE="${PROJECT_DIR}/systemd/clash-proxy.service"
GENERATE_SCRIPT="${PROJECT_DIR}/systemd/generate-proxied-service.sh"
PROXY_SCRIPT="${PROJECT_DIR}/proxy.sh"

assert_contains() {
    local haystack="$1"
    local needle="$2"
    local message="$3"

    if [[ "$haystack" != *"$needle"* ]]; then
        echo "ASSERTION FAILED: $message" >&2
        echo "Expected to find: $needle" >&2
        exit 1
    fi
}

service_text="$(cat "$SERVICE_FILE")"
assert_contains "$service_text" "ExecStart=/root/clash_proxy/proxy.sh restart" "systemd 主服务应通过 restart 接管已有实例"

proxy_text="$(cat "$PROXY_SCRIPT")"
assert_contains "$proxy_text" 'LOG_FILE="${LOG_FILE:-${XDG_STATE_HOME:-$HOME/.local/state}/clash_proxy/clash.log}"' "旧版 proxy.sh 默认日志应写入 state 目录而不是仓库根目录"

generate_codex_output="$("$GENERATE_SCRIPT" codex)"
assert_contains "$generate_codex_output" "/etc/systemd/system/codex.service.d/proxy.conf" "裸服务名应生成正确 drop-in 路径"

generate_codex_service_output="$("$GENERATE_SCRIPT" codex.service)"
assert_contains "$generate_codex_service_output" "/etc/systemd/system/codex.service.d/proxy.conf" ".service 输入应被归一化为正确 drop-in 路径"

echo "review_fixes_test: PASS"
