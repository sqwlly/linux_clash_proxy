#!/bin/bash

set -euo pipefail

SCRIPT_DIR="${BASH_SOURCE[0]%/*}"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

require_cmd() {
    local name="$1"
    if ! command -v "$name" >/dev/null 2>&1; then
        echo "错误: 缺少依赖命令: $name" >&2
        exit 1
    fi
}

install_with_pipx() {
    pipx install --force --editable "$ROOT_DIR"
}

install_with_pip() {
    python3 -m pip install --user --editable "$ROOT_DIR"
}

warn_missing_geodata() {
    local data_home="${XDG_DATA_HOME:-$HOME/.local/share}"
    local mmdb_path="${data_home}/cproxy/country.mmdb"
    if [ -f "$mmdb_path" ]; then
        echo "GeoIP 数据: ${mmdb_path}"
        return 0
    fi

    echo "警告: 未检测到 GeoIP 数据文件: ${mmdb_path}" >&2
    echo "提示: 在无代理或受限网络环境下，mihomo 可能无法自动获取 country.mmdb" >&2
    echo "提示: 可手动放置该文件后再运行 cproxy test" >&2
}

add_logrotate_cron() {
    local config_path="$1"
    local pattern="$2"
    local cron_job="0 */6 * * * /usr/sbin/logrotate ${config_path} >/dev/null 2>&1"

    if ! command -v crontab >/dev/null 2>&1; then
        echo "警告: 未检测到 crontab，跳过日志轮转定时任务配置" >&2
        return 0
    fi

    if ! crontab -l 2>/dev/null | grep -q "${pattern}"; then
        (crontab -l 2>/dev/null | grep -v "${pattern}"; echo "$cron_job") | crontab - 2>/dev/null || true
        echo "日志轮转定时任务: 已添加"
    fi
}

setup_logrotate() {
    local logrotate_dir="${CPROXY_LOGROTATE_DIR:-/etc/logrotate.d}"
    local state_home="${XDG_STATE_HOME:-$HOME/.local/state}"
    local log_file="${state_home}/cproxy/cproxy.log"
    local logrotate_conf="${logrotate_dir}/cproxy"

    if [ ! -f "${ROOT_DIR}/logrotate.conf.template" ]; then
        echo "警告: 未找到 logrotate 模板，跳过日志轮转配置" >&2
        return 0
    fi

    if [ ! -d "${logrotate_dir}" ]; then
        echo "警告: ${logrotate_dir} 不存在，跳过日志轮转配置" >&2
        return 0
    fi

    if [ ! -w "${logrotate_dir}" ]; then
        echo "警告: 无权限写入 ${logrotate_dir}，跳过日志轮转配置" >&2
        return 0
    fi

    sed -e "s|{{LOG_FILE}}|${log_file}|g" \
        "${ROOT_DIR}/logrotate.conf.template" > "${logrotate_conf}"

    echo "日志轮转配置: ${logrotate_conf}"

    add_logrotate_cron "${logrotate_conf}" "logrotate.*cproxy"
}

setup_legacy_logrotate() {
    local logrotate_dir="${CPROXY_LOGROTATE_DIR:-/etc/logrotate.d}"
    local logrotate_conf="${logrotate_dir}/clash_proxy"
    local state_home="${XDG_STATE_HOME:-$HOME/.local/state}"
    local log_file="${state_home}/clash_proxy/clash.log"

    if [ ! -d "${logrotate_dir}" ]; then
        return 0
    fi

    if [ ! -w "${logrotate_dir}" ]; then
        return 0
    fi

    cat > "${logrotate_conf}" << 'EOF'
{{LOG_FILE}} {
    daily
    size 10M
    rotate 7
    compress
    delaycompress
    missingok
    notifempty
    copytruncate
}
EOF

    sed -i "s|{{LOG_FILE}}|${log_file}|g" "${logrotate_conf}"

    add_logrotate_cron "${logrotate_conf}" "logrotate.*clash_proxy"
}

main() {
    require_cmd python3

    if command -v pipx >/dev/null 2>&1; then
        install_with_pipx
    else
        install_with_pip
    fi

    PYTHONPATH="${ROOT_DIR}/src${PYTHONPATH:+:${PYTHONPATH}}" \
        python3 -m cproxy.cli init >/dev/null

    warn_missing_geodata

    setup_logrotate
    setup_legacy_logrotate

    if PYTHONPATH="${ROOT_DIR}/src${PYTHONPATH:+:${PYTHONPATH}}" \
        CPROXY_LEGACY_ROOT="${ROOT_DIR}" \
        python3 -m cproxy.cli bootstrap; then
        echo "一键部署: 完成"
    else
        echo "警告: 一键部署未完成，可稍后手动执行 cproxy bootstrap" >&2
    fi

    echo "安装完成"
    echo "命令入口: cproxy"
    echo "用户配置: ${XDG_CONFIG_HOME:-$HOME/.config}/cproxy/config.yaml"
}

main "$@"
