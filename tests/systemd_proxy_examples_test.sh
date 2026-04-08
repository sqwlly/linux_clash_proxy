#!/bin/bash

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_EXAMPLE="${PROJECT_DIR}/systemd/clash-proxy-command.env.example"
OVERRIDE_EXAMPLE="${PROJECT_DIR}/systemd/example-proxied-service.override.conf"

assert_file_contains() {
    local file="$1"
    local needle="$2"
    local message="$3"

    if [ ! -f "$file" ]; then
        echo "ASSERTION FAILED: missing file $file" >&2
        exit 1
    fi

    if ! grep -Fq "$needle" "$file"; then
        echo "ASSERTION FAILED: $message" >&2
        echo "Expected to find: $needle" >&2
        echo "In file: $file" >&2
        exit 1
    fi
}

assert_file_contains "$ENV_EXAMPLE" "HTTP_PROXY=http://127.0.0.1:7890" "代理环境模板应包含 HTTP_PROXY"
assert_file_contains "$ENV_EXAMPLE" "ALL_PROXY=socks5h://127.0.0.1:7890" "代理环境模板应包含 ALL_PROXY"
assert_file_contains "$ENV_EXAMPLE" "NO_PROXY=127.0.0.1,localhost" "代理环境模板应包含 NO_PROXY"

assert_file_contains "$OVERRIDE_EXAMPLE" "EnvironmentFile=/etc/default/clash-proxy-command" "override 示例应从统一环境文件读取代理配置"
assert_file_contains "$OVERRIDE_EXAMPLE" "After=clash-proxy.service" "override 示例应依赖 clash-proxy.service"
assert_file_contains "$OVERRIDE_EXAMPLE" "Requires=clash-proxy.service" "override 示例应要求 clash-proxy.service"

echo "systemd_proxy_examples_test: PASS"
