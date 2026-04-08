#!/bin/bash
# 根据原始配置变更自动 render，并在需要时重启 systemd 服务

set -euo pipefail

PROJECT_DIR="/root/clash_proxy"
PROXY_SH="${PROJECT_DIR}/proxy.sh"
RUNTIME_CONFIG="${PROJECT_DIR}/runtime.yaml"
SERVICE_NAME="${SERVICE_NAME:-clash-proxy.service}"

before_hash=""
after_hash=""

if [ -f "$RUNTIME_CONFIG" ]; then
    before_hash="$(sha256sum "$RUNTIME_CONFIG" | awk '{print $1}')"
fi

"$PROXY_SH" render >/dev/null

if [ -f "$RUNTIME_CONFIG" ]; then
    after_hash="$(sha256sum "$RUNTIME_CONFIG" | awk '{print $1}')"
fi

if systemctl is-active --quiet "$SERVICE_NAME"; then
    if [ "$before_hash" != "$after_hash" ]; then
        systemctl restart "$SERVICE_NAME"
    fi
elif systemctl is-enabled --quiet "$SERVICE_NAME"; then
    systemctl start "$SERVICE_NAME"
fi
