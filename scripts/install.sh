#!/bin/bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
INSTALL_MODE=""
TMP_MMDB=""

cleanup_tmp() {
    if [ -n "${TMP_MMDB:-}" ]; then
        rm -f "$TMP_MMDB"
    fi
}
trap cleanup_tmp EXIT

package_target() {
    if [ "${CPROXY_INSTALL_TUI:-1}" = "0" ]; then
        printf '%s\n' "$ROOT_DIR"
    else
        printf '%s[tui]\n' "$ROOT_DIR"
    fi
}

require_cmd() {
    local name="$1"
    if ! command -v "$name" >/dev/null 2>&1; then
        echo "错误: 缺少依赖命令: $name" >&2
        exit 1
    fi
}

install_with_pipx() {
    local target
    target="$(package_target)"
    if [ "${CPROXY_EDITABLE:-1}" = "0" ]; then
        pipx install --force "$target"
    else
        pipx install --force --editable "$target"
    fi
}

install_with_pip() {
    local target
    target="$(package_target)"
    if [ "${CPROXY_EDITABLE:-1}" = "0" ]; then
        python3 -m pip install --user "$target"
    else
        python3 -m pip install --user --editable "$target"
    fi
}

install_runtime_dependencies() {
    local specs=("PyYAML>=6" "requests>=2" "tqdm>=4" "urllib3>=2.7.0" "idna>=3.15" "h2>=4" "cryptography>=42")
    if [ "${CPROXY_INSTALL_TUI:-1}" != "0" ]; then
        specs+=("textual>=5.0")
    fi
    # 不带 --upgrade：已满足约束的系统包保持原版本，只补缺失或不满足最低版本的依赖。
    python3 -m pip install "${specs[@]}"
}

# root 下的安装路径。刻意不带 --user：root 的 --user 会装到 /root/.local，而
# PATH 里 ~/.local/bin 排在 /usr/local/bin 之前，于是交互命令走用户级副本、
# systemd 服务走系统级副本（unit 硬编码 /usr/local/bin/cproxy），两者可能跑
# 不同版本的代码。统一装到 /usr/local 可根除这种分叉。
install_system_wide() {
    local target
    target="$(package_target)"
    install_runtime_dependencies
    if [ "${CPROXY_EDITABLE:-1}" = "0" ]; then
        # 依赖已按最低版本单独检查；这里只替换项目本体，不重复重装系统包。
        python3 -m pip install --force-reinstall --no-deps "$target"
    else
        python3 -m pip install --no-deps --editable "$target"
    fi
}

run_cproxy() {
    local pipx_venv_py="${PIPX_HOME:-$HOME/.local/pipx}/venvs/cproxy/bin/python"
    local pipx_bin="${PIPX_BIN_DIR:-$HOME/.local/bin}/cproxy"
    local python_dir
    python_dir="$(cd "$(dirname -- "$(command -v python3)")" && pwd)"

    if [ "$INSTALL_MODE" = "pipx" ]; then
        if [ -x "$pipx_venv_py" ]; then
            "$pipx_venv_py" -m cproxy.cli "$@"
            return $?
        fi
        if [ -x "$pipx_bin" ]; then
            "$pipx_bin" "$@"
            return $?
        fi
    elif [ "$INSTALL_MODE" = "user" ]; then
        if [ -x "${HOME}/.local/bin/cproxy" ]; then
            "${HOME}/.local/bin/cproxy" "$@"
            return $?
        fi
    elif [ "$INSTALL_MODE" = "system" ]; then
        if [ -x "${python_dir}/cproxy" ]; then
            "${python_dir}/cproxy" "$@"
            return $?
        fi
    fi

    PYTHONPATH="${ROOT_DIR}/src${PYTHONPATH:+:${PYTHONPATH}}" \
        python3 -m cproxy.cli "$@"
}

ensure_geodata() {
    local data_home="${XDG_DATA_HOME:-$HOME/.local/share}"
    local mmdb_path="${data_home}/cproxy/country.mmdb"
    local bundled_mmdb="${ROOT_DIR}/Country.mmdb"
    local mmdb_url="${CPROXY_GEODATA_URL:-https://github.com/MetaCubeX/meta-rules-dat/releases/download/latest/country.mmdb}"
    if [ -f "$mmdb_path" ]; then
        echo "GeoIP 数据: ${mmdb_path}"
        return 0
    fi

    mkdir -p "${data_home}/cproxy"

    # 仓库根目录的 Country.mmdb 已不入库；本机遗留副本仍优先复用
    if [ -f "$bundled_mmdb" ]; then
        cp "$bundled_mmdb" "$mmdb_path"
        echo "GeoIP 数据: 已从 ${bundled_mmdb} 安装到 ${mmdb_path}"
        return 0
    fi

    if [ "${CPROXY_GEODATA_DOWNLOAD:-1}" != "0" ]; then
        TMP_MMDB="${mmdb_path}.tmp.$$"
        local downloaded=0
        if command -v curl >/dev/null 2>&1; then
            curl -fsSL --max-time 120 -o "$TMP_MMDB" "$mmdb_url" && downloaded=1
        fi
        if [ "$downloaded" = "0" ] && command -v wget >/dev/null 2>&1; then
            wget -q -O "$TMP_MMDB" "$mmdb_url" && downloaded=1
        fi
        # 下载到临时文件并做基本完整性检查（country.mmdb 正常为数 MB），
        # 通过后原子替换，避免中断产生的残缺文件被下次运行当成有效数据
        if [ "$downloaded" = "1" ] && [ "$(stat -c %s "$TMP_MMDB" 2>/dev/null || echo 0)" -gt 1000000 ]; then
            mv -f "$TMP_MMDB" "$mmdb_path"
            TMP_MMDB=""
            echo "GeoIP 数据: 已从 ${mmdb_url} 下载到 ${mmdb_path}"
            return 0
        fi
        rm -f "$TMP_MMDB"
        TMP_MMDB=""
    fi

    echo "警告: 未检测到 GeoIP 数据文件: ${mmdb_path}" >&2
    echo "提示: 在无代理或受限网络环境下，mihomo 可能无法自动获取 country.mmdb" >&2
    echo "提示: 可手动放置该文件后再运行 cproxy test" >&2
}

ensure_ruleset() {
    local data_home="${XDG_DATA_HOME:-$HOME/.local/share}"
    local ruleset_dir="${data_home}/cproxy/ruleset"
    local dst="${ruleset_dir}/ChinaMax.yml"
    local src="${ROOT_DIR}/ruleset/ChinaMax.yml"

    if [ -f "$dst" ]; then
        echo "规则集数据: ${dst}"
        return 0
    fi
    if [ -f "$src" ]; then
        mkdir -p "$ruleset_dir"
        cp "$src" "$dst"
        echo "规则集数据: 已从 ${src} 安装到 ${dst}"
    fi
}

add_logrotate_cron() {
    local config_path="$1"
    local pattern="$2"
    local state_home="${XDG_STATE_HOME:-$HOME/.local/state}"
    local cron_job
    if [ "$(id -u)" = "0" ]; then
        cron_job="0 */6 * * * /usr/sbin/logrotate '${config_path}' >/dev/null 2>&1"
    else
        mkdir -p "${state_home}/cproxy"
        cron_job="0 */6 * * * /usr/sbin/logrotate -s '${state_home}/cproxy/logrotate.status' '${config_path}' >/dev/null 2>&1"
    fi

    if ! command -v crontab >/dev/null 2>&1; then
        echo "警告: 未检测到 crontab，跳过日志轮转定时任务配置" >&2
        return 0
    fi

    if ! crontab -l 2>/dev/null | grep -q "${pattern}"; then
        local existing_cron
        existing_cron="$(crontab -l 2>/dev/null || true)"
        if {
            if [ -n "$existing_cron" ]; then
                printf '%s\n' "$existing_cron" | grep -v "${pattern}" || true
            fi
            printf '%s\n' "$cron_job"
        } | crontab - 2>/dev/null; then
            echo "日志轮转定时任务: 已添加"
        else
            echo "警告: 添加日志轮转定时任务失败，请手动检查 crontab 权限" >&2
        fi
    fi
}

setup_logrotate() {
    local logrotate_dir="${CPROXY_LOGROTATE_DIR:-/etc/logrotate.d}"
    local state_home="${XDG_STATE_HOME:-$HOME/.local/state}"
    local log_file="${state_home}/cproxy/cproxy.log"
    local logrotate_conf="${logrotate_dir}/cproxy"

    if [ ! -f "${ROOT_DIR}/logrotate.conf.template" ]; then
        echo "警告: 未找到 logrotate 模板，跳过日志轮转配置" >&2
        return 0
    fi

    if [ ! -d "${logrotate_dir}" ]; then
        echo "警告: ${logrotate_dir} 不存在，跳过日志轮转配置" >&2
        return 0
    fi

    if [ ! -w "${logrotate_dir}" ]; then
        echo "警告: 无权限写入 ${logrotate_dir}，跳过日志轮转配置" >&2
        return 0
    fi

    CPROXY_LOG_FILE_REPL="${log_file}" awk '{
        repl = ENVIRON["CPROXY_LOG_FILE_REPL"]
        target = "{{LOG_FILE}}"
        while ((idx = index($0, target)) > 0) {
            $0 = substr($0, 1, idx - 1) repl substr($0, idx + length(target))
        }
        print
    }' "${ROOT_DIR}/logrotate.conf.template" > "${logrotate_conf}"

    echo "日志轮转配置: ${logrotate_conf}"

    add_logrotate_cron "${logrotate_conf}" "logrotate.*cproxy"
}

main() {
    require_cmd python3

    # root 下一律走系统级安装，避免交互命令与 systemd 服务分叉到两个副本；
    # 非 root 保持原有 pipx / --user 逻辑不变
    if [ "$(id -u)" = "0" ]; then
        echo "检测到 root：使用系统级安装（/usr/local），与 systemd 服务保持同一副本"
        install_system_wide
        INSTALL_MODE="system"
    elif command -v pipx >/dev/null 2>&1; then
        pipx_bin="$(command -v pipx)"
        python_bin="$(command -v python3)"

        pipx_dir="${pipx_bin%/*}"
        python_dir="${python_bin%/*}"
        first_path_entry="${PATH%%:*}"

        # 当 pipx 与当前 python 同目录，或位于 PATH 的优先目录时，尝试使用 pipx；
        # 否则回退到 pip 安装，避免误用系统级 pipx 造成环境错配。
        if [ "${pipx_dir}" = "${python_dir}" ] || [ "${pipx_dir}" = "${first_path_entry}" ]; then
            if install_with_pipx; then
                INSTALL_MODE="pipx"
            else
                echo "警告: pipx 安装失败，改用用户 pip 安装" >&2
                install_with_pip
                INSTALL_MODE="user"
            fi
        else
            install_with_pip
            INSTALL_MODE="user"
        fi
    else
        install_with_pip
        INSTALL_MODE="user"
    fi

    run_cproxy init >/dev/null

    ensure_geodata
    ensure_ruleset

    setup_logrotate

    if CPROXY_LEGACY_ROOT="${ROOT_DIR}" \
        CPROXY_NONINTERACTIVE=1 \
        run_cproxy bootstrap; then
        echo "一键部署: 完成"
    else
        echo "警告: 一键部署未完成，可稍后手动执行 cproxy bootstrap" >&2
    fi

    echo "安装完成"
    echo "命令入口: cproxy"
    echo "用户配置: ${XDG_CONFIG_HOME:-$HOME/.config}/cproxy/config.yaml"
}

main "$@"
