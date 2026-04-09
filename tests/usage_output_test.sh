#!/bin/bash

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPT="${PROJECT_DIR}/proxy.sh"

assert_contains() {
    local haystack="$1"
    local needle="$2"
    local message="$3"

    if [[ "$haystack" != *"$needle"* ]]; then
        echo "ASSERTION FAILED: $message" >&2
        echo "Expected to find: $needle" >&2
        echo "Actual output:" >&2
        printf '%s\n' "$haystack" >&2
        exit 1
    fi
}

help_output="$("$SCRIPT" --help)"

assert_contains "$help_output" "配置与进程" "usage 应按场景分组展示配置与进程命令"
assert_contains "$help_output" "AI 路由控制" "usage 应按场景分组展示 AI 路由控制命令"
assert_contains "$help_output" "命令级代理" "usage 应按场景分组展示命令级代理命令"
assert_contains "$help_output" "诊断与排障" "usage 应按场景分组展示诊断与排障命令"
assert_contains "$help_output" "list-nodes <group>" "usage 应保留节点查看命令"
assert_contains "$help_output" "with-proxy <cmd...>" "usage 应保留命令级代理示例"

echo "usage_output_test: PASS"
