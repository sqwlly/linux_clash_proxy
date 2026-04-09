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
