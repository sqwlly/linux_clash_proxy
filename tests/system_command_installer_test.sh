#!/bin/bash

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
INSTALLER="${PROJECT_DIR}/scripts/install-system-commands.sh"
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

assert_file_contains() {
    local file="$1"
    local needle="$2"
    local message="$3"

    if [ ! -f "$file" ]; then
        echo "ASSERTION FAILED: missing file $file" >&2
        exit 1
    fi

    if ! grep -Fq "$needle" "$file"; then
        echo "ASSERTION FAILED: $message" >&2
        echo "Expected to find: $needle" >&2
        echo "In file: $file" >&2
        exit 1
    fi
}

dry_run_output="$("$INSTALLER" --dry-run --bindir "${TMP_DIR}/bin")"
if [ -d "${TMP_DIR}/bin" ]; then
    echo "ASSERTION FAILED: dry-run should not create bindir" >&2
    exit 1
fi
if ! grep -Fq "DRY-RUN install clash-proxy -> proxy.sh" <<< "$dry_run_output"; then
    echo "ASSERTION FAILED: dry-run should list clash-proxy action" >&2
    exit 1
fi
if ! grep -Fq "DRY-RUN install payload proxy.sh" <<< "$dry_run_output"; then
    echo "ASSERTION FAILED: dry-run should list proxy.sh payload action" >&2
    exit 1
fi

"$INSTALLER" --bindir "${TMP_DIR}/bin" --libdir "${TMP_DIR}/lib/clash-proxy" >/dev/null

test -x "${TMP_DIR}/bin/clash-proxy"
test -x "${TMP_DIR}/bin/clash-proxy-update"
test -x "${TMP_DIR}/lib/clash-proxy/proxy.sh"
test -x "${TMP_DIR}/lib/clash-proxy/update_config.sh"
test ! -e "${TMP_DIR}/bin/cproxy"
test ! -e "${TMP_DIR}/bin/cproxy-update"

assert_file_contains "${TMP_DIR}/bin/clash-proxy" 'WRAPPER_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"' "clash-proxy 应基于自身位置解析目标"
assert_file_contains "${TMP_DIR}/bin/clash-proxy" 'exec "${WRAPPER_DIR}/' "clash-proxy 应使用相对目标"
assert_file_contains "${TMP_DIR}/bin/clash-proxy" 'CLASH_PROXY_CLI_NAME="clash-proxy"' "clash-proxy 应向帮助输出传递稳定命令名"
assert_file_contains "${TMP_DIR}/bin/clash-proxy" 'proxy.sh" "$@"' "clash-proxy 应转发到 root 生产入口"
assert_file_contains "${TMP_DIR}/bin/clash-proxy" '../lib/clash-proxy/proxy.sh' "clash-proxy 应指向安装后的 lib payload"
assert_file_contains "${TMP_DIR}/bin/clash-proxy-update" 'update_config.sh" "$@"' "clash-proxy-update 应转发到安全更新入口"
assert_file_contains "${TMP_DIR}/bin/clash-proxy-update" "PROJECT_DIR=\"${PROJECT_DIR}\"" "clash-proxy-update 应保留真实项目目录"

"$INSTALLER" --bindir "${TMP_DIR}/bin" --libdir "${TMP_DIR}/lib/clash-proxy" --with-cproxy-alias >/dev/null

test -x "${TMP_DIR}/bin/cproxy"
test -x "${TMP_DIR}/bin/cproxy-update"
assert_file_contains "${TMP_DIR}/bin/cproxy" 'proxy.sh" "$@"' "cproxy alias 应显式转发到 root 生产入口"
assert_file_contains "${TMP_DIR}/bin/cproxy-update" 'update_config.sh" "$@"' "cproxy-update alias 应显式转发到安全更新入口"

echo "system_command_installer_test: PASS"
