#!/bin/bash
# 根据原始配置变更自动 render，并只在新运行配置可启动时重启服务

set -euo pipefail

PROJECT_DIR="${PROJECT_DIR:-/root/clash_proxy}"
PROXY_SH="${PROXY_SH:-${PROJECT_DIR}/proxy.sh}"
RUNTIME_CONFIG="${RUNTIME_CONFIG:-${PROJECT_DIR}/runtime.yaml}"
SERVICE_NAME="${SERVICE_NAME:-clash-proxy.service}"
SYSTEMCTL="${SYSTEMCTL:-systemctl}"
MIHOMO_BIN="${MIHOMO_BIN:-${PROG_PATH:-/usr/local/bin/mihomo}}"
CURL_BIN="${CURL_BIN:-curl}"
API_PROBE_ATTEMPTS="${API_PROBE_ATTEMPTS:-10}"
API_PROBE_INTERVAL="${API_PROBE_INTERVAL:-1}"
REQUIRED_PROXY_GROUPS=(AI-MANUAL AI-AUTO AI-US AI-SG)

before_hash=""
after_hash=""
backup_runtime=""
had_runtime=0

log_stage() {
    local stage="$1"

    printf '{"event":"clash_proxy_refresh","stage":"%s"}\n' "$stage"
}

runtime_hash() {
    local file="$1"

    if [ ! -f "$file" ]; then
        echo ""
        return 0
    fi

    sha256sum "$file" | awk '{print $1}'
}

backup_current_runtime() {
    if [ ! -f "$RUNTIME_CONFIG" ]; then
        return 0
    fi

    backup_runtime="$(mktemp "${RUNTIME_CONFIG}.bak.XXXXXX")"
    cp "$RUNTIME_CONFIG" "$backup_runtime"
    had_runtime=1
}

restore_runtime() {
    if [ "$had_runtime" -eq 1 ] && [ -n "$backup_runtime" ] && [ -f "$backup_runtime" ]; then
        cp "$backup_runtime" "$RUNTIME_CONFIG"
    elif [ "$had_runtime" -eq 0 ]; then
        rm -f "$RUNTIME_CONFIG"
    fi
}

rollback_runtime() {
    log_stage "rollback_started"
    restore_runtime
    log_stage "rollback_done"
}

cleanup() {
    if [ -n "$backup_runtime" ]; then
        rm -f "$backup_runtime"
    fi
}
trap cleanup EXIT

validate_runtime() {
    "$MIHOMO_BIN" -t -f "$RUNTIME_CONFIG" -d "$PROJECT_DIR" >/dev/null
}

api_settings() {
    python3 - "$RUNTIME_CONFIG" <<'PY'
import sys
import yaml

config_path = sys.argv[1]
try:
    with open(config_path, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
except Exception:
    data = {}

controller = str(data.get("external-controller") or "127.0.0.1:9090")
if not controller.startswith(("http://", "https://")):
    controller = f"http://{controller}"

secret = str(data.get("secret") or "")
print(controller.rstrip("/"))
print(secret)
PY
}

api_available() {
    local controller
    local secret
    local curl_args=(-fsS --connect-timeout 2)

    mapfile -t settings < <(api_settings)
    controller="${settings[0]:-http://127.0.0.1:9090}"
    secret="${settings[1]:-}"

    if [ -n "$secret" ]; then
        curl_args+=(-H "Authorization: Bearer $secret")
    fi

    "$CURL_BIN" "${curl_args[@]}" "${controller}/version" >/dev/null
    "$CURL_BIN" "${curl_args[@]}" "${controller}/proxies" \
        | python3 -c '
import json
import sys

required = sys.argv[1:]
try:
    data = json.load(sys.stdin)
except Exception:
    sys.exit(1)

proxies = data.get("proxies") if isinstance(data, dict) else None
if not isinstance(proxies, dict):
    sys.exit(1)

missing = [name for name in required if name not in proxies]
if missing:
    print("missing required proxy groups: " + ",".join(missing), file=sys.stderr)
    sys.exit(1)
' "${REQUIRED_PROXY_GROUPS[@]}" >/dev/null
}

wait_for_api() {
    local attempt=0

    while [ "$attempt" -lt "$API_PROBE_ATTEMPTS" ]; do
        if api_available; then
            return 0
        fi
        attempt=$((attempt + 1))
        sleep "$API_PROBE_INTERVAL"
    done

    return 1
}

restart_service() {
    "$SYSTEMCTL" restart "$SERVICE_NAME"
}

start_service() {
    "$SYSTEMCTL" start "$SERVICE_NAME"
}

backup_current_runtime
before_hash="$(runtime_hash "$RUNTIME_CONFIG")"

log_stage "render_started"
"$PROXY_SH" render >/dev/null

after_hash="$(runtime_hash "$RUNTIME_CONFIG")"

if [ "$before_hash" = "$after_hash" ]; then
    exit 0
fi
log_stage "runtime_changed"

if ! validate_runtime; then
    rollback_runtime
    echo "错误: 新运行配置未通过 mihomo 配置校验，已保留上一份可用 runtime" >&2
    exit 1
fi
log_stage "mihomo_test_ok"

if "$SYSTEMCTL" is-active --quiet "$SERVICE_NAME"; then
    log_stage "restart_started"
    if ! restart_service || ! wait_for_api; then
        log_stage "rollback_started"
        restore_runtime
        restart_service >/dev/null 2>&1 || true
        log_stage "rollback_done"
        echo "错误: 服务刷新后 API 不可用，已回滚 runtime 并尝试恢复服务" >&2
        exit 1
    fi
    log_stage "api_probe_ok"
elif "$SYSTEMCTL" is-enabled --quiet "$SERVICE_NAME"; then
    log_stage "start_started"
    if ! start_service || ! wait_for_api; then
        rollback_runtime
        echo "错误: 服务启动后 API 不可用，已回滚 runtime" >&2
        exit 1
    fi
    log_stage "api_probe_ok"
fi
