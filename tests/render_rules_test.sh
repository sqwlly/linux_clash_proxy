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
  - DOMAIN-SUFFIX,openai.com,SSRDOG
  - RULE-SET,ChinaMax,DIRECT
  - MATCH,SSRDOG
EOF

CONFIG_DIR="$TMP_DIR" \
SOURCE_CONFIG_FILE="$SOURCE_CONFIG" \
RUNTIME_CONFIG_FILE="$RUNTIME_CONFIG" \
PROG_PATH="$MIHOMO_BIN" \
"$SCRIPT" render >/dev/null

line_openai="$(rg -n 'DOMAIN-SUFFIX,openai.com,AI-MANUAL' "$RUNTIME_CONFIG" | cut -d: -f1)"
line_chinamax="$(rg -n 'RULE-SET,ChinaMax,DIRECT' "$RUNTIME_CONFIG" | cut -d: -f1)"
line_geoip_cn="$(rg -n 'GEOIP,CN,DIRECT,no-resolve' "$RUNTIME_CONFIG" | cut -d: -f1)"
line_match="$(rg -n 'MATCH,SSRDOG' "$RUNTIME_CONFIG" | cut -d: -f1)"
https_checks="$(rg -c 'url: https://cp.cloudflare.com/generate_204' "$RUNTIME_CONFIG")"

if [ -z "$line_openai" ] || [ -z "$line_chinamax" ] || [ -z "$line_geoip_cn" ] || [ -z "$line_match" ]; then
    echo "ASSERTION FAILED: 缺少关键规则" >&2
    exit 1
fi

if [ "$line_openai" -ge "$line_chinamax" ]; then
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

if [ "$https_checks" -lt 3 ]; then
    echo "ASSERTION FAILED: AI fallback/url 默认健康检查地址应使用 HTTPS" >&2
    exit 1
fi

echo "render_rules_test: PASS"
