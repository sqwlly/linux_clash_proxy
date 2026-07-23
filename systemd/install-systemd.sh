#!/bin/bash

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SYSTEMD_DIR="/etc/systemd/system"
DEFAULT_ENV_DIR="/etc/default"

install -m 644 "${PROJECT_DIR}/systemd/clash-proxy.service" "${SYSTEMD_DIR}/clash-proxy.service"
install -m 644 "${PROJECT_DIR}/systemd/clash-proxy-refresh.service" "${SYSTEMD_DIR}/clash-proxy-refresh.service"
install -m 644 "${PROJECT_DIR}/systemd/clash-proxy-refresh.timer" "${SYSTEMD_DIR}/clash-proxy-refresh.timer"
install -m 644 "${PROJECT_DIR}/systemd/clash-proxy-refresh.path" "${SYSTEMD_DIR}/clash-proxy-refresh.path"
install -m 644 "${PROJECT_DIR}/systemd/clash-proxy-subscription.service" "${SYSTEMD_DIR}/clash-proxy-subscription.service"
install -m 644 "${PROJECT_DIR}/systemd/clash-proxy-subscription.timer" "${SYSTEMD_DIR}/clash-proxy-subscription.timer"
install -m 755 "${PROJECT_DIR}/systemd/clash-proxy-subscription.sh" "${PROJECT_DIR}/systemd/clash-proxy-subscription.sh"
install -m 644 "${PROJECT_DIR}/systemd/clash-proxy-command.env.example" "${DEFAULT_ENV_DIR}/clash-proxy-command.example"

if [ ! -f "${DEFAULT_ENV_DIR}/clash-proxy-command" ]; then
    install -m 644 "${DEFAULT_ENV_DIR}/clash-proxy-command.example" "${DEFAULT_ENV_DIR}/clash-proxy-command"
fi

systemctl daemon-reload
systemctl enable --now clash-proxy.service
systemctl enable --now clash-proxy-refresh.timer
systemctl enable --now clash-proxy-refresh.path
systemctl enable --now clash-proxy-subscription.timer

cat <<'EOF'
systemd 安装完成:
- /etc/systemd/system/clash-proxy.service
- /etc/systemd/system/clash-proxy-refresh.service
- /etc/systemd/system/clash-proxy-refresh.timer
- /etc/systemd/system/clash-proxy-refresh.path
- /etc/systemd/system/clash-proxy-subscription.service
- /etc/systemd/system/clash-proxy-subscription.timer
- /etc/default/clash-proxy-command.example
- /etc/default/clash-proxy-command

如需让其它服务走代理，可继续运行:
  /root/clash_proxy/systemd/generate-proxied-service.sh <service-name>
EOF
