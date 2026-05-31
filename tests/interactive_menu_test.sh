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

output="$(printf '10\n\nq\n' | "$SCRIPT" menu)"

assert_contains "$output" "10) 导入订阅" "交互式菜单应展示订阅导入入口"
assert_contains "$output" "订阅 URL:" "订阅导入入口应提示输入 URL"
assert_contains "$output" "订阅 URL 不能为空" "空 URL 应安全返回菜单"

echo "interactive_menu_test: PASS"
