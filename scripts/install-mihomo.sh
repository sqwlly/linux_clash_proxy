#!/bin/bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PREFIX="${PREFIX:-/usr/local}"
BINDIR="${BINDIR:-${PREFIX}/bin}"

# 固定的 mihomo 版本与哈希（按架构分别固定）。升级时修改这些常量；
# 新版本的哈希可通过 bash scripts/install-mihomo.sh --fetch-hash <version> 获取。
MIHOMO_VERSION_PIN="v1.19.28"
MIHOMO_SHA256_PIN_AMD64="d5967e079d9f793515a5a8193aabda455f7e012427eccd567dbc4f2f15498204"
MIHOMO_SHA256_PIN_ARM64="2474450cd1c41dfa53036a54a4e85579f493d3af524d86c3d4b8e2b240b56cd2"
BASE_URL="${MIHOMO_BASE_URL:-https://github.com/MetaCubeX/mihomo/releases/download}"

DRY_RUN=0
FORCE=0
ACTION="install"
FETCH_VERSION=""
CLEANUP_DIR=""
STAGED_FILE=""

cleanup() {
    if [ -n "$CLEANUP_DIR" ]; then
        rm -rf "$CLEANUP_DIR"
    fi
    if [ -n "$STAGED_FILE" ]; then
        rm -f "$STAGED_FILE"
    fi
}
trap cleanup EXIT

usage() {
    cat <<EOF
Usage: $0 [--check] [--fetch-hash VERSION] [--force] [--dry-run] [--bindir PATH]

安装固定版本的 mihomo 二进制，下载后强制 sha256 校验。

Options:
  --check               只检查已安装版本是否与固定版本一致（不一致时退出码 1）。
  --fetch-hash VERSION  下载指定版本并打印应写入脚本的版本/哈希常量，不安装。
  --force               即使已安装相同版本也重新安装。
  --dry-run             打印将执行的动作，不下载不写入。
  --bindir PATH         安装到 PATH。默认: /usr/local/bin。
  -h, --help            显示本帮助。

环境变量:
  MIHOMO_VERSION   覆盖固定版本（此时必须同时设置 MIHOMO_SHA256）。
  MIHOMO_SHA256    覆盖固定哈希。
EOF
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --check)
            ACTION="check"
            shift
            ;;
        --fetch-hash)
            if [ "$#" -lt 2 ]; then
                echo "错误: --fetch-hash 需要版本参数（例如 v1.19.28）" >&2
                exit 1
            fi
            ACTION="fetch-hash"
            FETCH_VERSION="$2"
            shift 2
            ;;
        --force)
            FORCE=1
            shift
            ;;
        --dry-run)
            DRY_RUN=1
            shift
            ;;
        --bindir)
            if [ "$#" -lt 2 ]; then
                echo "错误: --bindir 需要路径参数" >&2
                exit 1
            fi
            BINDIR="$2"
            shift 2
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "错误: 未知参数: $1" >&2
            usage >&2
            exit 1
            ;;
    esac
done

require_cmd() {
    if ! command -v "$1" >/dev/null 2>&1; then
        echo "错误: 缺少依赖命令: $1" >&2
        exit 1
    fi
}

detect_arch() {
    case "$(uname -m)" in
        x86_64) echo "amd64" ;;
        aarch64) echo "arm64" ;;
        *)
            echo "错误: 不支持的架构: $(uname -m)" >&2
            exit 1
            ;;
    esac
}

installed_version() {
    local bin="${BINDIR}/mihomo"
    [ -x "$bin" ] || return 1
    "$bin" -v 2>/dev/null | sed -n 's/^Mihomo Meta \(v[^ ]*\).*/\1/p' | head -1
}

download_asset() {
    local version="$1" dest="$2" arch
    arch="$(detect_arch)"
    curl -fsSL --max-time 600 -o "$dest" "${BASE_URL}/${version}/mihomo-linux-${arch}-${version}.gz"
}

resolve_version_and_sha() {
    VERSION="${MIHOMO_VERSION:-$MIHOMO_VERSION_PIN}"
    local arch pin_sha
    arch="$(detect_arch)"
    case "$arch" in
        amd64) pin_sha="$MIHOMO_SHA256_PIN_AMD64" ;;
        arm64) pin_sha="$MIHOMO_SHA256_PIN_ARM64" ;;
    esac
    SHA256="${MIHOMO_SHA256:-$pin_sha}"
    if [ "$VERSION" != "$MIHOMO_VERSION_PIN" ] && [ -z "${MIHOMO_SHA256:-}" ]; then
        echo "错误: 自定义 MIHOMO_VERSION 时必须同时设置 MIHOMO_SHA256" >&2
        exit 1
    fi
}

action_check() {
    resolve_version_and_sha
    local current
    current="$(installed_version || true)"
    if [ -z "$current" ]; then
        echo "mihomo 未安装（期望版本: ${VERSION}）"
        return 1
    fi
    if [ "$current" = "$VERSION" ]; then
        echo "mihomo 版本一致: ${current}"
        return 0
    fi
    echo "mihomo 版本不一致: 已安装 ${current}，期望 ${VERSION}"
    return 1
}

action_fetch_hash() {
    require_cmd curl
    CLEANUP_DIR="$(mktemp -d)"
    echo "下载 ${FETCH_VERSION} 资产以计算哈希..." >&2
    download_asset "$FETCH_VERSION" "${CLEANUP_DIR}/mihomo.gz"
    local hash arch_upper
    hash="$(sha256sum "${CLEANUP_DIR}/mihomo.gz" | awk '{print $1}')"
    arch_upper="$(detect_arch | tr '[:lower:]' '[:upper:]')"
    cat <<EOF
请将 scripts/install-mihomo.sh 中的常量更新为:
MIHOMO_VERSION_PIN="${FETCH_VERSION}"
MIHOMO_SHA256_PIN_${arch_upper}="${hash}"
提示: 写入前请与 GitHub release 页面或另一网络环境交叉核对哈希，避免单点信任。
EOF
}

action_install() {
    resolve_version_and_sha
    require_cmd curl

    local current
    current="$(installed_version || true)"
    if [ "$current" = "$VERSION" ] && [ "$FORCE" = "0" ]; then
        echo "mihomo 已是 ${VERSION}，跳过安装"
        return 0
    fi

    if [ "$DRY_RUN" = "1" ]; then
        echo "[dry-run] 将安装 mihomo ${VERSION}（当前: ${current:-未安装}）到 ${BINDIR}/mihomo"
        echo "[dry-run] 下载地址: ${BASE_URL}/${VERSION}/mihomo-linux-$(detect_arch)-${VERSION}.gz"
        echo "[dry-run] 期望 sha256: ${SHA256}"
        return 0
    fi

    CLEANUP_DIR="$(mktemp -d)"

    echo "下载 mihomo ${VERSION}..."
    download_asset "$VERSION" "${CLEANUP_DIR}/mihomo.gz"

    local actual
    actual="$(sha256sum "${CLEANUP_DIR}/mihomo.gz" | awk '{print $1}')"
    if [ "$actual" != "$SHA256" ]; then
        echo "错误: sha256 校验失败" >&2
        echo "  期望: ${SHA256}" >&2
        echo "  实际: ${actual}" >&2
        exit 1
    fi
    echo "sha256 校验通过"

    gunzip -c "${CLEANUP_DIR}/mihomo.gz" > "${CLEANUP_DIR}/mihomo"
    chmod 0755 "${CLEANUP_DIR}/mihomo"
    if ! "${CLEANUP_DIR}/mihomo" -v >/dev/null 2>&1; then
        echo "错误: 下载的 mihomo 无法在当前机器运行" >&2
        echo "提示: 若是较老 CPU，可手动下载 mihomo-linux-amd64-compatible 构建，并用 MIHOMO_SHA256 指定校验值安装" >&2
        exit 1
    fi

    if [ ! -d "$BINDIR" ]; then
        echo "错误: 目录不存在: ${BINDIR}" >&2
        exit 1
    fi
    if [ ! -w "$BINDIR" ]; then
        echo "错误: 无权限写入 ${BINDIR}，请使用 sudo 运行" >&2
        exit 1
    fi

    # 先写入临时文件再 rename，避免覆盖正在运行的二进制导致 ETXTBSY
    STAGED_FILE="$(mktemp "${BINDIR}/.mihomo.new.XXXXXX")"
    cp "${CLEANUP_DIR}/mihomo" "$STAGED_FILE"
    chmod 0755 "$STAGED_FILE"
    mv -f "$STAGED_FILE" "${BINDIR}/mihomo"
    STAGED_FILE=""

    echo "mihomo 已安装: ${current:-未安装} -> $("${BINDIR}/mihomo" -v | head -1)"
    if pidof mihomo >/dev/null 2>&1; then
        echo "提示: mihomo 正在运行，重启服务后生效（systemctl restart clash-proxy 或 cproxy restart）"
    fi
}

case "$ACTION" in
    check) action_check ;;
    fetch-hash) action_fetch_hash ;;
    install) action_install ;;
esac
