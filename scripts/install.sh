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

main() {
    require_cmd python3

    if command -v pipx >/dev/null 2>&1; then
        install_with_pipx
    else
        install_with_pip
    fi

    PYTHONPATH="${ROOT_DIR}/src${PYTHONPATH:+:${PYTHONPATH}}" \
        python3 -m cproxy.cli init >/dev/null

    echo "安装完成"
    echo "命令入口: cproxy"
    echo "用户配置: ${XDG_CONFIG_HOME:-$HOME/.config}/cproxy/config.yaml"
}

main "$@"
