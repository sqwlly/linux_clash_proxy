#!/bin/bash

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REFRESH_SCRIPT="${PROJECT_DIR}/systemd/clash-proxy-refresh.sh"
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

    cat > "${tmp_dir}/proxy.sh" <<'EOF'
#!/bin/bash
set -euo pipefail

if [ "${1:-}" != "render" ]; then
    echo "unexpected proxy command: ${1:-}" >&2
    exit 64
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

run_refresh() {
    local tmp_dir="$1"

    PROJECT_DIR="$tmp_dir" \
    PROXY_SH="${tmp_dir}/proxy.sh" \
    RUNTIME_CONFIG="${tmp_dir}/runtime.yaml" \
    SYSTEMCTL="${tmp_dir}/systemctl" \
    MIHOMO_BIN="${tmp_dir}/mihomo" \
    CURL_BIN="${tmp_dir}/curl" \
    SERVICE_NAME="clash-proxy.service" \
    API_PROBE_ATTEMPTS=1 \
    API_PROBE_INTERVAL=0 \
    bash "$REFRESH_SCRIPT"
}

test_valid_runtime_restarts_and_probes_api() {
    local tmp_dir
    tmp_dir="$(mktemp -d)"
    TMP_DIRS+=("$tmp_dir")

    write_fake_tools "$tmp_dir"
    write_runtime "${tmp_dir}/runtime.yaml" "old"
    write_runtime "${tmp_dir}/next-runtime.yaml" "new"

    export NEXT_RUNTIME="${tmp_dir}/next-runtime.yaml"
    export SYSTEMCTL_LOG="${tmp_dir}/systemctl.log"
    export CURL_LOG="${tmp_dir}/curl.log"
    export PROXIES_JSON="${tmp_dir}/proxies.json"
    export ACTIVE_RC=0
    export ENABLED_RC=0
    export CURL_RC=0
    write_proxies_json "$PROXIES_JSON" AI-MANUAL AI-AUTO AI-US AI-SG

    run_refresh "$tmp_dir"

    assert_contains "${tmp_dir}/runtime.yaml" "marker: new" "有效 runtime 应替换旧 runtime"
    assert_contains "$SYSTEMCTL_LOG" "restart clash-proxy.service" "runtime 变化后应重启服务"
    assert_contains "$CURL_LOG" "http://127.0.0.1:9090/version" "重启后应探活 Mihomo API"
    assert_contains "$CURL_LOG" "http://127.0.0.1:9090/proxies" "重启后应校验 Mihomo proxy groups"
}

test_invalid_runtime_rolls_back_before_restart() {
    local tmp_dir
    tmp_dir="$(mktemp -d)"
    TMP_DIRS+=("$tmp_dir")

    write_fake_tools "$tmp_dir"
    write_runtime "${tmp_dir}/runtime.yaml" "old"
    write_runtime "${tmp_dir}/next-runtime.yaml" "bad" "true"

    export NEXT_RUNTIME="${tmp_dir}/next-runtime.yaml"
    export SYSTEMCTL_LOG="${tmp_dir}/systemctl.log"
    export CURL_LOG="${tmp_dir}/curl.log"
    export PROXIES_JSON="${tmp_dir}/proxies.json"
    export ACTIVE_RC=0
    export ENABLED_RC=0
    export CURL_RC=0
    write_proxies_json "$PROXIES_JSON" AI-MANUAL AI-AUTO AI-US AI-SG

    if run_refresh "$tmp_dir"; then
        echo "ASSERTION FAILED: invalid runtime should fail refresh" >&2
        exit 1
    fi

    assert_contains "${tmp_dir}/runtime.yaml" "marker: old" "校验失败时应恢复旧 runtime"
    assert_not_contains "$SYSTEMCTL_LOG" "restart clash-proxy.service" "校验失败时不应重启服务"
}

test_api_probe_failure_rolls_back_runtime() {
    local tmp_dir
    tmp_dir="$(mktemp -d)"
    TMP_DIRS+=("$tmp_dir")

    write_fake_tools "$tmp_dir"
    write_runtime "${tmp_dir}/runtime.yaml" "old"
    write_runtime "${tmp_dir}/next-runtime.yaml" "new"

    export NEXT_RUNTIME="${tmp_dir}/next-runtime.yaml"
    export SYSTEMCTL_LOG="${tmp_dir}/systemctl.log"
    export CURL_LOG="${tmp_dir}/curl.log"
    export PROXIES_JSON="${tmp_dir}/proxies.json"
    export ACTIVE_RC=0
    export ENABLED_RC=0
    export CURL_RC=1
    write_proxies_json "$PROXIES_JSON" AI-MANUAL AI-AUTO AI-US AI-SG

    if run_refresh "$tmp_dir"; then
        echo "ASSERTION FAILED: API probe failure should fail refresh" >&2
        exit 1
    fi

    assert_contains "${tmp_dir}/runtime.yaml" "marker: old" "API 探活失败时应恢复旧 runtime"
    assert_contains "$SYSTEMCTL_LOG" "restart clash-proxy.service" "API 探活失败前应尝试应用新 runtime"
}

test_missing_required_proxy_group_rolls_back_runtime() {
    local tmp_dir
    tmp_dir="$(mktemp -d)"
    TMP_DIRS+=("$tmp_dir")

    write_fake_tools "$tmp_dir"
    write_runtime "${tmp_dir}/runtime.yaml" "old"
    write_runtime "${tmp_dir}/next-runtime.yaml" "new"

    export NEXT_RUNTIME="${tmp_dir}/next-runtime.yaml"
    export SYSTEMCTL_LOG="${tmp_dir}/systemctl.log"
    export CURL_LOG="${tmp_dir}/curl.log"
    export PROXIES_JSON="${tmp_dir}/proxies.json"
    export ACTIVE_RC=0
    export ENABLED_RC=0
    export CURL_RC=0
    write_proxies_json "$PROXIES_JSON" AI-MANUAL AI-AUTO AI-US

    if run_refresh "$tmp_dir"; then
        echo "ASSERTION FAILED: missing proxy group should fail refresh" >&2
        exit 1
    fi

    assert_contains "${tmp_dir}/runtime.yaml" "marker: old" "缺少必要 proxy group 时应恢复旧 runtime"
    assert_contains "$SYSTEMCTL_LOG" "restart clash-proxy.service" "proxy group 探活失败前应尝试应用新 runtime"
    assert_contains "$CURL_LOG" "http://127.0.0.1:9090/proxies" "应读取 /proxies 校验必要 proxy groups"
}

test_valid_runtime_restarts_and_probes_api
test_invalid_runtime_rolls_back_before_restart
test_api_probe_failure_rolls_back_runtime
test_missing_required_proxy_group_rolls_back_runtime

echo "systemd_refresh_safety_test: PASS"
