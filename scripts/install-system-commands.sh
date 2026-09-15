#!/bin/bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
PREFIX="${PREFIX:-/usr/local}"
BINDIR="${BINDIR:-${PREFIX}/bin}"
LIBDIR="${LIBDIR:-${PREFIX}/lib/clash-proxy}"
DEFAULT_TMPDIR="${DEFAULT_TMPDIR:-${PROJECT_DIR}/.tmp}"
DRY_RUN=0
WITH_CPROXY_ALIAS=0
WITH_LEGACY=0

usage() {
    cat <<EOF
Usage: $0 [--with-legacy] [--dry-run] [--prefix PATH] [--bindir PATH] [--libdir PATH] [--with-cproxy-alias]

安装 legacy 命令 wrapper：
  clash-proxy         -> ../lib/clash-proxy/proxy.sh
  clash-proxy-update  -> ../lib/clash-proxy/update_config.sh

**默认不安装**：这两个入口已于 2026-09-11 停用待退役。不加 --with-legacy 时本脚本
只打印说明就退出——这样将来任何一次重跑都不会把已停用的入口装回 PATH。
用户级 cproxy 由 pip / pipx 安装，与本脚本无关。

Options:
  --with-legacy         实际安装 legacy wrapper（默认只打印说明）。
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
        --with-legacy)
            WITH_LEGACY=1
            shift
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
    local legacy="${3:-1}"
    local destination="${BINDIR}/${command_name}"
    local relative_target
    local project_dir_env=""
    local warning_line=""
    local warned_env=""
    local temp_file

    relative_target="$(relative_to_bindir "$target")"
    if [ "$command_name" = "clash-proxy-update" ] || [ "$command_name" = "cproxy-update" ]; then
        project_dir_env="PROJECT_DIR=\"${PROJECT_DIR}\" "
    fi

    # legacy 入口每次调用都往 stderr 打一条弃用警告：走 stderr 是为了不污染
    # stdout，`clash-proxy status --raw` 之类的脚本消费仍可正常解析
    if [ "$legacy" -eq 1 ]; then
        local hint="请改用 cproxy 等价命令（cproxy --help 查看）"
        case "$command_name" in
            *update) hint="订阅与配置更新请改用 cproxy refresh" ;;
        esac
        warning_line="printf '%s\\n' '警告: ${command_name} 是已停用的 legacy 入口，${hint}。' >&2"
        # 标记已警告：proxy.sh 本体也会在直接调用时打警告，靠这个变量去重，
        # 避免经 wrapper 调用时同一句话打两遍
        warned_env="CPROXY_LEGACY_WARNED=1 "
    fi

    if [ "$DRY_RUN" -eq 1 ]; then
        echo "DRY-RUN install ${command_name} -> $(basename "$target")"
        return
    fi

    temp_file="$(mktemp)"
    {
        printf '%s\n' '#!/bin/sh'
        printf '%s\n' 'WRAPPER_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"'
        printf '%s\n' "DEFAULT_TMPDIR=\"${DEFAULT_TMPDIR}\""
        printf '%s\n' 'if [ -z "${TMPDIR:-}" ]; then'
        printf '%s\n' '    mkdir -p "$DEFAULT_TMPDIR" 2>/dev/null || true'
        printf '%s\n' '    TMPDIR="$DEFAULT_TMPDIR"'
        printf '%s\n' '    export TMPDIR'
        printf '%s\n' 'fi'
        if [ -n "$warning_line" ]; then
            printf '%s\n' "$warning_line"
        fi
        printf '%sCLASH_PROXY_CLI_NAME="%s" %sexec "${WRAPPER_DIR}/%s" "$@"\n' "$project_dir_env" "$command_name" "$warned_env" "$relative_target"
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
    if [ "$WITH_LEGACY" -eq 0 ]; then
        cat <<'EOF'
legacy 命令已于 2026-09-11 停用，不再默认安装。

这些入口（clash-proxy / clash-proxy-update）已由 cproxy 用户级链路取代，
保留仅为回滚对照。确需安装时加 --with-legacy：

  ./scripts/install-system-commands.sh --with-legacy

用户级 cproxy 由 pip / pipx 安装，与本脚本无关。
EOF
        exit 0
    fi

    require_file "${PROJECT_DIR}/proxy.sh"
    require_file "${PROJECT_DIR}/update_config.sh"
    require_file "${PROJECT_DIR}/scripts/import_subscription.py"
    require_file "${PROJECT_DIR}/scripts/probe_stable_node.py"
    require_file "${PROJECT_DIR}/scripts/probe_history.py"
    require_file "${PROJECT_DIR}/scripts/progress.py"
    require_file "${PROJECT_DIR}/scripts/ai_tools.py"
    require_cmd install
    require_cmd mktemp
    require_cmd python3

    if [ "$DRY_RUN" -eq 0 ]; then
        install -d -m 755 "$BINDIR"
        install -d -m 755 "$LIBDIR"
        install -d -m 755 "$DEFAULT_TMPDIR"
    fi

    install_payload "${PROJECT_DIR}/proxy.sh" "proxy.sh"
    install_payload "${PROJECT_DIR}/update_config.sh" "update_config.sh"
    install_payload "${PROJECT_DIR}/scripts/import_subscription.py" "import_subscription.py"
    install_payload "${PROJECT_DIR}/scripts/probe_stable_node.py" "probe_stable_node.py"
    install_payload "${PROJECT_DIR}/scripts/probe_history.py" "probe_history.py"
    install_payload "${PROJECT_DIR}/scripts/progress.py" "progress.py"
    install_payload "${PROJECT_DIR}/scripts/ai_tools.py" "ai_tools.py"

    write_wrapper "clash-proxy" "${LIBDIR}/proxy.sh"
    write_wrapper "clash-proxy-update" "${LIBDIR}/update_config.sh"
    # `cproxy-update` 名字形似 cproxy 子命令，实际转发到 legacy 的 update_config.sh；
    # 它属于 legacy 入口，所以随 --with-legacy 一起装，并带上弃用警告
    write_wrapper "cproxy-update" "${LIBDIR}/update_config.sh"

    if [ "$WITH_CPROXY_ALIAS" -eq 1 ]; then
        # 保护：`cproxy` 通常由 pip / pipx 安装（真正的用户级 CLI）。把它覆盖成指向
        # proxy.sh 的 wrapper 会让 cproxy 命令直接失效——这是不可逆的破坏，必须拦住。
        if [ -e "${BINDIR}/cproxy" ] && ! grep -q "lib/clash-proxy/proxy.sh" "${BINDIR}/cproxy" 2>/dev/null; then
            echo "错误: ${BINDIR}/cproxy 已存在，且不是本脚本安装的 legacy alias。" >&2
            echo "提示: 覆盖它会让 cproxy 命令失效。如确需覆盖，请先备份并手动移除该文件。" >&2
            exit 1
        fi
        write_wrapper "cproxy" "${LIBDIR}/proxy.sh"
        warn_shadowed_command "cproxy"
        warn_shadowed_command "cproxy-update"
    fi

    cat <<EOF
legacy 命令安装完成（已停用，仅供回滚对照）:
- clash-proxy
- clash-proxy-update

这两个 wrapper 每次调用都会向 stderr 打印弃用警告，指向 cproxy 等价命令。
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
