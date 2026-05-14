#!/bin/bash

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPT="${PROJECT_DIR}/update_config.sh"
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

    if ! rg -Fq "$needle" "$file"; then
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

    if [ -f "$file" ] && rg -Fq "$needle" "$file"; then
        echo "ASSERTION FAILED: $message" >&2
        echo "Unexpected content: $needle" >&2
        echo "In file: $file" >&2
        exit 1
    fi
}

assert_file_equals() {
    local left="$1"
    local right="$2"
    local message="$3"

    if ! cmp -s "$left" "$right"; then
        echo "ASSERTION FAILED: $message" >&2
        diff -u "$left" "$right" >&2 || true
        exit 1
    fi
}

write_config() {
    local path="$1"
    local marker="$2"
    local refresh_invalid="${3:-false}"

    cat > "$path" <<EOF
mixed-port: 7890
external-controller: 127.0.0.1:9090
secret: ''
marker: ${marker}
refresh-invalid: ${refresh_invalid}
proxies:
  - name: node-a
    type: ss
    server: example.invalid
    port: 443
    cipher: aes-256-gcm
    password: password
proxy-groups:
  - name: SSRDOG
    type: select
    proxies:
      - node-a
rules:
  - MATCH,SSRDOG
EOF
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

cp "$SOURCE_CONFIG_FILE" "$RUNTIME_CONFIG"
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

if rg -Fq "refresh-invalid: true" "$config"; then
    exit 1
fi

exit 0
EOF
    chmod +x "${tmp_dir}/mihomo"

    cat > "${tmp_dir}/refresh.sh" <<'EOF'
#!/bin/bash
set -euo pipefail

echo "refresh source=${SOURCE_CONFIG_FILE:-} config=${CONFIG_FILE:-}" >> "$REFRESH_LOG"
"$PROXY_SH" render
"$MIHOMO_BIN" -t -f "$RUNTIME_CONFIG" -d "$PROJECT_DIR"
EOF
    chmod +x "${tmp_dir}/refresh.sh"
}

run_update_config() {
    local tmp_dir="$1"
    shift

    PROJECT_DIR="$tmp_dir" \
    CONFIG_FILE="${tmp_dir}/config.yaml" \
    CANDIDATE_FILE="${tmp_dir}/config_1.yaml" \
    REFRESH_SCRIPT="${tmp_dir}/refresh.sh" \
    PROXY_SH="${tmp_dir}/proxy.sh" \
    RUNTIME_CONFIG="${tmp_dir}/runtime.yaml" \
    MIHOMO_BIN="${tmp_dir}/mihomo" \
    REFRESH_LOG="${tmp_dir}/refresh.log" \
    bash "$SCRIPT" "$@"
}

test_default_dry_run_does_not_write_or_refresh() {
    local tmp_dir
    tmp_dir="$(mktemp -d)"
    TMP_DIRS+=("$tmp_dir")

    write_fake_tools "$tmp_dir"
    write_config "${tmp_dir}/config.yaml" "old"
    cp "${tmp_dir}/config.yaml" "${tmp_dir}/before.yaml"
    write_config "${tmp_dir}/config_1.yaml" "new"

    run_update_config "$tmp_dir" >/dev/null

    assert_file_equals "${tmp_dir}/before.yaml" "${tmp_dir}/config.yaml" "默认 dry-run 不应修改源配置"
    assert_not_contains "${tmp_dir}/config.yaml" "AI-MANUAL" "update_config 不应注入 AI 代理组或规则"

    if [ -f "${tmp_dir}/refresh.log" ]; then
        echo "ASSERTION FAILED: dry-run 不应调用 refresh" >&2
        exit 1
    fi
}

test_apply_writes_candidate_and_refreshes() {
    local tmp_dir
    tmp_dir="$(mktemp -d)"
    TMP_DIRS+=("$tmp_dir")

    write_fake_tools "$tmp_dir"
    write_config "${tmp_dir}/config.yaml" "old"
    write_config "${tmp_dir}/config_1.yaml" "new"

    run_update_config "$tmp_dir" --apply >/dev/null

    assert_contains "${tmp_dir}/config.yaml" "marker: new" "apply 应写入候选源配置"
    assert_contains "${tmp_dir}/runtime.yaml" "marker: new" "apply 应通过 refresh 生成运行配置"
    assert_contains "${tmp_dir}/refresh.log" "source=${tmp_dir}/config.yaml" "apply 应把源配置传给 refresh"
    assert_not_contains "${tmp_dir}/config.yaml" "AI-MANUAL" "update_config 不应注入 AI 配置"
}

test_invalid_yaml_fails_before_refresh() {
    local tmp_dir
    tmp_dir="$(mktemp -d)"
    TMP_DIRS+=("$tmp_dir")

    write_fake_tools "$tmp_dir"
    write_config "${tmp_dir}/config.yaml" "old"
    cp "${tmp_dir}/config.yaml" "${tmp_dir}/before.yaml"
    printf 'proxies: [\n' > "${tmp_dir}/config_1.yaml"

    if run_update_config "$tmp_dir" --apply >/dev/null 2>&1; then
        echo "ASSERTION FAILED: invalid YAML should fail" >&2
        exit 1
    fi

    assert_file_equals "${tmp_dir}/before.yaml" "${tmp_dir}/config.yaml" "YAML 失败不应修改源配置"

    if [ -f "${tmp_dir}/refresh.log" ]; then
        echo "ASSERTION FAILED: YAML 失败不应调用 refresh" >&2
        exit 1
    fi
}

test_refresh_failure_restores_previous_config() {
    local tmp_dir
    tmp_dir="$(mktemp -d)"
    TMP_DIRS+=("$tmp_dir")

    write_fake_tools "$tmp_dir"
    write_config "${tmp_dir}/config.yaml" "old"
    cp "${tmp_dir}/config.yaml" "${tmp_dir}/before.yaml"
    write_config "${tmp_dir}/config_1.yaml" "bad" "true"

    if run_update_config "$tmp_dir" --apply >/dev/null 2>&1; then
        echo "ASSERTION FAILED: refresh failure should fail apply" >&2
        exit 1
    fi

    assert_file_equals "${tmp_dir}/before.yaml" "${tmp_dir}/config.yaml" "refresh 失败应恢复旧源配置"
    assert_contains "${tmp_dir}/refresh.log" "source=${tmp_dir}/config.yaml" "refresh 失败前应尝试安全刷新"
}

test_default_dry_run_does_not_write_or_refresh
test_apply_writes_candidate_and_refreshes
test_invalid_yaml_fails_before_refresh
test_refresh_failure_restores_previous_config

echo "update_config_safety_test: PASS"
