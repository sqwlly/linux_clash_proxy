#!/bin/bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
PREFIX="${PREFIX:-/usr/local}"
BINDIR="${BINDIR:-${PREFIX}/bin}"
LIBDIR="${LIBDIR:-${PREFIX}/lib/clash-proxy}"
DRY_RUN=0
WITH_CPROXY_ALIAS=0

usage() {
    cat <<EOF
Usage: $0 [--dry-run] [--prefix PATH] [--bindir PATH] [--libdir PATH] [--with-cproxy-alias]

Install root-level Clash Proxy command wrappers:
  clash-proxy         -> ../lib/clash-proxy/proxy.sh
  clash-proxy-update  -> ../lib/clash-proxy/update_config.sh

Options:
  --dry-run             Print actions without writing files.
  --prefix PATH         Install under PATH/bin. Default: /usr/local.
  --bindir PATH         Install directly into PATH. Overrides --prefix.
  --libdir PATH         Install script payloads into PATH. Default: PREFIX/lib/clash-proxy.
  --with-cproxy-alias   Also install cproxy and cproxy-update aliases.
  -h, --help            Show this help.
EOF
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --dry-run)
            DRY_RUN=1
            shift
            ;;
        --prefix)
            if [ "$#" -lt 2 ]; then
                echo "错误: --prefix 需要路径参数" >&2
                exit 2
            fi
            PREFIX="$2"
            BINDIR="${PREFIX}/bin"
            LIBDIR="${PREFIX}/lib/clash-proxy"
            shift 2
            ;;
        --bindir)
            if [ "$#" -lt 2 ]; then
                echo "错误: --bindir 需要路径参数" >&2
                exit 2
            fi
            BINDIR="$2"
            shift 2
            ;;
        --libdir)
            if [ "$#" -lt 2 ]; then
                echo "错误: --libdir 需要路径参数" >&2
                exit 2
            fi
            LIBDIR="$2"
            shift 2
            ;;
        --with-cproxy-alias)
            WITH_CPROXY_ALIAS=1
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "错误: 未知参数: $1" >&2
            usage >&2
            exit 2
            ;;
    esac
done

require_file() {
    local path="$1"
    if [ ! -f "$path" ]; then
        echo "错误: 缺少必要文件: $path" >&2
        exit 1
    fi
}

relative_to_bindir() {
    local target="$1"
    python3 - "$target" "$BINDIR" <<'PY'
import os
import sys

target = os.path.abspath(sys.argv[1])
bindir = os.path.abspath(sys.argv[2])
print(os.path.relpath(target, bindir))
PY
}

require_cmd() {
    local name="$1"
    if ! command -v "$name" >/dev/null 2>&1; then
        echo "错误: 缺少依赖命令: $name" >&2
        exit 1
    fi
}

write_wrapper() {
    local command_name="$1"
    local target="$2"
    local destination="${BINDIR}/${command_name}"
    local relative_target
    local project_dir_env=""
    local temp_file

    relative_target="$(relative_to_bindir "$target")"
    if [ "$command_name" = "clash-proxy-update" ] || [ "$command_name" = "cproxy-update" ]; then
        project_dir_env="PROJECT_DIR=\"${PROJECT_DIR}\" "
    fi

    if [ "$DRY_RUN" -eq 1 ]; then
        echo "DRY-RUN install ${command_name} -> $(basename "$target")"
        return
    fi

    temp_file="$(mktemp)"
    {
        printf '%s\n' '#!/bin/sh'
        printf '%s\n' 'WRAPPER_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"'
        printf '%sCLASH_PROXY_CLI_NAME="%s" exec "${WRAPPER_DIR}/%s" "$@"\n' "$project_dir_env" "$command_name" "$relative_target"
    } > "$temp_file"

    install -m 755 "$temp_file" "$destination"
    rm -f "$temp_file"
    echo "installed ${command_name} -> $(basename "$target")"
}

install_payload() {
    local source="$1"
    local name="$2"
    local destination="${LIBDIR}/${name}"

    if [ "$DRY_RUN" -eq 1 ]; then
        echo "DRY-RUN install payload ${name}"
        return
    fi

    install -m 755 "$source" "$destination"
    echo "installed payload ${name}"
}

warn_shadowed_command() {
    local command_name="$1"
    local destination="${BINDIR}/${command_name}"
    local resolved

    resolved="$(command -v "$command_name" 2>/dev/null || true)"
    if [ -n "$resolved" ] && [ "$resolved" != "$destination" ]; then
        echo "警告: 当前 PATH 中 ${command_name} 解析为 ${resolved}" >&2
        echo "提示: 如需立即使用 ${destination}，可运行 hash -r 后重试，或直接执行完整路径。" >&2
    fi
}

main() {
    require_file "${PROJECT_DIR}/proxy.sh"
    require_file "${PROJECT_DIR}/update_config.sh"
    require_cmd install
    require_cmd mktemp
    require_cmd python3

    if [ "$DRY_RUN" -eq 0 ]; then
        install -d -m 755 "$BINDIR"
        install -d -m 755 "$LIBDIR"
    fi

    install_payload "${PROJECT_DIR}/proxy.sh" "proxy.sh"
    install_payload "${PROJECT_DIR}/update_config.sh" "update_config.sh"

    write_wrapper "clash-proxy" "${LIBDIR}/proxy.sh"
    write_wrapper "clash-proxy-update" "${LIBDIR}/update_config.sh"

    if [ "$WITH_CPROXY_ALIAS" -eq 1 ]; then
        write_wrapper "cproxy" "${LIBDIR}/proxy.sh"
        write_wrapper "cproxy-update" "${LIBDIR}/update_config.sh"
        warn_shadowed_command "cproxy"
        warn_shadowed_command "cproxy-update"
    fi

    cat <<EOF
系统命令安装完成:
- clash-proxy
- clash-proxy-update
EOF

    if [ "$WITH_CPROXY_ALIAS" -eq 1 ]; then
        cat <<EOF
- cproxy
- cproxy-update
EOF
    fi

    cat <<EOF

常用命令:
  clash-proxy status
  clash-proxy status --raw
  clash-proxy-update --dry-run config_1.yaml
  clash-proxy-update --apply config_1.yaml
EOF
}

main "$@"
