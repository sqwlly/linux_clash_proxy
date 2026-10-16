#!/bin/bash

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
INSTALL_SCRIPT="${PROJECT_DIR}/systemd/install-systemd.sh"

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

assert_file_lacks() {
    local file="$1"
    local needle="$2"
    local message="$3"

    if [ -f "$file" ] && grep -Fq -- "$needle" "$file"; then
        echo "ASSERTION FAILED: $message" >&2
        echo "Did not expect to find: $needle" >&2
        echo "In file: $file" >&2
        exit 1
    fi
}

# 系统级安装脚本只剩「装上活跃的流量采集单元」这一件事——它名字带 legacy 前缀，
# 但 ExecStart 调的是 cproxy（见 AGENTS.md 的「不要按名字前缀判断归属」）。
assert_file_contains "$INSTALL_SCRIPT" 'clash-proxy-traffic-collector.service" "${SYSTEMD_DIR}/clash-proxy-traffic-collector.service' "安装脚本应安装流量采集 service"
assert_file_contains "$INSTALL_SCRIPT" 'clash-proxy-traffic-collector.timer" "${SYSTEMD_DIR}/clash-proxy-traffic-collector.timer' "安装脚本应安装流量采集 timer"
assert_file_contains "$INSTALL_SCRIPT" "run systemctl enable --now clash-proxy-traffic-collector.timer" "安装脚本应启用流量采集 timer"
assert_file_contains "${PROJECT_DIR}/systemd/clash-proxy-traffic-collector.timer" "Unit=clash-proxy-traffic-collector.service" "流量采集 timer 应触发采集 service"
assert_file_contains "${PROJECT_DIR}/systemd/clash-proxy-traffic-collector.service" "traffic collect" "流量采集 service 应调用 cproxy traffic collect"

# 退役后不应再有 legacy 安装能力：既无开关，也不引用已删除的单元
assert_file_lacks "$INSTALL_SCRIPT" "--with-legacy" "安装脚本不应再有 legacy 安装开关"
assert_file_lacks "$INSTALL_SCRIPT" "WITH_LEGACY" "安装脚本不应再有 legacy 开关变量"
assert_file_lacks "$INSTALL_SCRIPT" "clash-proxy.service" "安装脚本不应再引用已删除的 legacy 主服务"
assert_file_lacks "$INSTALL_SCRIPT" "clash-proxy-subscription" "安装脚本不应再引用已删除的订阅单元"

# 活跃单元不应再挂已删除的 legacy 服务名（悬空 After=）
assert_file_lacks "${PROJECT_DIR}/systemd/clash-proxy-traffic-collector.service" "clash-proxy.service" "采集 service 的 After= 不应再引用已删除的 legacy 服务"

echo "systemd_helper_scripts_test: PASS"
