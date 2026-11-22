#!/bin/bash

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
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

assert_file_contains "$INSTALL_SCRIPT" 'USER_SYSTEMD_DIR="${XDG_CONFIG_HOME:-${HOME}/.config}/systemd/user"' "用户级安装脚本应定义 USER_SYSTEMD_DIR"
assert_file_contains "$INSTALL_SCRIPT" 'USER_CONFIG_DIR="${XDG_CONFIG_HOME:-${HOME}/.config}/cproxy"' "用户级安装脚本应定义 USER_CONFIG_DIR"
assert_file_contains "$INSTALL_SCRIPT" '${USER_SYSTEMD_DIR}/cproxy.service' "用户级安装脚本应使用 USER_SYSTEMD_DIR 安装主服务"
assert_file_contains "$INSTALL_SCRIPT" '${USER_SYSTEMD_DIR}/cproxy-h2-keepalive.service' "用户级安装脚本应安装并在摘要中列出 cproxy-h2-keepalive.service"
assert_file_contains "$INSTALL_SCRIPT" '${USER_SYSTEMD_DIR}/cproxy-refresh.timer' "用户级安装脚本应安装定时器"
assert_file_contains "$INSTALL_SCRIPT" '${USER_CONFIG_DIR}/cproxy-command.env.example' "用户级安装脚本应安装 example 环境文件"
assert_file_contains "$INSTALL_SCRIPT" 'if [ ! -f "${USER_CONFIG_DIR}/cproxy-command.env" ]' "用户级安装脚本只应在正式环境文件不存在时初始化它"
assert_file_contains "$INSTALL_SCRIPT" "systemctl --user enable --now cproxy.service" "用户级安装脚本应启用主服务"
assert_file_contains "$INSTALL_SCRIPT" "systemctl --user enable --now cproxy-refresh.timer" "用户级安装脚本应启用定时器"

assert_file_contains "$GENERATE_SCRIPT" "%h/.config/systemd/user/" "用户级生成脚本应输出 drop-in 目录"
assert_file_contains "$GENERATE_SCRIPT" "EnvironmentFile=%h/.config/cproxy/cproxy-command.env" "用户级生成脚本应包含代理环境文件"
assert_file_contains "$GENERATE_SCRIPT" "After=cproxy.service" "用户级生成脚本应让目标服务依赖 cproxy.service"

gen_out="$(bash "$GENERATE_SCRIPT" my-app.service)"
if ! grep -Fq "%h/.config/systemd/user/my-app.service.d/proxy.conf" <<<"$gen_out"; then
    echo "ASSERTION FAILED: generate-proxied-service.sh 应自动剥离 .service 后缀" >&2
    exit 1
fi
if grep -Fq "my-app.service.service.d" <<<"$gen_out"; then
    echo "ASSERTION FAILED: generate-proxied-service.sh 不应重复拼接 .service 后缀" >&2
    exit 1
fi

if grep -Fq "/root/clash_proxy" "$INSTALL_SCRIPT"; then
    echo "ASSERTION FAILED: 用户级安装脚本不应包含仓库绝对路径" >&2
    exit 1
fi

echo "systemd_user_helper_scripts_test: PASS"
