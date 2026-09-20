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

    if ! grep -Fq -- "$needle" "$file"; then
        echo "ASSERTION FAILED: $message" >&2
        echo "Expected to find: $needle" >&2
        echo "In file: $file" >&2
        exit 1
    fi
}

assert_file_contains "$INSTALL_SCRIPT" "--with-legacy" "legacy 单元必须只能通过显式参数安装"
assert_file_contains "$INSTALL_SCRIPT" 'if [ "$WITH_LEGACY" -eq 1 ]' "legacy 安装与启用必须受开关保护"
assert_file_contains "$INSTALL_SCRIPT" 'clash-proxy.service" "${SYSTEMD_DIR}/clash-proxy.service' "回滚模式应保留主服务安装能力"
assert_file_contains "$INSTALL_SCRIPT" 'clash-proxy-refresh.timer" "${SYSTEMD_DIR}/clash-proxy-refresh.timer' "回滚模式应保留刷新 timer"
assert_file_contains "$INSTALL_SCRIPT" 'clash-proxy-refresh.path" "${SYSTEMD_DIR}/clash-proxy-refresh.path' "回滚模式应保留 path 单元"
assert_file_contains "$INSTALL_SCRIPT" 'clash-proxy-subscription.service" "${SYSTEMD_DIR}/clash-proxy-subscription.service' "回滚模式应保留订阅 service"
assert_file_contains "$INSTALL_SCRIPT" 'clash-proxy-subscription.timer" "${SYSTEMD_DIR}/clash-proxy-subscription.timer' "回滚模式应保留订阅 timer"
assert_file_contains "$INSTALL_SCRIPT" 'clash-proxy-command.example"' "回滚模式应保留 example 环境文件"
assert_file_contains "$INSTALL_SCRIPT" 'if [ ! -f "${DEFAULT_ENV_DIR}/clash-proxy-command" ]' "安装脚本只应在正式环境文件不存在时初始化它"
assert_file_contains "$INSTALL_SCRIPT" "run systemctl enable --now clash-proxy.service" "回滚模式应能启用主服务"
assert_file_contains "$INSTALL_SCRIPT" "run systemctl enable --now clash-proxy-refresh.timer" "回滚模式应能启用定时器"
assert_file_contains "$INSTALL_SCRIPT" "run systemctl enable --now clash-proxy-refresh.path" "回滚模式应能启用 path 单元"
assert_file_contains "$INSTALL_SCRIPT" "run systemctl enable --now clash-proxy-subscription.timer" "回滚模式应能启用每日订阅 timer"

assert_file_contains "${PROJECT_DIR}/systemd/clash-proxy-refresh.path" "PathChanged=/root/clash_proxy/config.yaml" "path 单元应监听 root 级 config.yaml"
assert_file_contains "${PROJECT_DIR}/systemd/clash-proxy-refresh.path" "Unit=clash-proxy-refresh.service" "path 单元应触发 refresh service"

assert_file_contains "${PROJECT_DIR}/systemd/clash-proxy-subscription.timer" "Unit=clash-proxy-subscription.service" "订阅 timer 应触发订阅 service"
assert_file_contains "${PROJECT_DIR}/systemd/clash-proxy-subscription.timer" "OnCalendar=*-*-* 04:00:00" "订阅 timer 应每日凌晨触发"
assert_file_contains "${PROJECT_DIR}/systemd/clash-proxy-subscription.timer" "RandomizedDelaySec=30min" "订阅 timer 应带随机延迟避开整点"
assert_file_contains "${PROJECT_DIR}/systemd/clash-proxy-subscription.timer" "Persistent=true" "订阅 timer 应在错过后补跑"
assert_file_contains "${PROJECT_DIR}/systemd/clash-proxy-subscription.service" "ExecStart=/root/clash_proxy/systemd/clash-proxy-subscription.sh" "订阅 service 应执行仓库内编排脚本"
assert_file_contains "${PROJECT_DIR}/systemd/clash-proxy-subscription.sh" "update_subscription_prod.py" "订阅编排脚本应调用订阅更新 helper"
assert_file_contains "${PROJECT_DIR}/systemd/clash-proxy-subscription.sh" "rules_delta" "订阅编排脚本应输出规则变化日志"

assert_file_contains "$INSTALL_SCRIPT" 'clash-proxy-traffic-collector.service" "${SYSTEMD_DIR}/clash-proxy-traffic-collector.service' "安装脚本应安装流量采集 service"
assert_file_contains "$INSTALL_SCRIPT" 'clash-proxy-traffic-collector.timer" "${SYSTEMD_DIR}/clash-proxy-traffic-collector.timer' "安装脚本应安装流量采集 timer"
assert_file_contains "$INSTALL_SCRIPT" "run systemctl enable --now clash-proxy-traffic-collector.timer" "安装脚本应启用流量采集 timer"
assert_file_contains "${PROJECT_DIR}/systemd/clash-proxy-traffic-collector.timer" "Unit=clash-proxy-traffic-collector.service" "流量采集 timer 应触发采集 service"
assert_file_contains "${PROJECT_DIR}/systemd/clash-proxy-traffic-collector.service" "traffic collect" "流量采集 service 应调用 cproxy traffic collect"

assert_file_contains "$GENERATE_SCRIPT" "/etc/systemd/system/" "生成脚本应输出 drop-in 目录"
assert_file_contains "$GENERATE_SCRIPT" "EnvironmentFile=/etc/default/clash-proxy-command" "生成脚本应包含代理环境文件"
assert_file_contains "$GENERATE_SCRIPT" "After=clash-proxy.service" "生成脚本应让目标服务依赖 clash-proxy.service"

echo "systemd_helper_scripts_test: PASS"
