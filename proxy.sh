#!/bin/bash
# Clash/Mihomo 代理管理脚本
# 版本: 1.2.0
# 更新: 2026-04-09

set -o pipefail

# ==================== 配置区 ====================
# 兼容旧版 CONFIG_FILE，将其视为原始配置输入
PROG_NAME="${PROG_NAME:-mihomo}"
PROG_PATH="${PROG_PATH:-/usr/local/bin/mihomo}"
CONFIG_DIR="${CONFIG_DIR:-/root/clash_proxy}"
SOURCE_CONFIG_FILE="${SOURCE_CONFIG_FILE:-${CONFIG_FILE:-$CONFIG_DIR/config.yaml}}"
RUNTIME_CONFIG_FILE="${RUNTIME_CONFIG_FILE:-$CONFIG_DIR/runtime.yaml}"
PID_FILE="${PID_FILE:-$CONFIG_DIR/mihomo.pid}"
LOG_FILE="${LOG_FILE:-${XDG_STATE_HOME:-$HOME/.local/state}/clash_proxy/clash.log}"
DEFAULT_PROXY_PORT="${DEFAULT_PROXY_PORT:-7890}"
DEFAULT_CONTROLLER_ADDR="${DEFAULT_CONTROLLER_ADDR:-127.0.0.1:9090}"
START_TIMEOUT="${START_TIMEOUT:-5}"
LOCK_FILE="${CONFIG_DIR}/.lock"
TEST_URL="${TEST_URL:-https://cp.cloudflare.com/generate_204}"
TEST_TIMEOUT="${TEST_TIMEOUT:-5000}"
PROXY_NO_PROXY_DEFAULT="${PROXY_NO_PROXY_DEFAULT:-127.0.0.1,localhost}"

# ==================== AI 规则配置 ====================
AI_MANUAL_GROUP="AI-MANUAL"
AI_AUTO_GROUP="AI-AUTO"
AI_US_GROUP="AI-US"
AI_SG_GROUP="AI-SG"
AI_REGION_US="🇺🇸 United States"
AI_REGION_SG="🇸🇬 Singapore"

# ==================== 颜色定义 ====================
if [ -t 1 ] || [ "${FORCE_COLOR:-0}" = "1" ]; then
    BOLD='\033[1m'
    RED='\033[0;31m'
    GREEN='\033[0;32m'
    YELLOW='\033[1;33m'
    BLUE='\033[0;34m'
    CYAN='\033[0;36m'
    NC='\033[0m'
else
    BOLD=''
    RED=''
    GREEN=''
    YELLOW=''
    BLUE=''
    CYAN=''
    NC=''
fi

# ==================== 全局变量 ====================
CACHED_PID=""

# ==================== 信号处理 ====================
cleanup() {
    rm -f "$LOCK_FILE" 2>/dev/null
    exit 0
}
trap cleanup EXIT INT TERM

# ==================== 工具函数 ====================

print_error() {
    echo -e "${RED}$*${NC}" >&2
}

print_warn() {
    echo -e "${YELLOW}$*${NC}"
}

print_info() {
    echo -e "${BLUE}$*${NC}"
}

print_success() {
    echo -e "${GREEN}$*${NC}"
}

print_section() {
    echo -e "${BOLD}${BLUE}$*${NC}"
}

accent_text() {
    echo -e "${CYAN}$*${NC}"
}

check_python_yaml() {
    if ! command -v python3 >/dev/null 2>&1; then
        print_error "错误: 未找到 python3，无法渲染运行配置或解析 API 响应"
        return 1
    fi

    if ! python3 - <<'PY' >/dev/null 2>&1
import yaml
PY
    then
        print_error "错误: python3 缺少 PyYAML 模块，无法渲染运行配置"
        return 1
    fi

    return 0
}

check_requirements() {
    local errors=0

    if [ ! -x "$PROG_PATH" ]; then
        print_error "错误: mihomo 可执行文件不存在或无执行权限: $PROG_PATH"
        errors=$((errors + 1))
    fi

    if [ ! -f "$SOURCE_CONFIG_FILE" ]; then
        print_error "错误: 原始配置文件不存在: $SOURCE_CONFIG_FILE"
        errors=$((errors + 1))
    fi

    if ! command -v curl >/dev/null 2>&1; then
        print_warn "警告: curl 未安装，API 控制与 test 功能将不可用"
    fi

    if ! check_python_yaml; then
        errors=$((errors + 1))
    fi

    return "$errors"
}

get_read_config_file() {
    if [ -f "$RUNTIME_CONFIG_FILE" ]; then
        echo "$RUNTIME_CONFIG_FILE"
    else
        echo "$SOURCE_CONFIG_FILE"
    fi
}

get_yaml_value() {
    local config_file="$1"
    local key="$2"
    local fallback="${3:-}"

    if [ ! -f "$config_file" ]; then
        echo "$fallback"
        return 0
    fi

    python3 - "$config_file" "$key" "$fallback" <<'PY'
import sys
import yaml

config_path, key, fallback = sys.argv[1:4]
try:
    with open(config_path, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
except Exception:
    print(fallback)
    raise SystemExit(0)

value = data.get(key, fallback)
if value is None:
    value = fallback

if isinstance(value, bool):
    print("true" if value else "false")
else:
    print(value)
PY
}

get_proxy_port() {
    local config_file
    config_file="$(get_read_config_file)"
    get_yaml_value "$config_file" "mixed-port" "$DEFAULT_PROXY_PORT"
}

get_controller_addr() {
    local config_file
    config_file="$(get_read_config_file)"
    get_yaml_value "$config_file" "external-controller" "$DEFAULT_CONTROLLER_ADDR"
}

get_api_secret() {
    local config_file
    config_file="$(get_read_config_file)"
    get_yaml_value "$config_file" "secret" ""
}

get_controller_url() {
    local addr
    addr="$(get_controller_addr)"

    case "$addr" in
        http://*|https://*)
            echo "$addr"
            ;;
        *)
            echo "http://$addr"
            ;;
    esac
}

get_proxy_http_url() {
    local port
    port="$(get_proxy_port)"
    echo "http://127.0.0.1:${port}"
}

get_proxy_all_url() {
    local port
    port="$(get_proxy_port)"
    echo "socks5h://127.0.0.1:${port}"
}

build_proxy_env_lines() {
    local http_url
    local all_url
    local no_proxy

    http_url="$(get_proxy_http_url)"
    all_url="$(get_proxy_all_url)"
    no_proxy="${PROXY_NO_PROXY:-$PROXY_NO_PROXY_DEFAULT}"

    cat <<EOF
HTTP_PROXY=$http_url
HTTPS_PROXY=$http_url
ALL_PROXY=$all_url
http_proxy=$http_url
https_proxy=$http_url
all_proxy=$all_url
NO_PROXY=$no_proxy
no_proxy=$no_proxy
EOF
}

python_display_name_def() {
    cat <<'PY'
import re

def display_name(value):
    if value in ("-", None):
        return "-"

    text = str(value).strip()
    match = re.match(r"^(\S+)\s+(.+)$", text)
    if match:
        prefix, rest = match.groups()
        if prefix and all(not ch.isalnum() for ch in prefix):
            text = rest.strip()

    text = text.replace("丨", " ")
    text = text.replace("|", " ")
    text = " ".join(text.split())
    return text
PY
}

proxy_env() {
    build_proxy_env_lines
}

with_proxy() {
    if [ "$#" -eq 0 ]; then
        print_error "错误: 用法: $0 with-proxy <command> [args...]"
        return 1
    fi

    local http_url
    local all_url
    local no_proxy

    http_url="$(get_proxy_http_url)"
    all_url="$(get_proxy_all_url)"
    no_proxy="${PROXY_NO_PROXY:-$PROXY_NO_PROXY_DEFAULT}"

    env \
        HTTP_PROXY="$http_url" \
        HTTPS_PROXY="$http_url" \
        ALL_PROXY="$all_url" \
        http_proxy="$http_url" \
        https_proxy="$http_url" \
        all_proxy="$all_url" \
        NO_PROXY="$no_proxy" \
        no_proxy="$no_proxy" \
        "$@"
}

proxy_shell() {
    local shell_bin

    shell_bin="${SHELL:-/bin/bash}"

    print_info "进入临时代理 shell，退出后代理环境失效"
    with_proxy "$shell_bin" -l
}

urlencode() {
    python3 - "$1" <<'PY'
import sys
from urllib.parse import quote

print(quote(sys.argv[1], safe=""))
PY
}

api_request() {
    local method="$1"
    local path="$2"
    local data="${3:-}"
    local base_url
    local secret
    local curl_args=()

    base_url="$(get_controller_url)"
    secret="$(get_api_secret)"

    curl_args=(-fsS --connect-timeout 5 -X "$method")

    if [ -n "$secret" ]; then
        curl_args+=(-H "Authorization: Bearer $secret")
    fi

    if [ -n "$data" ]; then
        curl_args+=(-H "Content-Type: application/json" --data "$data")
    fi

    curl "${curl_args[@]}" "${base_url}${path}"
}

api_delay_test() {
    local target="$1"
    local url="$2"
    local timeout="$3"
    local base_url
    local secret
    local encoded_target
    local curl_args=()

    base_url="$(get_controller_url)"
    secret="$(get_api_secret)"
    encoded_target="$(urlencode "$target")"

    curl_args=(-fsS --connect-timeout 5 --get)

    if [ -n "$secret" ]; then
        curl_args+=(-H "Authorization: Bearer $secret")
    fi

    curl_args+=(
        --data-urlencode "url=$url"
        --data-urlencode "timeout=$timeout"
    )

    curl "${curl_args[@]}" "${base_url}/proxies/${encoded_target}/delay"
}

api_available() {
    api_request "GET" "/version" >/dev/null 2>&1
}

require_api() {
    if ! api_available; then
        print_error "错误: Mihomo API 不可访问，请检查 external-controller 或 secret 配置"
        return 1
    fi

    return 0
}

runtime_needs_refresh() {
    if [ ! -f "$RUNTIME_CONFIG_FILE" ]; then
        return 0
    fi

    if [ "$SOURCE_CONFIG_FILE" -nt "$RUNTIME_CONFIG_FILE" ]; then
        return 0
    fi

    return 1
}

ensure_runtime_config() {
    if runtime_needs_refresh; then
        print_warn "运行配置不存在或已过期，开始重新渲染..."
        render || return 1
    fi

    return 0
}

get_candidate_config_files() {
    local files=()

    if [ -f "$RUNTIME_CONFIG_FILE" ]; then
        files+=("$RUNTIME_CONFIG_FILE")
    elif [ -f "$SOURCE_CONFIG_FILE" ]; then
        files+=("$SOURCE_CONFIG_FILE")
    fi

    printf '%s\n' "${files[@]}"
}

get_pid() {
    if [ -n "$CACHED_PID" ] && [ -d "/proc/$CACHED_PID" ]; then
        echo "$CACHED_PID"
        return 0
    fi

    local pid=""
    local proc_pid
    local cfg

    if [ -f "$PID_FILE" ]; then
        pid="$(tr -d '[:space:]' < "$PID_FILE" 2>/dev/null)"
        if [ -n "$pid" ] && [ -f "/proc/$pid/cmdline" ]; then
            while read -r cfg; do
                [ -z "$cfg" ] && continue
                if grep -qaF "$cfg" "/proc/$pid/cmdline" 2>/dev/null; then
                    CACHED_PID="$pid"
                    echo "$pid"
                    return 0
                fi
            done < <(get_candidate_config_files)
        fi
    fi

    while read -r proc_pid; do
        [ -z "$proc_pid" ] && continue
        if [ ! -f "/proc/$proc_pid/cmdline" ]; then
            continue
        fi

        while read -r cfg; do
            [ -z "$cfg" ] && continue
            if grep -qaF "$cfg" "/proc/$proc_pid/cmdline" 2>/dev/null; then
                pid="$proc_pid"
                break 2
            fi
        done < <(get_candidate_config_files)
    done < <(pgrep -x "$PROG_NAME" 2>/dev/null)

    if [ -n "$pid" ]; then
        CACHED_PID="$pid"
    fi

    echo "$pid"
}

clear_pid_cache() {
    CACHED_PID=""
}

get_running_config_path() {
    local pid
    pid="$(get_pid)"

    if [ -z "$pid" ] || [ ! -f "/proc/$pid/cmdline" ]; then
        return 1
    fi

    python3 - "/proc/$pid/cmdline" <<'PY'
import sys

with open(sys.argv[1], "rb") as fh:
    args = fh.read().split(b"\x00")

decoded = [arg.decode("utf-8", errors="ignore") for arg in args if arg]
for idx, arg in enumerate(decoded[:-1]):
    if arg == "-f":
        print(decoded[idx + 1])
        break
PY
}

is_running() {
    local pid
    local port

    pid="$(get_pid)"
    port="$(get_proxy_port)"

    if [ -z "$pid" ]; then
        clear_pid_cache
        return 1
    fi

    if [ ! -d "/proc/$pid" ]; then
        clear_pid_cache
        return 1
    fi

    if ss -tln | grep -Eq "LISTEN.+:${port}[[:space:]]"; then
        return 0
    fi

    return 1
}

render() {
    if ! check_requirements; then
        return 1
    fi

    mkdir -p "$CONFIG_DIR" || {
        print_error "错误: 无法创建目录: $CONFIG_DIR"
        return 1
    }

    python3 - "$SOURCE_CONFIG_FILE" "$RUNTIME_CONFIG_FILE" \
        "$AI_MANUAL_GROUP" "$AI_AUTO_GROUP" "$AI_US_GROUP" "$AI_SG_GROUP" \
        "$AI_REGION_US" "$AI_REGION_SG" "$TEST_URL" <<'PY'
import os
import sys
import yaml

(
    source_path,
    runtime_path,
    ai_manual_group,
    ai_auto_group,
    ai_us_group,
    ai_sg_group,
    ai_region_us,
    ai_region_sg,
    test_url,
) = sys.argv[1:10]

AI_RULES = [
    f"DOMAIN-SUFFIX,openai.com,{ai_manual_group}",
    f"DOMAIN-SUFFIX,chatgpt.com,{ai_manual_group}",
    f"DOMAIN-SUFFIX,oaistatic.com,{ai_manual_group}",
    f"DOMAIN-SUFFIX,oaiusercontent.com,{ai_manual_group}",
    f"DOMAIN-SUFFIX,anthropic.com,{ai_manual_group}",
    f"DOMAIN-SUFFIX,claude.ai,{ai_manual_group}",
    f"DOMAIN,gemini.google.com,{ai_manual_group}",
    f"DOMAIN,aistudio.google.com,{ai_manual_group}",
    f"DOMAIN,ai.google.dev,{ai_manual_group}",
    f"DOMAIN,generativelanguage.googleapis.com,{ai_manual_group}",
]
MAINLAND_DIRECT_RULES = [
    "GEOIP,CN,DIRECT,no-resolve",
]

AI_CONFLICT_RULES = {
    "DOMAIN-KEYWORD,chatgpt,SSRDOG",
    "DOMAIN-KEYWORD,openai,SSRDOG",
    "DOMAIN-SUFFIX,chatgpt.com,SSRDOG",
    "DOMAIN-SUFFIX,openai.com,SSRDOG",
    "DOMAIN-SUFFIX,anthropic.com,SSRDOG",
    "DOMAIN-SUFFIX,claude.ai,SSRDOG",
}

AI_MANAGED_GROUPS = {ai_manual_group, ai_auto_group, ai_us_group, ai_sg_group}

with open(source_path, "r", encoding="utf-8") as fh:
    data = yaml.safe_load(fh) or {}

if not isinstance(data, dict):
    raise SystemExit("错误: 原始配置格式非法，顶层必须是 YAML 对象")

groups = data.get("proxy-groups") or []
if not isinstance(groups, list):
    raise SystemExit("错误: proxy-groups 必须是列表")

group_map = {}
for group in groups:
    if isinstance(group, dict) and group.get("name"):
        group_map[group["name"]] = group

missing_groups = [name for name in (ai_region_us, ai_region_sg) if name not in group_map]
if missing_groups:
    raise SystemExit(f"错误: 原始配置缺少必需的区域组: {', '.join(missing_groups)}")

us_proxies = group_map[ai_region_us].get("proxies") or []
sg_proxies = group_map[ai_region_sg].get("proxies") or []
if not us_proxies or not sg_proxies:
    raise SystemExit("错误: 美国或新加坡区域组未包含任何节点，无法生成 AI 自动切换组")

filtered_groups = [
    group
    for group in groups
    if not (isinstance(group, dict) and group.get("name") in AI_MANAGED_GROUPS)
]

ai_groups = [
    {
        "name": ai_us_group,
        "type": "fallback",
        "proxies": us_proxies,
        "url": test_url,
        "interval": 300,
    },
    {
        "name": ai_sg_group,
        "type": "fallback",
        "proxies": sg_proxies,
        "url": test_url,
        "interval": 300,
    },
    {
        "name": ai_auto_group,
        "type": "fallback",
        "proxies": [ai_us_group, ai_sg_group],
        "url": test_url,
        "interval": 300,
    },
    {
        "name": ai_manual_group,
        "type": "select",
        "proxies": [
            ai_auto_group,
            ai_us_group,
            ai_sg_group,
            ai_region_us,
            ai_region_sg,
        ],
    },
]

insert_after = None
for idx, group in enumerate(filtered_groups):
    if isinstance(group, dict) and group.get("name") == "Auto":
        insert_after = idx
        break

if insert_after is None:
    for idx, group in enumerate(filtered_groups):
        if isinstance(group, dict) and group.get("name") == "SSRDOG":
            insert_after = idx
            break

if insert_after is None:
    filtered_groups = ai_groups + filtered_groups
else:
    filtered_groups = (
        filtered_groups[: insert_after + 1]
        + ai_groups
        + filtered_groups[insert_after + 1 :]
    )

rules = data.get("rules") or []
if not isinstance(rules, list):
    raise SystemExit("错误: rules 必须是列表")

clean_rules = [
    rule
    for rule in rules
    if rule not in AI_RULES and rule not in AI_CONFLICT_RULES and rule not in MAINLAND_DIRECT_RULES
]

insert_index = None
for idx, rule in enumerate(clean_rules):
    if not isinstance(rule, str):
        continue
    if rule == "RULE-SET,ChinaMax,DIRECT":
        insert_index = idx
        break
    if rule.startswith("MATCH,"):
        insert_index = idx
        break

if insert_index is None:
    clean_rules.extend(AI_RULES)
else:
    clean_rules = clean_rules[:insert_index] + AI_RULES + clean_rules[insert_index:]

match_index = None
for idx, rule in enumerate(clean_rules):
    if isinstance(rule, str) and rule.startswith("MATCH,"):
        match_index = idx
        break

if match_index is None:
    clean_rules.extend(MAINLAND_DIRECT_RULES)
else:
    clean_rules = clean_rules[:match_index] + MAINLAND_DIRECT_RULES + clean_rules[match_index:]

data["proxy-groups"] = filtered_groups
data["rules"] = clean_rules

tmp_path = f"{runtime_path}.tmp"
with open(tmp_path, "w", encoding="utf-8") as fh:
    yaml.safe_dump(
        data,
        fh,
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
    )

os.replace(tmp_path, runtime_path)
PY

    local result=$?
    if [ "$result" -ne 0 ]; then
        print_error "错误: 渲染运行配置失败"
        return 1
    fi

    print_success "运行配置已生成: $RUNTIME_CONFIG_FILE"
    return 0
}

start() {
    if ! check_requirements; then
        return 1
    fi

    if ! ensure_runtime_config; then
        return 1
    fi

    if is_running; then
        local pid
        pid="$(get_pid)"
        print_success "代理已在运行中 (PID: $pid)"
        return 0
    fi

    if [ -f "$LOCK_FILE" ]; then
        local lock_age=0
        lock_age=$(( $(date +%s) - $(stat -c %Y "$LOCK_FILE" 2>/dev/null || echo 0) ))
        if [ "$lock_age" -lt 30 ]; then
            print_warn "另一个启动进程正在进行中，请稍候..."
            return 1
        fi
        rm -f "$LOCK_FILE"
    fi

    print_warn "启动代理..."
    touch "$LOCK_FILE"
    mkdir -p "$(dirname "$LOG_FILE")"

    cd "$CONFIG_DIR" || {
        print_error "错误: 无法进入目录: $CONFIG_DIR"
        rm -f "$LOCK_FILE"
        return 1
    }

    nohup "$PROG_PATH" -f "$RUNTIME_CONFIG_FILE" -d "$CONFIG_DIR" >> "$LOG_FILE" 2>&1 &

    local count=0
    while [ "$count" -lt "$START_TIMEOUT" ]; do
        if is_running; then
            local pid
            local port
            pid="$(get_pid)"
            port="$(get_proxy_port)"
            echo "$pid" > "$PID_FILE"
            rm -f "$LOCK_FILE"
            clear_pid_cache
            print_success "代理启动成功! (PID: $pid, 端口: $port)"
            return 0
        fi
        sleep 1
        count=$((count + 1))
    done

    rm -f "$LOCK_FILE"
    clear_pid_cache
    print_error "代理启动失败! (超时 ${START_TIMEOUT}秒)"
    print_warn "请查看日志: $LOG_FILE"
    return 1
}

stop() {
    if ! is_running; then
        print_warn "代理未运行"
        rm -f "$PID_FILE"
        clear_pid_cache
        return 0
    fi

    local pid
    pid="$(get_pid)"
    print_warn "停止代理 (PID: $pid)..."

    if kill "$pid" 2>/dev/null; then
        local count=0
        local max_wait=10
        while kill -0 "$pid" 2>/dev/null; do
            if [ "$count" -ge "$max_wait" ]; then
                print_warn "进程未响应，强制终止..."
                kill -9 "$pid" 2>/dev/null
                sleep 1
                break
            fi
            sleep 1
            count=$((count + 1))
            echo -n "."
        done
        echo ""
    fi

    rm -f "$PID_FILE"
    clear_pid_cache

    if is_running; then
        print_error "停止失败，进程仍在运行"
        return 1
    fi

    print_success "代理已停止"
    return 0
}

restart() {
    print_warn "重启代理..."

    stop
    local stop_result=$?
    sleep 1
    start
    local start_result=$?

    if [ "$stop_result" -ne 0 ] || [ "$start_result" -ne 0 ]; then
        return 1
    fi

    return 0
}

status() {
    local raw_mode=0
    local port
    local controller
    local running_config
    local config_state
    local status_text
    local api_text
    local ai_mode="-"
    local ai_summary="-"

    if [ "${1:-}" = "--raw" ]; then
        raw_mode=1
    fi

    port="$(get_proxy_port)"
    controller="$(get_controller_addr)"

    if runtime_needs_refresh; then
        config_state="${YELLOW}待刷新${NC}"
    else
        config_state="${GREEN}已就绪${NC}"
    fi

    if is_running; then
        local pid
        local elapsed
        local mem_usage
        local connections
        local log_size

        pid="$(get_pid)"
        running_config="$(get_running_config_path)"

        status_text="${GREEN}运行中${NC}"

        elapsed="$(ps -o etimes= -p "$pid" 2>/dev/null | tr -d ' ')"
        if [ -n "$elapsed" ]; then
            local hours=$((elapsed / 3600))
            local minutes=$(((elapsed % 3600) / 60))
            local seconds=$((elapsed % 60))
        fi

        mem_usage="$(ps -o rss= -p "$pid" 2>/dev/null | tr -d ' ')"
        if [ -n "$mem_usage" ]; then
            local mem_mb=$((mem_usage / 1024))
            mem_usage="${mem_mb}MB"
        fi

        connections=$(ss -tn | grep ":$port" | wc -l)

        if api_available; then
            api_text="${GREEN}可访问${NC}"
            readarray -t ai_status_lines < <(api_request "GET" "/proxies" | DISPLAY_NAME_PY="$(python_display_name_def)" python3 -c '
import json
import os
import sys

exec(os.environ["DISPLAY_NAME_PY"])
data = json.load(sys.stdin).get("proxies", {})

manual_target = (data.get("AI-MANUAL") or {}).get("now", "-")
auto_target = (data.get("AI-AUTO") or {}).get("now", "-")

active_group = auto_target if manual_target == "AI-AUTO" else manual_target
active = data.get(active_group) or {}
active_node = active.get("now", "-")
history = active.get("history") or []
delay = "-"
if history:
    delay = history[-1].get("delay", "-")

auto_mode = manual_target == "AI-AUTO"
mode_label = "自动切换" if auto_mode else f"固定 {manual_target}"
summary = f"{active_group} -> {display_name(active_node)}"
if delay not in (None, "-"):
    summary = f"{summary} ({delay}ms)"

print(mode_label)
print(summary)
')
            if [ "${#ai_status_lines[@]}" -ge 1 ] && [ -n "${ai_status_lines[0]}" ]; then
                ai_mode="${ai_status_lines[0]}"
            fi
            if [ "${#ai_status_lines[@]}" -ge 2 ] && [ -n "${ai_status_lines[1]}" ]; then
                ai_summary="${ai_status_lines[1]}"
            fi
        else
            api_text="${RED}不可访问${NC}"
        fi

        if [ -f "$LOG_FILE" ]; then
            log_size="$(du -h "$LOG_FILE" 2>/dev/null | cut -f1)"
        fi

        if [ "$raw_mode" -eq 1 ]; then
            echo -e "版本: 1.2.0"
            echo -e "原始配置: $SOURCE_CONFIG_FILE"
            echo -e "运行配置: $RUNTIME_CONFIG_FILE"
            echo -e "控制接口: $controller"
            echo -e "代理端口: $port"
            echo -e "运行配置状态: ${config_state}"
            echo -e "状态: ${status_text}"
            echo -e "PID: $pid"
            if [ -n "$running_config" ] && [ "$running_config" != "$RUNTIME_CONFIG_FILE" ]; then
                echo -e "实际运行配置: $running_config"
            fi
            if [ -n "${elapsed:-}" ]; then
                printf "运行时间: %02dh %02dm %02ds\n" "$hours" "$minutes" "$seconds"
            fi
            if [ -n "${mem_usage:-}" ]; then
                echo -e "内存使用: ${mem_usage}"
            fi
            echo -e "当前连接数: $connections"
            echo -e "API 状态: ${api_text}"
            if [ -n "${log_size:-}" ]; then
                echo -e "日志大小: $log_size"
            fi
            return 0
        fi

        print_section "摘要"
        echo -e "状态: $status_text"
        echo -e "API: $api_text"
        echo -e "AI 路由模式: $ai_mode"
        echo -e "AI 当前出口: $ai_summary"
        echo -e "运行配置状态: $config_state"
        echo ""
        print_section "资源"
        echo -e "代理端口: $port"
        echo -e "控制接口: $controller"
        echo -e "连接数: $connections"
        if [ -n "${elapsed:-}" ]; then
            printf "运行时间: %02dh %02dm %02ds\n" "$hours" "$minutes" "$seconds"
        fi
        if [ -n "${mem_usage:-}" ]; then
            echo -e "内存: $mem_usage"
        fi
        if [ -n "${log_size:-}" ]; then
            echo -e "日志: $log_size"
        fi
        echo ""
        print_section "路径"
        echo -e "原始配置: $SOURCE_CONFIG_FILE"
        echo -e "运行配置: $RUNTIME_CONFIG_FILE"
        if [ -n "$running_config" ] && [ "$running_config" != "$RUNTIME_CONFIG_FILE" ]; then
            echo -e "实际运行配置: $running_config"
        fi
        echo -e "PID: $pid"
    else
        status_text="${RED}未运行${NC}"
        api_text="${RED}不可访问${NC}"

        if [ "$raw_mode" -eq 1 ]; then
            echo -e "版本: 1.2.0"
            echo -e "原始配置: $SOURCE_CONFIG_FILE"
            echo -e "运行配置: $RUNTIME_CONFIG_FILE"
            echo -e "控制接口: $controller"
            echo -e "代理端口: $port"
            echo -e "运行配置状态: ${config_state}"
            echo -e "状态: ${status_text}"
            return 0
        fi

        print_section "摘要"
        echo -e "状态: $status_text"
        echo -e "API: $api_text"
        echo -e "运行配置状态: $config_state"
        echo ""
        print_section "资源"
        echo -e "代理端口: $port"
        echo -e "控制接口: $controller"
        echo ""
        print_section "路径"
        echo -e "原始配置: $SOURCE_CONFIG_FILE"
        echo -e "运行配置: $RUNTIME_CONFIG_FILE"
    fi
}

logs() {
    if [ ! -f "$LOG_FILE" ]; then
        print_error "日志文件不存在: $LOG_FILE"
        return 1
    fi

    print_section "日志"
    echo -e "日志文件: $LOG_FILE"
    echo ""

    trap 'echo -e "\n${YELLOW}日志查看已停止${NC}"; return 0' INT

    tail -f "$LOG_FILE" 2>/dev/null || {
        print_error "无法读取日志文件"
        return 1
    }
}

test() {
    local proxy_url
    local passed=0
    local total=0
    local ip=""
    local api
    local port

    if ! is_running; then
        print_error "代理未运行，无法测试"
        return 1
    fi

    if ! command -v curl >/dev/null 2>&1; then
        print_error "curl 未安装，无法进行测试"
        return 1
    fi

    port="$(get_proxy_port)"
    proxy_url="http://127.0.0.1:$port"

    local results_output=""

    total=$((total + 1))
    if curl -x "$proxy_url" -I -s --connect-timeout 5 "https://www.google.com" >/dev/null 2>&1; then
        passed=$((passed + 1))
        results_output="${results_output}正常  https://www.google.com  成功"$'\n'
    else
        results_output="${results_output}失败  https://www.google.com  失败"$'\n'
    fi

    total=$((total + 1))
    if curl -x "$proxy_url" -I -s --connect-timeout 5 "https://github.com" >/dev/null 2>&1; then
        passed=$((passed + 1))
        results_output="${results_output}正常  https://github.com  成功"$'\n'
    else
        results_output="${results_output}失败  https://github.com  失败"$'\n'
    fi

    total=$((total + 1))
    for api in "https://api.ip.sb/ip" "https://ifconfig.me/ip" "https://icanhazip.com"; do
        ip=$(curl -x "$proxy_url" -s --connect-timeout 5 "$api" 2>/dev/null | tr -d '[:space:]')
        if [ -n "$ip" ]; then
            passed=$((passed + 1))
            results_output="${results_output}正常  出口 IP  ${ip}"$'\n'
            break
        fi
    done

    if [ -z "$ip" ]; then
        results_output="${results_output}失败  出口 IP  获取失败"$'\n'
    fi

    print_section "摘要"
    echo "目标: 代理连通性"
    echo "可用: $passed/$total"
    echo "出口 IP: ${ip:--}"
    echo ""
    print_section "结果"
    printf '%s' "$results_output"

    if [ "$passed" -eq "$total" ]; then
        return 0
    fi

    return 1
}

list_groups() {
    local raw_mode=0

    if [ "${1:-}" = "--raw" ]; then
        raw_mode=1
    fi

    if ! ensure_runtime_config; then
        return 1
    fi

    if [ "$raw_mode" -eq 1 ]; then
        python3 - "$RUNTIME_CONFIG_FILE" <<'PY'
import sys
import yaml

with open(sys.argv[1], "r", encoding="utf-8") as fh:
    data = yaml.safe_load(fh) or {}

for group in data.get("proxy-groups", []):
    if not isinstance(group, dict):
        continue
    group_type = str(group.get("type", "")).lower()
    if group_type in {"select", "fallback", "url-test", "load-balance"}:
        print(f"{group.get('name', '')}\t{group_type}")
PY
        return $?
    fi

    if api_available; then
        local proxies_json
        proxies_json="$(api_request "GET" "/proxies")" || return 1

        PROXIES_JSON="$proxies_json" BOLD="$BOLD" BLUE="$BLUE" NC="$NC" python3 - "$RUNTIME_CONFIG_FILE" <<'PY'
import json
import os
import re
import sys
import yaml

with open(sys.argv[1], "r", encoding="utf-8") as fh:
    data = yaml.safe_load(fh) or {}

proxies = json.loads(os.environ["PROXIES_JSON"]).get("proxies", {})
bold = os.environ.get("BOLD", "").replace("\\033", "\033")
blue = os.environ.get("BLUE", "").replace("\\033", "\033")
reset = os.environ.get("NC", "").replace("\\033", "\033")

def section(title):
    print(f"{bold}{blue}{title}{reset}")

def display_name(value):
    if value in ("-", None):
        return "-"
    text = str(value).strip()
    match = re.match(r"^(\S+)\s+(.+)$", text)
    if match:
        prefix, rest = match.groups()
        if prefix and all(not ch.isalnum() for ch in prefix):
            text = rest.strip()
    text = text.replace("丨", " ")
    text = text.replace("|", " ")
    text = " ".join(text.split())
    return text

items = []
for group in data.get("proxy-groups", []):
    if not isinstance(group, dict):
        continue
    group_name = group.get("name", "")
    group_type = str(group.get("type", "")).lower()
    if group_type not in {"select", "fallback", "url-test", "load-balance"}:
        continue
    current = display_name((proxies.get(group_name) or {}).get("now", "-"))
    items.append((group_name, group_type, current))

section("摘要")
print(f"总组数: {len(items)}")
print(f"可切换组数: {len(items)}")
print()
section("列表")
print(f"{'组名':<20} {'类型':<12} 当前选择")
for group_name, group_type, current in items:
    print(f"{group_name:<20} {group_type:<12} {current}")
PY
        return $?
    fi

    BOLD="$BOLD" BLUE="$BLUE" NC="$NC" python3 - "$RUNTIME_CONFIG_FILE" <<'PY'
import sys
import yaml

with open(sys.argv[1], "r", encoding="utf-8") as fh:
    data = yaml.safe_load(fh) or {}

import os
bold = os.environ.get("BOLD", "").replace("\\033", "\033")
blue = os.environ.get("BLUE", "").replace("\\033", "\033")
reset = os.environ.get("NC", "").replace("\\033", "\033")

def section(title):
    print(f"{bold}{blue}{title}{reset}")

items = []
for group in data.get("proxy-groups", []):
    if not isinstance(group, dict):
        continue
    group_type = str(group.get("type", "")).lower()
    if group_type in {"select", "fallback", "url-test", "load-balance"}:
        items.append((group.get("name", ""), group_type, "-"))

section("摘要")
print(f"总组数: {len(items)}")
print(f"可切换组数: {len(items)}")
print()
section("列表")
print(f"{'组名':<20} {'类型':<12} 当前选择")
for name, group_type, current in items:
    print(f"{name:<20} {group_type:<12} {current}")
PY
}

list_nodes() {
    local group_name="$1"
    local raw_mode=0

    if [ -z "$group_name" ]; then
        print_error "错误: 请指定代理组名称"
        return 1
    fi

    if [ "${2:-}" = "--raw" ]; then
        raw_mode=1
    fi

    if api_available; then
        local proxies_json
        proxies_json="$(api_request "GET" "/proxies")" || return 1

        if [ "$raw_mode" -eq 1 ]; then
            printf '%s' "$proxies_json" | python3 -c '
import json
import sys

group_name = sys.argv[1]
data = json.load(sys.stdin)
group = data.get("proxies", {}).get(group_name)

if not group:
    raise SystemExit(f"错误: 未找到代理组: {group_name}")

current = group.get("now", "")
for item in group.get("all", []):
    prefix = "* " if item == current else "  "
    print(f"{prefix}{item}")
' "$group_name"
            return $?
        fi

        printf '%s' "$proxies_json" | BOLD="$BOLD" BLUE="$BLUE" NC="$NC" python3 -c '
import json
import os
import re
import sys

bold = os.environ.get("BOLD", "").replace("\\033", "\033")
blue = os.environ.get("BLUE", "").replace("\\033", "\033")
reset = os.environ.get("NC", "").replace("\\033", "\033")

def section(title):
    print(f"{bold}{blue}{title}{reset}")

group_name = sys.argv[1]
data = json.load(sys.stdin)
group = data.get("proxies", {}).get(group_name)

if not group:
    raise SystemExit(f"错误: 未找到代理组: {group_name}")

def display_name(value):
    if value in ("-", None):
        return "-"
    text = str(value).strip()
    match = re.match(r"^(\S+)\s+(.+)$", text)
    if match:
        prefix, rest = match.groups()
        if prefix and all(not ch.isalnum() for ch in prefix):
            text = rest.strip()
    text = text.replace("丨", " ")
    text = text.replace("|", " ")
    text = " ".join(text.split())
    return text

current = group.get("now", "")
items = group.get("all", [])
section("摘要")
print(f"目标组: {group_name}")
print(f"当前选择: {display_name(current)}")
print(f"候选数: {len(items)}")
print()
section("列表")
for item in items:
    label = "当前" if item == current else "候选"
    print(f"{label}  {display_name(item)}")
' "$group_name"
        return $?
    fi

    if ! ensure_runtime_config; then
        return 1
    fi

    BOLD="$BOLD" BLUE="$BLUE" NC="$NC" python3 - "$RUNTIME_CONFIG_FILE" "$group_name" <<'PY'
import sys
import re
import yaml
import os

bold = os.environ.get("BOLD", "").replace("\\033", "\033")
blue = os.environ.get("BLUE", "").replace("\\033", "\033")
reset = os.environ.get("NC", "").replace("\\033", "\033")

def section(title):
    print(f"{bold}{blue}{title}{reset}")

config_path, group_name = sys.argv[1:3]
with open(config_path, "r", encoding="utf-8") as fh:
    data = yaml.safe_load(fh) or {}

def display_name(value):
    if value in ("-", None):
        return "-"
    text = str(value).strip()
    match = re.match(r"^(\S+)\s+(.+)$", text)
    if match:
        prefix, rest = match.groups()
        if prefix and all(not ch.isalnum() for ch in prefix):
            text = rest.strip()
    text = text.replace("丨", " ")
    text = text.replace("|", " ")
    text = " ".join(text.split())
    return text

for group in data.get("proxy-groups", []):
    if isinstance(group, dict) and group.get("name") == group_name:
        items = list(group.get("proxies", []))
        section("摘要")
        print(f"目标组: {group_name}")
        print("当前选择: -")
        print(f"候选数: {len(items)}")
        print()
        section("列表")
        for item in items:
            print(f"候选  {display_name(item)}")
        break
else:
    raise SystemExit(f"错误: 未找到代理组: {group_name}")
PY
}

current_group() {
    local group_name="$1"
    local raw_mode=0

    if [ -z "$group_name" ]; then
        print_error "错误: 请指定代理组名称"
        return 1
    fi

    if [ "${2:-}" = "--raw" ]; then
        raw_mode=1
    fi

    if ! require_api; then
        return 1
    fi

    local proxies_json
    proxies_json="$(api_request "GET" "/proxies")" || return 1

    if [ "$raw_mode" -eq 1 ]; then
        printf '%s' "$proxies_json" | python3 -c '
import json
import sys

group_name = sys.argv[1]
data = json.load(sys.stdin)
group = data.get("proxies", {}).get(group_name)

if not group:
    raise SystemExit(f"错误: 未找到代理组: {group_name}")

current = group.get("now")
if current:
    print(current)
else:
    raise SystemExit(f"错误: 代理组 [{group_name}] 当前无可读的 now 状态，请确认该组类型支持读取当前选择")
' "$group_name"
        return $?
    fi

    printf '%s' "$proxies_json" | DISPLAY_NAME_PY="$(python_display_name_def)" CYAN="$CYAN" NC="$NC" python3 -c '
import json
import os
import sys

exec(os.environ["DISPLAY_NAME_PY"])
cyan = os.environ.get("CYAN", "").replace("\\033", "\033")
reset = os.environ.get("NC", "").replace("\\033", "\033")
group_name = sys.argv[1]
data = json.load(sys.stdin)
group = data.get("proxies", {}).get(group_name)

if not group:
    raise SystemExit(f"错误: 未找到代理组: {group_name}")

current = group.get("now")
if current:
    print(f"当前选择: {cyan}{display_name(current)}{reset}")
else:
    raise SystemExit(f"错误: 代理组 [{group_name}] 当前无可读的 now 状态，请确认该组类型支持读取当前选择")
' "$group_name"
}

switch_group() {
    local group_name="$1"
    local target_name="$2"

    if [ -z "$group_name" ] || [ -z "$target_name" ]; then
        print_error "错误: 用法: $0 switch <group> <node>"
        return 1
    fi

    if ! require_api; then
        return 1
    fi

    local proxies_json
    proxies_json="$(api_request "GET" "/proxies")" || return 1

    if ! printf '%s' "$proxies_json" | python3 -c '
import json
import sys

group_name, target_name = sys.argv[1:3]
data = json.load(sys.stdin)
group = data.get("proxies", {}).get(group_name)

if not group:
    raise SystemExit(f"错误: 未找到代理组: {group_name}")

if group.get("type") != "Selector":
    raise SystemExit(f"错误: 代理组 [{group_name}] 不是可手动切换的 Selector 类型")

if target_name not in group.get("all", []):
    raise SystemExit(f"错误: 目标 [{target_name}] 不在代理组 [{group_name}] 的候选列表中")
' "$group_name" "$target_name"
    then
        return 1
    fi

    local encoded_group
    encoded_group="$(urlencode "$group_name")"

    api_request "PUT" "/proxies/${encoded_group}" "{\"name\":\"${target_name//\"/\\\"}\"}" >/dev/null || return 1
    proxies_json="$(api_request "GET" "/proxies")" || return 1

    print_section "切换结果"
    printf '%s' "$proxies_json" | DISPLAY_NAME_PY="$(python_display_name_def)" CYAN="$CYAN" NC="$NC" python3 -c '
import json
import os
import sys

exec(os.environ["DISPLAY_NAME_PY"])
cyan = os.environ.get("CYAN", "").replace("\\033", "\033")
reset = os.environ.get("NC", "").replace("\\033", "\033")
group_name = sys.argv[1]
data = json.load(sys.stdin).get("proxies", {})
group = data.get(group_name)

if not group:
    raise SystemExit(f"错误: 未找到代理组: {group_name}")

current = group.get("now", "-")
print(f"代理组: {group_name}")
print(f"当前选择: {cyan}{display_name(current)}{reset}")
' "$group_name"
    return 0
}

ai_status() {
    local raw_mode=0
    local config_file
    local proxy_http_url
    local probe_timeout
    local chatgpt_url
    local openai_api_url

    if [ "${1:-}" = "--raw" ]; then
        raw_mode=1
    fi

    if ! require_api; then
        return 1
    fi

    local proxies_json
    proxies_json="$(api_request "GET" "/proxies")" || return 1

    config_file="$(get_read_config_file)"
    proxy_http_url="$(get_proxy_http_url)"
    probe_timeout="$(get_yaml_value "$config_file" "ai-probe-timeout" "$(get_yaml_value "$config_file" "connectivity-timeout" "5")")"
    chatgpt_url="$(get_yaml_value "$config_file" "ai-chatgpt-url" "https://chatgpt.com")"
    openai_api_url="$(get_yaml_value "$config_file" "ai-openai-api-url" "https://api.openai.com/v1/models")"

    PROXIES_JSON="$proxies_json" DISPLAY_NAME_PY="$(python_display_name_def)" BOLD="$BOLD" BLUE="$BLUE" NC="$NC" python3 -c '
import json
import os
import socket
import sys
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener

exec(os.environ["DISPLAY_NAME_PY"])
bold = os.environ.get("BOLD", "").replace("\\033", "\033")
blue = os.environ.get("BLUE", "").replace("\\033", "\033")
reset = os.environ.get("NC", "").replace("\\033", "\033")
raw_mode, ai_manual, ai_auto, ai_us, ai_sg, region_us, region_sg, proxy_url, probe_timeout, chatgpt_url, openai_api_url = sys.argv[1:12]
data = json.loads(os.environ["PROXIES_JSON"]).get("proxies", {})

def section(title):
    print(f"{bold}{blue}{title}{reset}")

def get_group(name):
    return data.get(name) or {}

def get_current(name):
    return get_group(name).get("now", "-")

def get_type(name):
    return get_group(name).get("type", "-")

def get_delay(name):
    history = get_group(name).get("history") or []
    if not history:
        return "-"
    return history[-1].get("delay", "-")

def get_status(name):
    alive = get_group(name).get("alive")
    if alive is True:
        return "正常"
    if alive is False:
        return "异常"
    return "未知"

def format_delay(value):
    if value in ("-", None):
        return "-"
    return f"{value}ms"

def probe_failure_detail(exc):
    if isinstance(exc, socket.timeout):
        return "超时"

    text = str(exc).strip()
    if "timed out" in text.lower():
        return "超时"
    if text:
        return text
    return "连接失败"

def probe_targets():
    opener = build_opener(ProxyHandler({"http": proxy_url, "https": proxy_url}))
    results = []

    for name, url in (("ChatGPT Web", chatgpt_url), ("OpenAI API", openai_api_url)):
        request = Request(url, headers={"User-Agent": "cproxy/1.2.0"})
        try:
            with opener.open(request, timeout=int(probe_timeout)) as response:
                status = getattr(response, "status", response.getcode())
            results.append({"name": name, "url": url, "ok": True, "detail": f"HTTP {status}" if status else "成功"})
        except HTTPError as exc:
            results.append({"name": name, "url": url, "ok": 400 <= exc.code < 500, "detail": f"HTTP {exc.code}"})
        except (URLError, OSError) as exc:
            results.append({"name": name, "url": url, "ok": False, "detail": probe_failure_detail(exc)})

    return results

def probe_summary(results):
    ok_count = sum(1 for item in results if item["ok"])
    if ok_count == len(results):
        return "正常"
    if ok_count == 0:
        return "失败"
    return "部分异常"

manual_target = get_current(ai_manual)
auto_target = get_current(ai_auto)
probe_results = probe_targets()
probe_status = probe_summary(probe_results)

auto_mode = manual_target == ai_auto

if auto_mode:
    active_group = auto_target
    mode_label = "自动切换"
else:
    active_group = manual_target
    mode_label = f"固定 {manual_target}"

active_node = get_current(active_group)
active_delay = get_delay(active_group)
active_status = get_status(active_group)

if active_group == ai_us:
    standby_group = ai_sg
elif active_group == ai_sg:
    standby_group = ai_us
else:
    standby_group = ai_sg

standby_node = get_current(standby_group)
standby_delay = get_delay(standby_group)
standby_status = get_status(standby_group)

if raw_mode == "1":
    def describe(name):
        group = data.get(name)
        if not group:
            print(f"{name}: 缺失")
            return

        current = group.get("now", "-")
        group_type = group.get("type", "-")
        alive = group.get("alive", "-")
        history = group.get("history") or []
        delay = "-"
        if history:
            delay = history[-1].get("delay", "-")
        print(f"{name}: type={group_type} now={current} alive={alive} last_delay={delay}")

    for name in (ai_manual, ai_auto, ai_us, ai_sg, region_us, region_sg):
        describe(name)

    print(f"AI-PROBE: {probe_status}")
    for item in probe_results:
        print(
            "AI-PROBE-ITEM: name="
            + str(item["name"])
            + " ok="
            + str(item["ok"])
            + " detail="
            + str(item["detail"])
            + " url="
            + str(item["url"])
        )
    raise SystemExit(0)

section("摘要")
print(
    f"AI 路由: {mode_label}  当前出口={display_name(active_node)}  "
    f"区域={active_group}  延迟={format_delay(active_delay)}  状态={active_status}"
)
print(f"AI 探测: {probe_status}")
print()
section("连通性")
for item in probe_results:
    label = "正常" if item["ok"] else "失败"
    print(label + "  " + str(item["name"]) + "  " + str(item["url"]))
print()
section("链路")
print(ai_manual)

if auto_mode:
    print(f"└─ {ai_auto}")
    print(f"   └─ {active_group}")
    print(f"      └─ {display_name(active_node)} ({format_delay(active_delay)})")
else:
    print(f"└─ {active_group}")
    if active_node != active_group:
        print(f"   └─ {display_name(active_node)} ({format_delay(active_delay)})")

print()
section("备用")
print(f"{standby_group} -> {display_name(standby_node)} ({format_delay(standby_delay)}, {standby_status})")
print()
section("分组")

for name in (ai_manual, ai_auto, ai_us, ai_sg):
    print(f"{name:<10} {get_type(name):<8} 当前: {display_name(get_current(name))}")
' "$raw_mode" "$AI_MANUAL_GROUP" "$AI_AUTO_GROUP" "$AI_US_GROUP" "$AI_SG_GROUP" "$AI_REGION_US" "$AI_REGION_SG" "$proxy_http_url" "$probe_timeout" "$chatgpt_url" "$openai_api_url"
}

test_group() {
    local group_name="$1"
    local raw_mode=0
    local candidates
    local member
    local result
    local delay
    local total=0
    local passed=0
    local best_member=""
    local best_delay=""
    local worst_member=""
    local worst_delay=""
    local results_output=""
    local exit_code=0

    if [ -z "$group_name" ]; then
        print_error "错误: 用法: $0 test-group <group>"
        return 1
    fi

    if [ "${2:-}" = "--raw" ]; then
        raw_mode=1
    fi

    if ! require_api; then
        return 1
    fi

    local proxies_json
    proxies_json="$(api_request "GET" "/proxies")" || return 1

    candidates="$(printf '%s' "$proxies_json" | python3 -c '
import json
import sys

group_name = sys.argv[1]
data = json.load(sys.stdin).get("proxies", {})
group = data.get(group_name)

if not group:
    raise SystemExit(f"错误: 未找到代理组或节点: {group_name}")

items = group.get("all")
if items:
    for item in items:
        print(item)
else:
    print(group_name)
    ' "$group_name"
)" || return 1

    while IFS= read -r member; do
        [ -z "$member" ] && continue
        total=$((total + 1))
        if result="$(api_delay_test "$member" "$TEST_URL" "$TEST_TIMEOUT" 2>/dev/null)"; then
            delay="$(printf '%s' "$result" | python3 -c '
import json
import sys

data = json.load(sys.stdin)
print(data.get("delay", "-"))
')"
            passed=$((passed + 1))
            if [ -z "$best_delay" ] || [ "$delay" -lt "$best_delay" ]; then
                best_delay="$delay"
                best_member="$member"
            fi
            if [ -z "$worst_delay" ] || [ "$delay" -gt "$worst_delay" ]; then
                worst_delay="$delay"
                worst_member="$member"
            fi
            results_output="${results_output}正常  ${member}  ${delay}ms"$'\n'
        else
            results_output="${results_output}失败  ${member}  -"$'\n'
            exit_code=1
        fi
    done <<<"$candidates"

    if [ "$raw_mode" -eq 1 ]; then
        while IFS= read -r line; do
            [ -z "$line" ] && continue
            status="${line%%  *}"
            rest="${line#*  }"
            name="${rest%%  *}"
            value="${rest##*  }"
            if [ "$status" = "正常" ]; then
                echo "${name}: ${value}"
            else
                echo "${name}: 失败"
            fi
        done <<<"$results_output"
        return "$exit_code"
    fi

    echo "摘要"
    echo "目标组: $group_name"
    echo "可用: $passed/$total"
    if [ -n "$best_member" ]; then
        echo "最佳: $best_member (${best_delay}ms)"
    else
        echo "最佳: -"
    fi
    if [ -n "$worst_member" ]; then
        echo "最慢: $worst_member (${worst_delay}ms)"
    else
        echo "最慢: -"
    fi
    echo ""
    echo "结果"
    printf '%s' "$results_output"

    return "$exit_code"
}

usage() {
    cat << EOF
${BLUE}Mihomo 代理管理脚本 v1.2.0${NC}

用法: $0 {start|stop|restart|status|logs|test|render|list-groups|list-nodes|current|switch|ai-status|test-group|proxy-env|with-proxy|proxy-shell}

配置与进程:
  render                    - 从原始配置生成运行配置
  start                     - 渲染运行配置并启动代理
  stop                      - 停止代理
  restart                   - 渲染运行配置并重启代理
  status                    - 查看详细状态
  logs                      - 查看实时日志

AI 路由控制:
  list-groups               - 列出可切换代理组
  list-nodes <group>        - 列出代理组候选项
  current <group>           - 查看代理组当前选择
  switch <group> <node>     - 手动切换 Selector 代理组
  ai-status                 - 查看 AI 专用路由状态

命令级代理:
  proxy-env                 - 输出命令级代理环境变量
  with-proxy <cmd...>       - 仅为单条命令注入代理环境
  proxy-shell               - 打开临时代理 shell

诊断与排障:
  test-group <group>        - 测试代理组或节点健康情况
  test                      - 测试代理连通性

环境变量:
  PROG_PATH                 - mihomo 可执行文件路径
  CONFIG_DIR                - 配置目录
  SOURCE_CONFIG_FILE        - 原始配置文件路径
  CONFIG_FILE               - 原始配置文件路径（兼容旧变量）
  RUNTIME_CONFIG_FILE       - 运行配置文件路径
  DEFAULT_PROXY_PORT        - 默认代理端口（兜底）
  DEFAULT_CONTROLLER_ADDR   - 默认控制接口地址（兜底）
  START_TIMEOUT             - 启动超时秒数
  TEST_URL                  - 健康检查 URL
  TEST_TIMEOUT              - 健康检查超时毫秒
  PROXY_NO_PROXY            - 覆盖默认 NO_PROXY 列表

示例:
  # 配置与进程
  $0 render
  $0 start

  # AI 路由控制
  $0 list-groups
  $0 list-nodes "$AI_MANUAL_GROUP"
  $0 current "$AI_MANUAL_GROUP"
  $0 switch "$AI_MANUAL_GROUP" "$AI_AUTO_GROUP"
  $0 switch "$AI_REGION_US" "🇺🇸 United States丨02"
  $0 ai-status

  # 命令级代理
  $0 proxy-env
  $0 with-proxy curl https://chatgpt.com
  $0 proxy-shell

  # 诊断与排障
  $0 test-group "$AI_AUTO_GROUP"
  $0 test

EOF
}

main() {
    local command="${1:-}"

    case "$command" in
        start)
            start
            ;;
        stop)
            stop
            ;;
        restart)
            restart
            ;;
        status)
            shift
            status "$@"
            ;;
        logs)
            logs
            ;;
        test)
            test
            ;;
        render)
            render
            ;;
        list-groups)
            shift
            list_groups "$@"
            ;;
        list-nodes)
            shift
            list_nodes "$1" "$2"
            ;;
        current)
            shift
            current_group "$1" "$2"
            ;;
        switch)
            shift
            switch_group "$1" "$2"
            ;;
        ai-status)
            shift
            ai_status "$@"
            ;;
        test-group)
            shift
            test_group "$1" "$2"
            ;;
        proxy-env)
            proxy_env
            ;;
        with-proxy)
            shift
            with_proxy "$@"
            ;;
        proxy-shell)
            proxy_shell
            ;;
        ""|-h|--help|help)
            usage
            exit 0
            ;;
        *)
            print_error "错误: 未知命令 '$command'"
            echo ""
            usage
            exit 1
            ;;
    esac
}

main "$@"
