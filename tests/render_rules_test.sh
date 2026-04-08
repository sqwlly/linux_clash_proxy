#!/bin/bash

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUNTIME_CONFIG="${PROJECT_DIR}/runtime.yaml"
SCRIPT="${PROJECT_DIR}/proxy.sh"

"$SCRIPT" render >/dev/null

line_openai="$(rg -n 'DOMAIN-SUFFIX,openai.com,AI-MANUAL' "$RUNTIME_CONFIG" | cut -d: -f1)"
line_chinamax="$(rg -n 'RULE-SET,ChinaMax,DIRECT' "$RUNTIME_CONFIG" | cut -d: -f1)"
line_geoip_cn="$(rg -n 'GEOIP,CN,DIRECT,no-resolve' "$RUNTIME_CONFIG" | cut -d: -f1)"
line_match="$(rg -n 'MATCH,SSRDOG' "$RUNTIME_CONFIG" | cut -d: -f1)"

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

echo "render_rules_test: PASS"
