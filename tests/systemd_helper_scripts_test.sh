#!/bin/bash

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
INSTALL_SCRIPT="${PROJECT_DIR}/systemd/install-systemd.sh"
GENERATE_SCRIPT="${PROJECT_DIR}/systemd/generate-proxied-service.sh"

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

assert_file_contains "$INSTALL_SCRIPT" "/etc/systemd/system/clash-proxy.service" "安装脚本应安装主服务"
assert_file_contains "$INSTALL_SCRIPT" "/etc/systemd/system/clash-proxy-refresh.timer" "安装脚本应安装定时器"
assert_file_contains "$INSTALL_SCRIPT" "/etc/systemd/system/clash-proxy-refresh.path" "安装脚本应安装 path 单元"
assert_file_contains "$INSTALL_SCRIPT" "/etc/systemd/system/clash-proxy-subscription.service" "安装脚本应安装每日订阅 service"
assert_file_contains "$INSTALL_SCRIPT" "/etc/systemd/system/clash-proxy-subscription.timer" "安装脚本应安装每日订阅 timer"
assert_file_contains "$INSTALL_SCRIPT" "/etc/default/clash-proxy-command.example" "安装脚本应始终安装 example 环境文件"
assert_file_contains "$INSTALL_SCRIPT" 'if [ ! -f "${DEFAULT_ENV_DIR}/clash-proxy-command" ]' "安装脚本只应在正式环境文件不存在时初始化它"
assert_file_contains "$INSTALL_SCRIPT" "systemctl enable --now clash-proxy.service" "安装脚本应启用主服务"
assert_file_contains "$INSTALL_SCRIPT" "systemctl enable --now clash-proxy-refresh.timer" "安装脚本应启用定时器"
assert_file_contains "$INSTALL_SCRIPT" "systemctl enable --now clash-proxy-refresh.path" "安装脚本应启用 path 单元"
assert_file_contains "$INSTALL_SCRIPT" "systemctl enable --now clash-proxy-subscription.timer" "安装脚本应启用每日订阅 timer"

assert_file_contains "${PROJECT_DIR}/systemd/clash-proxy-refresh.path" "PathChanged=/root/clash_proxy/config.yaml" "path 单元应监听 root 级 config.yaml"
assert_file_contains "${PROJECT_DIR}/systemd/clash-proxy-refresh.path" "Unit=clash-proxy-refresh.service" "path 单元应触发 refresh service"

assert_file_contains "${PROJECT_DIR}/systemd/clash-proxy-subscription.timer" "Unit=clash-proxy-subscription.service" "订阅 timer 应触发订阅 service"
assert_file_contains "${PROJECT_DIR}/systemd/clash-proxy-subscription.timer" "OnCalendar=*-*-* 04:00:00" "订阅 timer 应每日凌晨触发"
assert_file_contains "${PROJECT_DIR}/systemd/clash-proxy-subscription.timer" "RandomizedDelaySec=30min" "订阅 timer 应带随机延迟避开整点"
assert_file_contains "${PROJECT_DIR}/systemd/clash-proxy-subscription.timer" "Persistent=true" "订阅 timer 应在错过后补跑"
assert_file_contains "${PROJECT_DIR}/systemd/clash-proxy-subscription.service" "ExecStart=/root/clash_proxy/systemd/clash-proxy-subscription.sh" "订阅 service 应执行仓库内编排脚本"
assert_file_contains "${PROJECT_DIR}/systemd/clash-proxy-subscription.sh" "update_subscription_prod.py" "订阅编排脚本应调用订阅更新 helper"
assert_file_contains "${PROJECT_DIR}/systemd/clash-proxy-subscription.sh" "rules_delta" "订阅编排脚本应输出规则变化日志"

assert_file_contains "$GENERATE_SCRIPT" "/etc/systemd/system/" "生成脚本应输出 drop-in 目录"
assert_file_contains "$GENERATE_SCRIPT" "EnvironmentFile=/etc/default/clash-proxy-command" "生成脚本应包含代理环境文件"
assert_file_contains "$GENERATE_SCRIPT" "After=clash-proxy.service" "生成脚本应让目标服务依赖 clash-proxy.service"

echo "systemd_helper_scripts_test: PASS"
