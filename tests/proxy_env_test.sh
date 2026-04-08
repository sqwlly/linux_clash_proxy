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
        exit 1
    fi
}

usage_output="$("$SCRIPT" --help)"
assert_contains "$usage_output" "proxy-env" "usage 应展示 proxy-env 命令"
assert_contains "$usage_output" "with-proxy" "usage 应展示 with-proxy 命令"
assert_contains "$usage_output" "proxy-shell" "usage 应展示 proxy-shell 命令"

proxy_env_output="$("$SCRIPT" proxy-env)"
assert_contains "$proxy_env_output" "HTTP_PROXY=http://127.0.0.1:7890" "proxy-env 应输出 HTTP_PROXY"
assert_contains "$proxy_env_output" "HTTPS_PROXY=http://127.0.0.1:7890" "proxy-env 应输出 HTTPS_PROXY"
assert_contains "$proxy_env_output" "ALL_PROXY=socks5h://127.0.0.1:7890" "proxy-env 应输出 ALL_PROXY"
assert_contains "$proxy_env_output" "NO_PROXY=127.0.0.1,localhost" "proxy-env 应输出 NO_PROXY"

with_proxy_output="$("$SCRIPT" with-proxy env)"
assert_contains "$with_proxy_output" "HTTP_PROXY=http://127.0.0.1:7890" "with-proxy 应向子进程注入 HTTP_PROXY"
assert_contains "$with_proxy_output" "HTTPS_PROXY=http://127.0.0.1:7890" "with-proxy 应向子进程注入 HTTPS_PROXY"
assert_contains "$with_proxy_output" "ALL_PROXY=socks5h://127.0.0.1:7890" "with-proxy 应向子进程注入 ALL_PROXY"
assert_contains "$with_proxy_output" "NO_PROXY=127.0.0.1,localhost" "with-proxy 应向子进程注入 NO_PROXY"

echo "proxy_env_test: PASS"
