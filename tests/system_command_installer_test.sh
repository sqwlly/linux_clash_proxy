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

# 默认（不带 --with-legacy）不应安装任何东西——防止将来重跑把已停用的入口装回 PATH
default_output="$("$INSTALLER" --bindir "${TMP_DIR}/bin-default")"
if [ -d "${TMP_DIR}/bin-default" ]; then
    echo "ASSERTION FAILED: 默认调用不应创建 bindir" >&2
    exit 1
fi
if ! grep -Fq "不再默认安装" <<< "$default_output"; then
    echo "ASSERTION FAILED: 默认调用应说明 legacy 已不再默认安装" >&2
    exit 1
fi

dry_run_output="$("$INSTALLER" --with-legacy --dry-run --bindir "${TMP_DIR}/bin")"
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
if ! grep -Fq "DRY-RUN install payload progress.py" <<< "$dry_run_output"; then
    echo "ASSERTION FAILED: dry-run should list progress.py payload action" >&2
    exit 1
fi
if ! grep -Fq "DRY-RUN install payload probe_history.py" <<< "$dry_run_output"; then
    echo "ASSERTION FAILED: dry-run should list probe_history.py payload action" >&2
    exit 1
fi
if ! grep -Fq "DRY-RUN install payload import_subscription.py" <<< "$dry_run_output"; then
    echo "ASSERTION FAILED: dry-run should list import_subscription.py payload action" >&2
    exit 1
fi

"$INSTALLER" --with-legacy --bindir "${TMP_DIR}/bin" --libdir "${TMP_DIR}/lib/clash-proxy" >/dev/null

test -x "${TMP_DIR}/bin/clash-proxy"
test -x "${TMP_DIR}/bin/clash-proxy-update"
test -x "${TMP_DIR}/lib/clash-proxy/proxy.sh"
test -x "${TMP_DIR}/lib/clash-proxy/update_config.sh"
test -x "${TMP_DIR}/lib/clash-proxy/import_subscription.py"
test -x "${TMP_DIR}/lib/clash-proxy/probe_history.py"
test -x "${TMP_DIR}/lib/clash-proxy/progress.py"
test ! -e "${TMP_DIR}/bin/cproxy"
test -x "${TMP_DIR}/bin/cproxy-update"
assert_file_contains "${TMP_DIR}/bin/cproxy-update" '警告: cproxy-update 是已停用的 legacy 入口' "cproxy-update 应打印弃用警告"

assert_file_contains "${TMP_DIR}/bin/clash-proxy" 'WRAPPER_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"' "clash-proxy 应基于自身位置解析目标"
assert_file_contains "${TMP_DIR}/bin/clash-proxy" "DEFAULT_TMPDIR=\"${PROJECT_DIR}/.tmp\"" "clash-proxy 应默认使用项目专用临时目录"
assert_file_contains "${TMP_DIR}/bin/clash-proxy" 'if [ -z "${TMPDIR:-}" ]; then' "clash-proxy 应尊重调用者显式设置的 TMPDIR"
assert_file_contains "${TMP_DIR}/bin/clash-proxy" 'export TMPDIR' "clash-proxy 应导出默认临时目录"
assert_file_contains "${TMP_DIR}/bin/clash-proxy" 'exec "${WRAPPER_DIR}/' "clash-proxy 应使用相对目标"
assert_file_contains "${TMP_DIR}/bin/clash-proxy" 'CLASH_PROXY_CLI_NAME="clash-proxy"' "clash-proxy 应向帮助输出传递稳定命令名"
assert_file_contains "${TMP_DIR}/bin/clash-proxy" 'proxy.sh" "$@"' "clash-proxy 应转发到 root 生产入口"
assert_file_contains "${TMP_DIR}/bin/clash-proxy" '../lib/clash-proxy/proxy.sh' "clash-proxy 应指向安装后的 lib payload"
assert_file_contains "${TMP_DIR}/bin/clash-proxy-update" 'update_config.sh" "$@"' "clash-proxy-update 应转发到安全更新入口"
assert_file_contains "${TMP_DIR}/bin/clash-proxy-update" "PROJECT_DIR=\"${PROJECT_DIR}\"" "clash-proxy-update 应保留真实项目目录"

# 弃用警告：走 stderr，不污染 stdout（脚本消费 --raw 仍可解析）
assert_file_contains "${TMP_DIR}/bin/clash-proxy" '警告: clash-proxy 是已停用的 legacy 入口' "clash-proxy 应打印弃用警告"
assert_file_contains "${TMP_DIR}/bin/clash-proxy" '>&2' "弃用警告应走 stderr"
assert_file_contains "${TMP_DIR}/bin/clash-proxy-update" '改用 cproxy refresh' "clash-proxy-update 的警告应指向 cproxy refresh"

# 保护：已存在的非 legacy cproxy（如 pip 安装的）绝不能被覆盖——覆盖会让 cproxy 命令失效
printf '#!/bin/sh\necho real-cproxy\n' > "${TMP_DIR}/bin/cproxy"
chmod +x "${TMP_DIR}/bin/cproxy"
if "$INSTALLER" --with-legacy --bindir "${TMP_DIR}/bin" --libdir "${TMP_DIR}/lib/clash-proxy" --with-cproxy-alias >/dev/null 2>&1; then
    echo "ASSERTION FAILED: 覆盖已存在的非 legacy cproxy 应被拒绝" >&2
    exit 1
fi
assert_file_contains "${TMP_DIR}/bin/cproxy" 'echo real-cproxy' "拒绝覆盖时原 cproxy 应原封不动"
rm -f "${TMP_DIR}/bin/cproxy"

"$INSTALLER" --with-legacy --bindir "${TMP_DIR}/bin" --libdir "${TMP_DIR}/lib/clash-proxy" --with-cproxy-alias >/dev/null

test -x "${TMP_DIR}/bin/cproxy"
test -x "${TMP_DIR}/bin/cproxy-update"
assert_file_contains "${TMP_DIR}/bin/cproxy" 'proxy.sh" "$@"' "cproxy alias 应显式转发到 root 生产入口"
assert_file_contains "${TMP_DIR}/bin/cproxy-update" 'update_config.sh" "$@"' "cproxy-update alias 应显式转发到安全更新入口"

echo "system_command_installer_test: PASS"
