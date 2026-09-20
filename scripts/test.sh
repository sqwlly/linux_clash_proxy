#!/bin/bash

set -euo pipefail

SCRIPT_DIR="${BASH_SOURCE[0]%/*}"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
PYTHON_BIN="${CPROXY_PYTHON:-python3}"

if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
    echo "错误: 找不到 Python 解释器: ${PYTHON_BIN}" >&2
    exit 1
fi

if ! "$PYTHON_BIN" -c 'import pytest, yaml' >/dev/null 2>&1; then
    echo "错误: ${PYTHON_BIN} 缺少测试依赖 pytest 或 PyYAML" >&2
    exit 1
fi

echo "测试解释器: $($PYTHON_BIN -c 'import sys; print(sys.executable)')"
cd "$ROOT_DIR"
exec "$PYTHON_BIN" -m pytest "$@"
