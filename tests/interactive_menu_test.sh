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

set +e
{ printf '10\n'; tail -f /dev/null; } | timeout --preserve-status -s INT 1s "$SCRIPT" menu >/dev/null
sigint_rc="${PIPESTATUS[1]}"
set -e

if [ "$sigint_rc" -ne 130 ]; then
    echo "ASSERTION FAILED: Ctrl-C 应让交互式菜单以 130 退出，实际: $sigint_rc" >&2
    exit 1
fi

log_file="$(mktemp)"
printf 'line-1\n' > "$log_file"
set +e
LOG_FILE="$log_file" timeout --preserve-status -s INT 1s "$SCRIPT" logs >/dev/null
logs_sigint_rc="$?"
set -e
rm -f "$log_file"

if [ "$logs_sigint_rc" -ne 130 ]; then
    echo "ASSERTION FAILED: Ctrl-C 应让日志查看以 130 退出，实际: $logs_sigint_rc" >&2
    exit 1
fi

echo "interactive_menu_test: PASS"
