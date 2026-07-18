#!/bin/bash

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
INSTALLER="${PROJECT_DIR}/scripts/install-mihomo.sh"

TMP_ROOT="$(mktemp -d)"
trap 'rm -rf "$TMP_ROOT"' EXIT

fail() {
    echo "ASSERTION FAILED: $1" >&2
    exit 1
}

assert_contains() {
    local haystack="$1" needle="$2" message="$3"
    if ! grep -Fq "$needle" <<<"$haystack"; then
        echo "ASSERTION FAILED: $message" >&2
        echo "Expected to find: $needle" >&2
        echo "In output: $haystack" >&2
        exit 1
    fi
}

STUB_VERSION="v9.9.9-test"

# 构造假 mihomo（能响应 -v），打包为 .gz 作为下载 fixture
STUB_BIN="${TMP_ROOT}/stub-mihomo"
cat > "$STUB_BIN" <<EOF
#!/bin/bash
if [ "\${1:-}" = "-v" ]; then
    echo "Mihomo Meta ${STUB_VERSION} linux amd64"
    exit 0
fi
exit 0
EOF
chmod 0755 "$STUB_BIN"
gzip -c "$STUB_BIN" > "${TMP_ROOT}/mihomo.gz"
STUB_SHA256="$(sha256sum "${TMP_ROOT}/mihomo.gz" | awk '{print $1}')"

# 假 curl：把 fixture 复制到 -o 指定路径
FAKE_BIN="${TMP_ROOT}/fake-bin"
mkdir -p "$FAKE_BIN"
cat > "${FAKE_BIN}/curl" <<'EOF'
#!/bin/bash
out=""
prev=""
for a in "$@"; do
    if [ "$prev" = "-o" ]; then
        out="$a"
    fi
    prev="$a"
done
if [ -z "$out" ] || [ -z "${FIXTURE_GZ:-}" ] || [ ! -f "${FIXTURE_GZ:-}" ]; then
    exit 1
fi
cp "$FIXTURE_GZ" "$out"
EOF
chmod 0755 "${FAKE_BIN}/curl"

BINDIR="${TMP_ROOT}/bin"
mkdir -p "$BINDIR"

run_installer() {
    env \
        PATH="${FAKE_BIN}:/usr/bin:/bin" \
        FIXTURE_GZ="${TMP_ROOT}/mihomo.gz" \
        BINDIR="$BINDIR" \
        MIHOMO_VERSION="$STUB_VERSION" \
        MIHOMO_SHA256="$STUB_SHA256" \
        bash "$INSTALLER" "$@"
}

echo ">>> 场景 1: 正常安装（下载 + 校验 + 安装）"
output="$(run_installer)"
assert_contains "$output" "sha256 校验通过" "应打印校验通过"
assert_contains "$output" "mihomo 已安装" "应打印安装成功"
[ -x "${BINDIR}/mihomo" ] || fail "mihomo 应被安装到 BINDIR"
"${BINDIR}/mihomo" -v | grep -Fq "$STUB_VERSION" || fail "安装的 mihomo -v 应输出固定版本"

echo ">>> 场景 2: 相同版本跳过安装"
output="$(run_installer)"
assert_contains "$output" "跳过安装" "相同版本应跳过"

echo ">>> 场景 3: --check 版本一致"
run_installer --check >/dev/null || fail "--check 一致时应返回 0"

echo ">>> 场景 4: --check 版本不一致返回 1"
cat > "${BINDIR}/mihomo" <<'EOF'
#!/bin/bash
echo "Mihomo Meta v0.0.1-old linux amd64"
EOF
chmod 0755 "${BINDIR}/mihomo"
if run_installer --check >/dev/null 2>&1; then
    fail "--check 不一致时应返回非 0"
fi

echo ">>> 场景 5: 哈希不匹配拒绝安装"
rm -f "${BINDIR}/mihomo"
if env \
    PATH="${FAKE_BIN}:/usr/bin:/bin" \
    FIXTURE_GZ="${TMP_ROOT}/mihomo.gz" \
    BINDIR="$BINDIR" \
    MIHOMO_VERSION="$STUB_VERSION" \
    MIHOMO_SHA256="0000000000000000000000000000000000000000000000000000000000000000" \
    bash "$INSTALLER" >/dev/null 2>&1; then
    fail "哈希不匹配时应返回非 0"
fi
[ ! -e "${BINDIR}/mihomo" ] || fail "哈希不匹配时不应安装任何文件"

echo ">>> 场景 6: 自定义版本缺少哈希时拒绝"
if env \
    PATH="${FAKE_BIN}:/usr/bin:/bin" \
    BINDIR="$BINDIR" \
    MIHOMO_VERSION="v1.2.3-custom" \
    bash "$INSTALLER" >/dev/null 2>&1; then
    fail "自定义版本但未提供 MIHOMO_SHA256 时应返回非 0"
fi

echo ">>> 场景 7: --dry-run 不下载不写入"
rm -f "${BINDIR}/mihomo"
output="$(run_installer --dry-run)"
assert_contains "$output" "[dry-run]" "dry-run 应打印计划"
[ ! -e "${BINDIR}/mihomo" ] || fail "dry-run 不应安装文件"

echo ">>> 场景 8: --fetch-hash 打印应写入的常量"
output="$(run_installer --fetch-hash v1.2.3)"
assert_contains "$output" "MIHOMO_VERSION_PIN=\"v1.2.3\"" "fetch-hash 应打印版本常量"
assert_contains "$output" "MIHOMO_SHA256_PIN_AMD64=\"${STUB_SHA256}\"" "fetch-hash 应打印当前架构的哈希常量"
assert_contains "$output" "交叉核对" "fetch-hash 应提示交叉核对哈希"

echo ">>> 场景 9: --check 未安装返回 1"
rm -f "${BINDIR}/mihomo"
if run_installer --check >/dev/null 2>&1; then
    fail "--check 未安装时应返回非 0"
fi

echo "mihomo installer 测试通过"
