#!/bin/bash

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_EXAMPLE="${PROJECT_DIR}/systemd-user/cproxy-command.env.example"
OVERRIDE_EXAMPLE="${PROJECT_DIR}/systemd-user/example-proxied-service.override.conf"
SERVICE_FILE="${PROJECT_DIR}/systemd-user/cproxy.service"

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

assert_file_contains "$ENV_EXAMPLE" "HTTP_PROXY=http://127.0.0.1:7890" "用户级代理环境模板应包含 HTTP_PROXY"
assert_file_contains "$ENV_EXAMPLE" "ALL_PROXY=socks5h://127.0.0.1:7890" "用户级代理环境模板应包含 ALL_PROXY"
assert_file_contains "$ENV_EXAMPLE" "NO_PROXY=127.0.0.1,localhost" "用户级代理环境模板应包含 NO_PROXY"

assert_file_contains "$OVERRIDE_EXAMPLE" "EnvironmentFile=%h/.config/cproxy/cproxy-command.env" "用户级 override 示例应读取用户环境文件"
assert_file_contains "$OVERRIDE_EXAMPLE" "After=cproxy.service" "用户级 override 示例应依赖 cproxy.service"
assert_file_contains "$OVERRIDE_EXAMPLE" "Requires=cproxy.service" "用户级 override 示例应要求 cproxy.service"

assert_file_contains "$SERVICE_FILE" "ExecStart=%h/.local/bin/cproxy start" "用户级 service 应通过 cproxy 启动"
assert_file_contains "$SERVICE_FILE" "ExecStop=%h/.local/bin/cproxy stop" "用户级 service 应通过 cproxy 停止"
if grep -Fq "/root/clash_proxy" "$SERVICE_FILE"; then
    echo "ASSERTION FAILED: 用户级 service 不应包含仓库绝对路径" >&2
    exit 1
fi

echo "systemd_user_examples_test: PASS"
