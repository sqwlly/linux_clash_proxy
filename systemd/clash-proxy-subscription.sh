#!/bin/bash
# 每日订阅更新：拉订阅合并进 config.yaml → render → mihomo -t 校验 →
# 仅在配置变化时 restart clash-proxy.service → API 探活，失败回滚 config.yaml 与 runtime.yaml。
#
# 与 clash-proxy-refresh.sh 的分工：
#   - refresh.sh：每 15 分钟，仅 render 对账（不拉订阅）
#   - subscription.sh：每天一次，拉订阅并触发受控 restart

set -euo pipefail

PROJECT_DIR="${PROJECT_DIR:-/root/clash_proxy}"
PROXY_SH="${PROXY_SH:-${PROJECT_DIR}/proxy.sh}"
SOURCE_CONFIG="${SOURCE_CONFIG:-${PROJECT_DIR}/config.yaml}"
RUNTIME_CONFIG="${RUNTIME_CONFIG:-${PROJECT_DIR}/runtime.yaml}"
SERVICE_NAME="${SERVICE_NAME:-clash-proxy.service}"
SYSTEMCTL="${SYSTEMCTL:-systemctl}"
PYTHON_BIN="${PYTHON_BIN:-/usr/local/bin/python}"
UPDATE_SUBSCRIPTION_PY="${UPDATE_SUBSCRIPTION_PY:-${PROJECT_DIR}/scripts/update_subscription_prod.py}"
MIHOMO_BIN="${MIHOMO_BIN:-${PROG_PATH:-/usr/local/bin/mihomo}}"
CURL_BIN="${CURL_BIN:-curl}"
API_PROBE_ATTEMPTS="${API_PROBE_ATTEMPTS:-10}"
API_PROBE_INTERVAL="${API_PROBE_INTERVAL:-1}"
REQUIRED_PROXY_GROUPS=(AI-MANUAL AI-AUTO AI-US AI-SG)

source_backup=""
runtime_backup=""
had_runtime=0

log_stage() {
    printf '{"event":"clash_proxy_subscription","stage":"%s"}\n' "$1"
}

file_hash() {
    local file="$1"
    if [ ! -f "$file" ]; then
        echo ""
        return 0
    fi
    sha256sum "$file" | awk '{print $1}'
}

backup_files() {
    if [ -f "$SOURCE_CONFIG" ]; then
        source_backup="$(mktemp "${SOURCE_CONFIG}.bak.XXXXXX")"
        cp "$SOURCE_CONFIG" "$source_backup"
    fi
    if [ -f "$RUNTIME_CONFIG" ]; then
        runtime_backup="$(mktemp "${RUNTIME_CONFIG}.bak.XXXXXX")"
        cp "$RUNTIME_CONFIG" "$runtime_backup"
        had_runtime=1
    fi
}

restore_files() {
    if [ -n "$source_backup" ] && [ -f "$source_backup" ]; then
        cp "$source_backup" "$SOURCE_CONFIG"
    fi
    if [ "$had_runtime" -eq 1 ] && [ -n "$runtime_backup" ] && [ -f "$runtime_backup" ]; then
        cp "$runtime_backup" "$RUNTIME_CONFIG"
    elif [ "$had_runtime" -eq 0 ]; then
        rm -f "$RUNTIME_CONFIG"
    fi
}

cleanup() {
    [ -n "$source_backup" ] && rm -f "$source_backup"
    [ -n "$runtime_backup" ] && rm -f "$runtime_backup"
}
trap cleanup EXIT

validate_runtime() {
    "$MIHOMO_BIN" -t -f "$RUNTIME_CONFIG" -d "$PROJECT_DIR" >/dev/null
}

# 输出 runtime.yaml 的规则计数 JSON（total/direct/reject/proxy/ai_manual），
# 用于订阅更新前后对比，方便从 journal 直接看出订阅是否改动了路由规则。
rule_counts() {
    local file="$1"
    [ -f "$file" ] || { echo '{}'; return 0; }
    "$PYTHON_BIN" - "$file" <<'PY'
import json
import sys

try:
    import yaml
    with open(sys.argv[1], encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    rules = [str(r) for r in (data.get("rules") or [])]
    counts = {
        "total": len(rules),
        "direct": sum(1 for r in rules if ",DIRECT" in r.upper()),
        "reject": sum(1 for r in rules if ",REJECT" in r.upper()),
        "proxy": sum(1 for r in rules if ",CYBERGUARD" in r.upper()),
        "ai_manual": sum(1 for r in rules if ",AI-MANUAL" in r.upper()),
    }
except Exception:
    counts = {}
print(json.dumps(counts, sort_keys=True))
PY
}

api_settings() {
    "$PYTHON_BIN" - "$RUNTIME_CONFIG" <<'PY'
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
        | "$PYTHON_BIN" -c '
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

check_no_competing_instance() {
    # 用户级 cproxy.service 会与生产服务抢占相同端口，导致 API 探活误判
    if systemctl --user is-active --quiet cproxy.service 2>/dev/null; then
        echo "错误: 用户级 cproxy.service 正在运行，会与 $SERVICE_NAME 抢占端口" >&2
        echo "提示: 先执行 systemctl --user stop cproxy.service 再重试" >&2
        exit 1
    fi
}

check_no_competing_instance

backup_files
before_source="$(file_hash "$SOURCE_CONFIG")"
before_runtime="$(file_hash "$RUNTIME_CONFIG")"

log_stage "subscription_started"
set +e
"$PYTHON_BIN" "$UPDATE_SUBSCRIPTION_PY"
update_rc=$?
set -e

case "$update_rc" in
    0)
        log_stage "subscription_updated"
        ;;
    3)
        log_stage "subscription_skipped"
        exit 0
        ;;
    *)
        log_stage "subscription_failed"
        echo "错误: 订阅拉取失败，保留现有配置" >&2
        exit 1
        ;;
esac

after_source="$(file_hash "$SOURCE_CONFIG")"
if [ "$before_source" = "$after_source" ]; then
    log_stage "config_unchanged"
    exit 0
fi

rules_before="$(rule_counts "$RUNTIME_CONFIG")"

log_stage "render_started"
if ! "$PROXY_SH" render >/dev/null; then
    log_stage "rollback_started"
    restore_files
    log_stage "rollback_done"
    echo "错误: render 失败，已回滚 config.yaml 与 runtime.yaml" >&2
    exit 1
fi

after_runtime="$(file_hash "$RUNTIME_CONFIG")"
if [ "$before_runtime" = "$after_runtime" ]; then
    log_stage "runtime_unchanged"
    exit 0
fi
log_stage "runtime_changed"

rules_after="$(rule_counts "$RUNTIME_CONFIG")"
printf '{"event":"clash_proxy_subscription","stage":"rules_delta","before":%s,"after":%s}\n' \
    "$rules_before" "$rules_after"

if ! validate_runtime; then
    log_stage "rollback_started"
    restore_files
    log_stage "rollback_done"
    echo "错误: 新运行配置未通过 mihomo 配置校验，已回滚" >&2
    exit 1
fi
log_stage "mihomo_test_ok"

if "$SYSTEMCTL" is-active --quiet "$SERVICE_NAME"; then
    log_stage "restart_started"
    if ! "$SYSTEMCTL" restart "$SERVICE_NAME" || ! wait_for_api; then
        log_stage "rollback_started"
        restore_files
        "$SYSTEMCTL" restart "$SERVICE_NAME" >/dev/null 2>&1 || true
        log_stage "rollback_done"
        echo "错误: 服务刷新后 API 不可用，已回滚并尝试恢复服务" >&2
        exit 1
    fi
    log_stage "api_probe_ok"
elif "$SYSTEMCTL" is-enabled --quiet "$SERVICE_NAME"; then
    log_stage "start_started"
    if ! "$SYSTEMCTL" start "$SERVICE_NAME" || ! wait_for_api; then
        log_stage "rollback_started"
        restore_files
        log_stage "rollback_done"
        echo "错误: 服务启动后 API 不可用，已回滚" >&2
        exit 1
    fi
    log_stage "api_probe_ok"
fi

log_stage "done"
