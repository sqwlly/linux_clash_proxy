#!/bin/bash

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
INSTALL_SCRIPT="${PROJECT_DIR}/systemd-user/install-systemd-user.sh"
GENERATE_SCRIPT="${PROJECT_DIR}/systemd-user/generate-proxied-service.sh"

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

assert_file_contains "$INSTALL_SCRIPT" "\${HOME}/.config/systemd/user/cproxy.service" "用户级安装脚本应安装主服务"
assert_file_contains "$INSTALL_SCRIPT" "\${HOME}/.config/systemd/user/cproxy-refresh.timer" "用户级安装脚本应安装定时器"
assert_file_contains "$INSTALL_SCRIPT" "\${HOME}/.config/cproxy/cproxy-command.env.example" "用户级安装脚本应安装 example 环境文件"
assert_file_contains "$INSTALL_SCRIPT" 'if [ ! -f "${HOME}/.config/cproxy/cproxy-command.env" ]' "用户级安装脚本只应在正式环境文件不存在时初始化它"
assert_file_contains "$INSTALL_SCRIPT" "systemctl --user enable --now cproxy.service" "用户级安装脚本应启用主服务"
assert_file_contains "$INSTALL_SCRIPT" "systemctl --user enable --now cproxy-refresh.timer" "用户级安装脚本应启用定时器"

assert_file_contains "$GENERATE_SCRIPT" "%h/.config/systemd/user/" "用户级生成脚本应输出 drop-in 目录"
assert_file_contains "$GENERATE_SCRIPT" "EnvironmentFile=%h/.config/cproxy/cproxy-command.env" "用户级生成脚本应包含代理环境文件"
assert_file_contains "$GENERATE_SCRIPT" "After=cproxy.service" "用户级生成脚本应让目标服务依赖 cproxy.service"

echo "systemd_user_helper_scripts_test: PASS"
