#!/bin/bash

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPT="${PROJECT_DIR}/proxy.sh"
TMP_DIR="$(mktemp -d)"
SOURCE_CONFIG="${TMP_DIR}/config.yaml"
RUNTIME_CONFIG="${TMP_DIR}/runtime.yaml"
MIHOMO_BIN="${TMP_DIR}/mihomo"

cleanup() {
    rm -rf "$TMP_DIR"
}

trap cleanup EXIT

cat >"$MIHOMO_BIN" <<'EOF'
#!/bin/sh
exit 0
EOF
chmod +x "$MIHOMO_BIN"

# 源配置：无 rule-providers、无 RULE-SET,ChinaMax —— 模拟订阅覆盖后的场景
cat >"$SOURCE_CONFIG" <<'EOF'
mixed-port: 7890
proxy-groups:
  - name: Auto
    type: select
    proxies:
      - DIRECT
  - name: 🇯🇵 Japan
    type: select
    proxies:
      - JP-1
  - name: 🇺🇸 United States
    type: select
    proxies:
      - US-1
  - name: 🇸🇬 Singapore
    type: select
    proxies:
      - SG-1
rules:
  - DOMAIN-SUFFIX,example.com,SSRDOG
  - MATCH,SSRDOG
EOF

CONFIG_DIR="$TMP_DIR" \
SOURCE_CONFIG_FILE="$SOURCE_CONFIG" \
RUNTIME_CONFIG_FILE="$RUNTIME_CONFIG" \
PROG_PATH="$MIHOMO_BIN" \
"$SCRIPT" render >/dev/null

line_number() {
    local needle="$1"
    awk -v needle="$needle" 'index($0, needle) { print NR; exit }' "$RUNTIME_CONFIG"
}

# rule-providers 必须被注入
if ! grep -q 'ChinaMax:' "$RUNTIME_CONFIG"; then
    echo "ASSERTION FAILED: rule-providers 缺少 ChinaMax" >&2
    exit 1
fi

if ! grep -q 'path: ./ruleset/ChinaMax.yml' "$RUNTIME_CONFIG"; then
    echo "ASSERTION FAILED: ChinaMax provider 缺少本地 path" >&2
    exit 1
fi

# RULE-SET,ChinaMax,DIRECT 必须被注入到 rules 中
line_chinamax="$(line_number 'RULE-SET,ChinaMax,DIRECT')"
line_geoip_cn="$(line_number 'GEOIP,CN,DIRECT,no-resolve')"
line_match="$(line_number 'MATCH,SSRDOG')"
line_openai="$(line_number 'DOMAIN-SUFFIX,openai.com,AI-MANUAL')"

if [ -z "$line_chinamax" ]; then
    echo "ASSERTION FAILED: rules 缺少 RULE-SET,ChinaMax,DIRECT" >&2
    exit 1
fi

if [ -z "$line_openai" ] || [ "$line_openai" -ge "$line_chinamax" ]; then
    echo "ASSERTION FAILED: AI 规则必须位于 ChinaMax 前" >&2
    exit 1
fi

if [ "$line_chinamax" -ge "$line_geoip_cn" ]; then
    echo "ASSERTION FAILED: ChinaMax 应位于 GEOIP,CN 前" >&2
    exit 1
fi

if [ "$line_geoip_cn" -ge "$line_match" ]; then
    echo "ASSERTION FAILED: GEOIP,CN 必须位于 MATCH 前" >&2
    exit 1
fi

echo "render_chinamax_inject_test: PASS"
