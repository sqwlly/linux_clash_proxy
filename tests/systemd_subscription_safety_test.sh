#!/bin/bash

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SUBSCRIPTION_SCRIPT="${PROJECT_DIR}/systemd/clash-proxy-subscription.sh"
TMP_DIRS=()

cleanup_tmp_dirs() {
    local tmp_dir

    for tmp_dir in "${TMP_DIRS[@]}"; do
        rm -rf "$tmp_dir"
    done
}
trap cleanup_tmp_dirs EXIT

assert_contains() {
    local file="$1"
    local needle="$2"
    local message="$3"

    if ! grep -Fq "$needle" "$file"; then
        echo "ASSERTION FAILED: $message" >&2
        echo "Expected to find: $needle" >&2
        echo "In file: $file" >&2
        exit 1
    fi
}

assert_not_contains() {
    local file="$1"
    local needle="$2"
    local message="$3"

    if [ -f "$file" ] && grep -Fq "$needle" "$file"; then
        echo "ASSERTION FAILED: $message" >&2
        echo "Unexpected content: $needle" >&2
        echo "In file: $file" >&2
        exit 1
    fi
}

write_fake_tools() {
    local tmp_dir="$1"

    # 假 python：按调用形式分发
    #   <python> <update_script>            -> 模拟订阅更新（rc 由 FAKE_UPDATE_RC 控制）
    #   <python> - <file> <<heredoc         -> 按 heredoc 内容识别 rule_counts / api_settings
    #   <python> -c <code> <groups...>      -> 校验 /proxies JSON 是否包含必要组
    cat > "${tmp_dir}/python" <<'EOF'
#!/bin/bash
set -euo pipefail

first="${1:-}"

if [ -n "${UPDATE_SUBSCRIPTION_PY:-}" ] && [ "$first" = "${UPDATE_SUBSCRIPTION_PY}" ]; then
    rc="${FAKE_UPDATE_RC:-0}"
    if [ "$rc" -eq 3 ]; then
        echo "跳过: 生产配置未设置 subscription-url"
        exit 3
    fi
    if [ "$rc" -ne 0 ]; then
        echo "错误: 订阅更新失败" >&2
        exit "$rc"
    fi
    if [ "${FAKE_UPDATE_CHANGES_CONFIG:-1}" = "1" ]; then
        printf 'subscription-url: https://example.com/sub\n' >> "$SOURCE_CONFIG"
    fi
    echo "订阅已更新: https://example.com/sub -> $SOURCE_CONFIG"
    exit 0
fi

if [ "$first" = "-c" ]; then
    data="$(cat)"
    case "$data" in
        *'"proxies"'*)
            for group in "${@:2}"; do
                if ! printf '%s' "$data" | grep -Fq "\"$group\""; then
                    echo "missing required proxy groups: $group" >&2
                    exit 1
                fi
            done
            exit 0
            ;;
        *)
            exit 0
            ;;
    esac
fi

if [ "$first" = "-" ]; then
    script="$(cat)"
    file="${2:-}"
    case "$script" in
        *'"total"'*)
            if [ -n "$file" ] && [ -f "$file" ]; then
                printf '{"total":%s}\n' "$(grep -c 'marker' "$file" || echo 0)"
            else
                echo '{}'
            fi
            ;;
        *)
            echo "http://127.0.0.1:9090"
            echo ""
            ;;
    esac
    exit 0
fi

echo "fake python: unexpected invocation: $*" >&2
exit 64
EOF
    chmod +x "${tmp_dir}/python"

    cat > "${tmp_dir}/proxy.sh" <<'EOF'
#!/bin/bash
set -euo pipefail

if [ "${1:-}" != "render" ]; then
    echo "unexpected proxy command: ${1:-}" >&2
    exit 64
fi

if [ "${RENDER_RC:-0}" -ne 0 ]; then
    echo "render failed" >&2
    exit "${RENDER_RC}"
fi

cp "$NEXT_RUNTIME" "$RUNTIME_CONFIG"
EOF
    chmod +x "${tmp_dir}/proxy.sh"

    cat > "${tmp_dir}/mihomo" <<'EOF'
#!/bin/bash
set -euo pipefail

config=""
while [ "$#" -gt 0 ]; do
    case "$1" in
        -f)
            config="$2"
            shift 2
            ;;
        *)
            shift
            ;;
    esac
done

if grep -Fq "mihomo-invalid: true" "$config"; then
    exit 1
fi

exit 0
EOF
    chmod +x "${tmp_dir}/mihomo"

    cat > "${tmp_dir}/systemctl" <<'EOF'
#!/bin/bash
set -euo pipefail

case "${1:-}" in
    is-active)
        exit "${ACTIVE_RC:-0}"
        ;;
    is-enabled)
        exit "${ENABLED_RC:-0}"
        ;;
    restart)
        echo "restart $2" >> "$SYSTEMCTL_LOG"
        exit "${RESTART_RC:-0}"
        ;;
    start)
        echo "start $2" >> "$SYSTEMCTL_LOG"
        exit "${START_RC:-0}"
        ;;
    *)
        echo "unexpected systemctl command: $*" >&2
        exit 64
        ;;
esac
EOF
    chmod +x "${tmp_dir}/systemctl"

    cat > "${tmp_dir}/curl" <<'EOF'
#!/bin/bash
set -euo pipefail

echo "$*" >> "$CURL_LOG"

case "${*: -1}" in
    */version)
        ;;
    */proxies)
        cat "$PROXIES_JSON"
        ;;
    *)
        echo "unexpected curl URL: ${*: -1}" >&2
        exit 64
        ;;
esac

exit "${CURL_RC:-0}"
EOF
    chmod +x "${tmp_dir}/curl"
}

write_proxies_json() {
    local path="$1"
    shift

    {
        printf '{"proxies":{'
        local first=1
        local group
        for group in "$@"; do
            if [ "$first" -eq 0 ]; then
                printf ','
            fi
            printf '"%s":{}' "$group"
            first=0
        done
        printf '}}\n'
    } > "$path"
}

write_source_config() {
    local path="$1"

    cat > "$path" <<'EOF'
mixed-port: 7890
original-marker: yes
EOF
}

write_runtime() {
    local path="$1"
    local marker="$2"
    local invalid="${3:-false}"

    cat > "$path" <<EOF
mixed-port: 7890
external-controller: 127.0.0.1:9090
secret: ''
marker: ${marker}
mihomo-invalid: ${invalid}
EOF
}

run_subscription() {
    local tmp_dir="$1"

    PROJECT_DIR="$tmp_dir" \
    PROXY_SH="${tmp_dir}/proxy.sh" \
    SOURCE_CONFIG="${tmp_dir}/config.yaml" \
    RUNTIME_CONFIG="${tmp_dir}/runtime.yaml" \
    PYTHON_BIN="${tmp_dir}/python" \
    UPDATE_SUBSCRIPTION_PY="${tmp_dir}/update_subscription_prod.py" \
    SYSTEMCTL="${tmp_dir}/systemctl" \
    MIHOMO_BIN="${tmp_dir}/mihomo" \
    CURL_BIN="${tmp_dir}/curl" \
    SERVICE_NAME="clash-proxy.service" \
    API_PROBE_ATTEMPTS=1 \
    API_PROBE_INTERVAL=0 \
    bash "$SUBSCRIPTION_SCRIPT"
}

setup_common_env() {
    local tmp_dir="$1"

    write_fake_tools "$tmp_dir"
    write_source_config "${tmp_dir}/config.yaml"
    write_runtime "${tmp_dir}/runtime.yaml" "old"
    write_runtime "${tmp_dir}/next-runtime.yaml" "new"

    touch "${tmp_dir}/update_subscription_prod.py"

    export NEXT_RUNTIME="${tmp_dir}/next-runtime.yaml"
    export SYSTEMCTL_LOG="${tmp_dir}/systemctl.log"
    export CURL_LOG="${tmp_dir}/curl.log"
    export PROXIES_JSON="${tmp_dir}/proxies.json"
    export ACTIVE_RC=0
    export ENABLED_RC=0
    export CURL_RC=0
    export RENDER_RC=0
    export FAKE_UPDATE_RC=0
    export FAKE_UPDATE_CHANGES_CONFIG=1
    write_proxies_json "$PROXIES_JSON" AI-MANUAL AI-AUTO AI-US AI-SG
}

test_subscription_update_restarts_and_probes_api() {
    local tmp_dir
    tmp_dir="$(mktemp -d)"
    TMP_DIRS+=("$tmp_dir")

    setup_common_env "$tmp_dir"

    run_subscription "$tmp_dir"

    assert_contains "${tmp_dir}/config.yaml" "subscription-url" "订阅更新后应写入 subscription-url"
    assert_contains "${tmp_dir}/runtime.yaml" "marker: new" "有效 runtime 应替换旧 runtime"
    assert_contains "$SYSTEMCTL_LOG" "restart clash-proxy.service" "配置变化后应重启服务"
    assert_contains "$CURL_LOG" "http://127.0.0.1:9090/version" "重启后应探活 Mihomo API"
    assert_contains "$CURL_LOG" "http://127.0.0.1:9090/proxies" "重启后应校验 Mihomo proxy groups"
}

test_no_subscription_url_skips_without_restart() {
    local tmp_dir
    tmp_dir="$(mktemp -d)"
    TMP_DIRS+=("$tmp_dir")

    setup_common_env "$tmp_dir"
    export FAKE_UPDATE_RC=3

    run_subscription "$tmp_dir"

    assert_not_contains "${tmp_dir}/config.yaml" "subscription-url" "未配置订阅时不应改动 config.yaml"
    assert_contains "${tmp_dir}/runtime.yaml" "marker: old" "未配置订阅时不应改动 runtime.yaml"
    if [ -f "$SYSTEMCTL_LOG" ]; then
        assert_not_contains "$SYSTEMCTL_LOG" "restart clash-proxy.service" "未配置订阅时不应重启服务"
    fi
}

test_config_unchanged_skips_render_and_restart() {
    local tmp_dir
    tmp_dir="$(mktemp -d)"
    TMP_DIRS+=("$tmp_dir")

    setup_common_env "$tmp_dir"
    export FAKE_UPDATE_CHANGES_CONFIG=0

    run_subscription "$tmp_dir"

    assert_contains "${tmp_dir}/runtime.yaml" "marker: old" "源配置未变化时不应改动 runtime.yaml"
    if [ -f "$SYSTEMCTL_LOG" ]; then
        assert_not_contains "$SYSTEMCTL_LOG" "restart clash-proxy.service" "源配置未变化时不应重启服务"
    fi
}

test_render_failure_rolls_back_source_config() {
    local tmp_dir
    tmp_dir="$(mktemp -d)"
    TMP_DIRS+=("$tmp_dir")

    setup_common_env "$tmp_dir"
    export RENDER_RC=1

    if run_subscription "$tmp_dir"; then
        echo "ASSERTION FAILED: render failure should fail subscription refresh" >&2
        exit 1
    fi

    assert_contains "${tmp_dir}/config.yaml" "original-marker: yes" "render 失败时应恢复原 config.yaml"
    assert_not_contains "${tmp_dir}/config.yaml" "subscription-url" "render 失败时不应保留订阅写入"
    assert_contains "${tmp_dir}/runtime.yaml" "marker: old" "render 失败时应保留旧 runtime"
    if [ -f "$SYSTEMCTL_LOG" ]; then
        assert_not_contains "$SYSTEMCTL_LOG" "restart clash-proxy.service" "render 失败时不应重启服务"
    fi
}

test_invalid_runtime_rolls_back_before_restart() {
    local tmp_dir
    tmp_dir="$(mktemp -d)"
    TMP_DIRS+=("$tmp_dir")

    setup_common_env "$tmp_dir"
    write_runtime "${tmp_dir}/next-runtime.yaml" "bad" "true"

    if run_subscription "$tmp_dir"; then
        echo "ASSERTION FAILED: invalid runtime should fail subscription refresh" >&2
        exit 1
    fi

    assert_contains "${tmp_dir}/config.yaml" "original-marker: yes" "校验失败时应恢复原 config.yaml"
    assert_contains "${tmp_dir}/runtime.yaml" "marker: old" "校验失败时应恢复旧 runtime"
    if [ -f "$SYSTEMCTL_LOG" ]; then
        assert_not_contains "$SYSTEMCTL_LOG" "restart clash-proxy.service" "校验失败时不应重启服务"
    fi
}

test_api_probe_failure_rolls_back_config_and_runtime() {
    local tmp_dir
    tmp_dir="$(mktemp -d)"
    TMP_DIRS+=("$tmp_dir")

    setup_common_env "$tmp_dir"
    export CURL_RC=1

    if run_subscription "$tmp_dir"; then
        echo "ASSERTION FAILED: API probe failure should fail subscription refresh" >&2
        exit 1
    fi

    assert_contains "${tmp_dir}/config.yaml" "original-marker: yes" "API 探活失败时应恢复原 config.yaml"
    assert_contains "${tmp_dir}/runtime.yaml" "marker: old" "API 探活失败时应恢复旧 runtime"
    assert_contains "$SYSTEMCTL_LOG" "restart clash-proxy.service" "API 探活失败前应尝试应用新配置"
}

test_subscription_update_restarts_and_probes_api
test_no_subscription_url_skips_without_restart
test_config_unchanged_skips_render_and_restart
test_render_failure_rolls_back_source_config
test_invalid_runtime_rolls_back_before_restart
test_api_probe_failure_rolls_back_config_and_runtime

echo "systemd_subscription_safety_test: PASS"
