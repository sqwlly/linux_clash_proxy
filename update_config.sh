#!/bin/bash
# Safely validate and apply a new source config. AI routing is rendered by proxy.sh.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="${PROJECT_DIR:-$SCRIPT_DIR}"
CONFIG_FILE="${CONFIG_FILE:-$PROJECT_DIR/config.yaml}"
CANDIDATE_FILE="${CANDIDATE_FILE:-$PROJECT_DIR/config_1.yaml}"
REFRESH_SCRIPT="${REFRESH_SCRIPT:-$PROJECT_DIR/systemd/clash-proxy-refresh.sh}"
PROXY_SH="${PROXY_SH:-$PROJECT_DIR/proxy.sh}"
RUNTIME_CONFIG="${RUNTIME_CONFIG:-$PROJECT_DIR/runtime.yaml}"

MODE="dry-run"
TMP_CONFIG=""
BACKUP_CONFIG=""
HAD_CONFIG=0

usage() {
    cat <<EOF
Usage: $0 [--dry-run|--apply] [candidate-config]

Options:
  --dry-run          Validate candidate config only. This is the default.
  --apply            Apply candidate config and run the refresh script.
  -h, --help         Show this help.

Environment overrides:
  PROJECT_DIR        Project directory. Default: script directory.
  CONFIG_FILE        Source config to replace. Default: PROJECT_DIR/config.yaml.
  CANDIDATE_FILE     Candidate config. Default: PROJECT_DIR/config_1.yaml.
  REFRESH_SCRIPT     Refresh script to run after apply.
  PROXY_SH           Passed through to the refresh script.
  RUNTIME_CONFIG     Passed through to the refresh script.
EOF
}

die() {
    echo "错误: $*" >&2
    exit 1
}

cleanup() {
    if [ -n "$TMP_CONFIG" ] && [ -f "$TMP_CONFIG" ]; then
        rm -f "$TMP_CONFIG"
    fi
    if [ -n "$BACKUP_CONFIG" ] && [ -f "$BACKUP_CONFIG" ]; then
        rm -f "$BACKUP_CONFIG"
    fi
}
trap cleanup EXIT

parse_args() {
    local candidate_arg_seen=0

    while [ "$#" -gt 0 ]; do
        case "$1" in
            --dry-run)
                MODE="dry-run"
                shift
                ;;
            --apply)
                MODE="apply"
                shift
                ;;
            -h|--help)
                usage
                exit 0
                ;;
            --)
                shift
                if [ "$#" -gt 1 ]; then
                    die "只能提供一个候选配置文件"
                fi
                if [ "$#" -eq 1 ]; then
                    if [ "$candidate_arg_seen" -eq 1 ]; then
                        die "只能提供一个候选配置文件"
                    fi
                    CANDIDATE_FILE="$1"
                fi
                break
                ;;
            -*)
                die "未知参数: $1"
                ;;
            *)
                if [ "$candidate_arg_seen" -eq 1 ]; then
                    die "只能提供一个候选配置文件"
                fi
                CANDIDATE_FILE="$1"
                candidate_arg_seen=1
                shift
                ;;
        esac
    done
}

validate_paths() {
    [ -f "$CANDIDATE_FILE" ] || die "候选配置不存在: $CANDIDATE_FILE"
    [ -r "$CANDIDATE_FILE" ] || die "候选配置不可读: $CANDIDATE_FILE"

    if [ "$MODE" = "apply" ]; then
        [ -f "$REFRESH_SCRIPT" ] || die "refresh 脚本不存在: $REFRESH_SCRIPT"
        [ -x "$REFRESH_SCRIPT" ] || die "refresh 脚本不可执行: $REFRESH_SCRIPT"
        [ -f "$PROXY_SH" ] || die "proxy.sh 不存在: $PROXY_SH"
    fi
}

validate_candidate_yaml() {
    python3 - "$CANDIDATE_FILE" <<'PY'
import sys
import yaml

config_path = sys.argv[1]

try:
    with open(config_path, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
except Exception as exc:
    raise SystemExit(f"YAML 解析失败: {exc}")

if not isinstance(data, dict):
    raise SystemExit("顶层 YAML 必须是对象")

required_lists = ("proxies", "proxy-groups", "rules")
for key in required_lists:
    if key not in data:
        raise SystemExit(f"缺少必需字段: {key}")
    if not isinstance(data[key], list):
        raise SystemExit(f"{key} 必须是列表")
PY
}

backup_current_config() {
    if [ ! -f "$CONFIG_FILE" ]; then
        HAD_CONFIG=0
        return 0
    fi

    BACKUP_CONFIG="$(mktemp "${CONFIG_FILE}.bak.XXXXXX")"
    cp "$CONFIG_FILE" "$BACKUP_CONFIG"
    HAD_CONFIG=1
}

install_candidate_config() {
    local config_dir

    config_dir="$(dirname "$CONFIG_FILE")"
    mkdir -p "$config_dir"
    TMP_CONFIG="$(mktemp "${CONFIG_FILE}.candidate.XXXXXX")"
    cp "$CANDIDATE_FILE" "$TMP_CONFIG"

    mv "$TMP_CONFIG" "$CONFIG_FILE"
    TMP_CONFIG=""
}

restore_config() {
    if [ "$HAD_CONFIG" -eq 1 ] && [ -n "$BACKUP_CONFIG" ] && [ -f "$BACKUP_CONFIG" ]; then
        cp "$BACKUP_CONFIG" "$CONFIG_FILE"
    elif [ "$HAD_CONFIG" -eq 0 ]; then
        rm -f "$CONFIG_FILE"
    fi
}

run_refresh() {
    PROJECT_DIR="$PROJECT_DIR" \
    CONFIG_FILE="$CONFIG_FILE" \
    SOURCE_CONFIG_FILE="$CONFIG_FILE" \
    PROXY_SH="$PROXY_SH" \
    RUNTIME_CONFIG="$RUNTIME_CONFIG" \
    bash "$REFRESH_SCRIPT"
}

apply_candidate() {
    backup_current_config
    install_candidate_config

    if ! run_refresh; then
        restore_config
        echo "错误: refresh 失败，已恢复原始源配置: $CONFIG_FILE" >&2
        return 1
    fi
}

parse_args "$@"
validate_paths
validate_candidate_yaml

if [ "$MODE" = "dry-run" ]; then
    echo "dry-run: 候选配置通过 YAML 校验，未修改源配置: $CANDIDATE_FILE"
    exit 0
fi

apply_candidate
echo "apply: 候选配置已写入并完成安全刷新: $CONFIG_FILE"
