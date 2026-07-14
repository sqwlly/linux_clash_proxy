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

# 模拟只有中文节点名、没有标准区域组、且 DNS fallback-filter 含 geosite 的其他订阅
cat >"$SOURCE_CONFIG" <<'EOF'
mixed-port: 7890
dns:
  enable: true
  enhanced-mode: fake-ip
  fake-ip-range: 198.18.0.1/16
  nameserver:
    - 223.5.5.5
  fallback:
    - 1.1.1.1
  fallback-filter:
    geoip: true
    geoip-code: CN
    geosite:
      - gfw
    ipcidr:
      - 240.0.0.0/4
proxies:
  - name: '🇭🇰香港 01'
    type: ss
    server: hk.example.com
    port: 443
    cipher: aes-256-gcm
    password: secret
  - name: '🇺🇸美国 01'
    type: ss
    server: us.example.com
    port: 443
    cipher: aes-256-gcm
    password: secret
  - name: '🇸🇬新加坡 01'
    type: ss
    server: sg.example.com
    port: 443
    cipher: aes-256-gcm
    password: secret
  - name: '🇯🇵日本 01'
    type: ss
    server: jp.example.com
    port: 443
    cipher: aes-256-gcm
    password: secret
proxy-groups:
  - name: CyberGuard
    type: select
    proxies:
      - 自动选择
      - DIRECT
  - name: 自动选择
    type: url-test
    proxies:
      - '🇭🇰香港 01'
      - '🇺🇸美国 01'
      - '🇸🇬新加坡 01'
      - '🇯🇵日本 01'
    url: http://www.gstatic.com/generate_204
    interval: 86400
rules:
  - MATCH,CyberGuard
EOF

CONFIG_DIR="$TMP_DIR" \
SOURCE_CONFIG_FILE="$SOURCE_CONFIG" \
RUNTIME_CONFIG_FILE="$RUNTIME_CONFIG" \
PROG_PATH="$MIHOMO_BIN" \
"$SCRIPT" render >/dev/null

for group in '🇯🇵 Japan' '🇺🇸 United States' '🇸🇬 Singapore' 'AI-MANUAL' 'AI-AUTO' 'AI-US' 'AI-SG'; do
    if ! grep -q "name: ${group}" "$RUNTIME_CONFIG"; then
        echo "ASSERTION FAILED: 缺少组 ${group}" >&2
        exit 1
    fi
done

# 确认自动生成的区域组包含正确节点
python3 - "$RUNTIME_CONFIG" <<'PY'
import sys
import yaml

with open(sys.argv[1], "r", encoding="utf-8") as fh:
    data = yaml.safe_load(fh) or {}

group_map = {g["name"]: g for g in data.get("proxy-groups", []) if isinstance(g, dict)}
required = {
    "🇯🇵 Japan": "🇯🇵日本 01",
    "🇺🇸 United States": "🇺🇸美国 01",
    "🇸🇬 Singapore": "🇸🇬新加坡 01",
}
for group_name, expected_node in required.items():
    proxies = group_map.get(group_name, {}).get("proxies") or []
    if expected_node not in proxies:
        print(f"ASSERTION FAILED: {group_name} 缺少预期节点 {expected_node}", file=sys.stderr)
        sys.exit(1)

fallback_filter = data.get("dns", {}).get("fallback-filter") or {}
if "geosite" in fallback_filter:
    print("ASSERTION FAILED: dns.fallback-filter.geosite 应被清理", file=sys.stderr)
    sys.exit(1)

print("render_auto_region_test: PASS")
PY
