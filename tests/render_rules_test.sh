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
  - DOMAIN-SUFFIX,anthropic.com,PROXY
  - DOMAIN-KEYWORD,openai,PROXY
  - DOMAIN-SUFFIX,example.com,SSRDOG
  - DOMAIN-SUFFIX,cursor.sh,DIRECT
  - DOMAIN,safebrowsing.googleapis.com,DIRECT
  - GEOIP,CN,DIRECT
  - RULE-SET,ChinaMax,DIRECT
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

line_openai="$(line_number 'DOMAIN-SUFFIX,openai.com,AI-MANUAL')"
line_chinamax="$(line_number 'RULE-SET,ChinaMax,DIRECT')"
line_geoip_cn="$(line_number 'GEOIP,CN,DIRECT,no-resolve')"
line_match="$(line_number 'MATCH,SSRDOG')"
line_subscription="$(line_number 'DOMAIN-SUFFIX,example.com,SSRDOG')"
https_checks="$(awk '/url: https:\/\/cp\.cloudflare\.com\/generate_204/ { count++ } END { print count + 0 }' "$RUNTIME_CONFIG")"

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

# 注入规则必须前移到订阅规则之前，防止被订阅通用规则遮蔽
if [ "$line_openai" -ge "$line_subscription" ]; then
    echo "ASSERTION FAILED: AI 规则必须位于订阅规则之前" >&2
    exit 1
fi

# 任意订阅商的 AI 冲突规则（不限 SSRDOG 组名）都应被清理
for conflict_rule in \
    "DOMAIN-SUFFIX,openai.com,SSRDOG" \
    "DOMAIN-SUFFIX,anthropic.com,PROXY" \
    "DOMAIN-KEYWORD,openai,PROXY"; do
    if grep -qF -- "- $conflict_rule" "$RUNTIME_CONFIG"; then
        echo "ASSERTION FAILED: AI 冲突规则未被清理: $conflict_rule" >&2
        exit 1
    fi
done

# 裸 GEOIP,CN,DIRECT（无 no-resolve）应被清理，只保留 no-resolve 版本
bare_geoip_count="$(grep -cE '^  - GEOIP,CN,DIRECT$' "$RUNTIME_CONFIG" || true)"
if [ "$bare_geoip_count" -ne 0 ]; then
    echo "ASSERTION FAILED: 裸 GEOIP,CN,DIRECT 应被移除" >&2
    exit 1
fi

# 失效/有害直连规则应被清理
for stale_rule in "DOMAIN-SUFFIX,cursor.sh,DIRECT" "DOMAIN,safebrowsing.googleapis.com,DIRECT"; do
    if grep -qF -- "- $stale_rule" "$RUNTIME_CONFIG"; then
        echo "ASSERTION FAILED: 有害规则未被清理: $stale_rule" >&2
        exit 1
    fi
done

# AI 域名覆盖与下载直连规则应注入
for expected_rule in \
    "DOMAIN-SUFFIX,claudeusercontent.com,AI-MANUAL" \
    "DOMAIN-SUFFIX,sora.com,AI-MANUAL" \
    "DOMAIN-SUFFIX,grok.com,AI-MANUAL" \
    "DOMAIN-SUFFIX,openai.azure.com,AI-MANUAL" \
    "DOMAIN,cdn.auth0.com,AI-MANUAL" \
    "DOMAIN-SUFFIX,pytorch.org,DIRECT" \
    "DOMAIN-SUFFIX,npmjs.org,DIRECT"; do
    if ! grep -qF -- "- $expected_rule" "$RUNTIME_CONFIG"; then
        echo "ASSERTION FAILED: 缺少注入规则: $expected_rule" >&2
        exit 1
    fi
done

if [ "$https_checks" -lt 3 ]; then
    echo "ASSERTION FAILED: AI fallback/url 默认健康检查地址应使用 HTTPS" >&2
    exit 1
fi

echo "render_rules_test: PASS"
