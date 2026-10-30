#!/bin/bash

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
USER_SYSTEMD_DIR="${HOME}/.config/systemd/user"
USER_CONFIG_DIR="${HOME}/.config/cproxy"

mkdir -p "${USER_SYSTEMD_DIR}" "${USER_CONFIG_DIR}"

install -m 644 "${PROJECT_DIR}/systemd-user/cproxy.service" "${HOME}/.config/systemd/user/cproxy.service"
install -m 644 "${PROJECT_DIR}/systemd-user/cproxy-h2-keepalive.service" "${HOME}/.config/systemd/user/cproxy-h2-keepalive.service"
install -m 644 "${PROJECT_DIR}/systemd-user/cproxy-refresh.service" "${HOME}/.config/systemd/user/cproxy-refresh.service"
install -m 644 "${PROJECT_DIR}/systemd-user/cproxy-refresh.timer" "${HOME}/.config/systemd/user/cproxy-refresh.timer"
install -m 644 "${PROJECT_DIR}/systemd-user/cproxy-subscription.service" "${HOME}/.config/systemd/user/cproxy-subscription.service"
install -m 644 "${PROJECT_DIR}/systemd-user/cproxy-subscription.timer" "${HOME}/.config/systemd/user/cproxy-subscription.timer"
install -m 644 "${PROJECT_DIR}/systemd-user/cproxy-ai-rebalance.service" "${HOME}/.config/systemd/user/cproxy-ai-rebalance.service"
install -m 644 "${PROJECT_DIR}/systemd-user/cproxy-ai-rebalance.timer" "${HOME}/.config/systemd/user/cproxy-ai-rebalance.timer"
install -m 644 "${PROJECT_DIR}/systemd-user/cproxy-ai-watch.service" "${HOME}/.config/systemd/user/cproxy-ai-watch.service"
install -m 644 "${PROJECT_DIR}/systemd-user/cproxy-ai-watch.timer" "${HOME}/.config/systemd/user/cproxy-ai-watch.timer"
install -m 644 "${PROJECT_DIR}/systemd-user/cproxy-command.env.example" "${HOME}/.config/cproxy/cproxy-command.env.example"

if [ ! -f "${HOME}/.config/cproxy/cproxy-command.env" ]; then
    install -m 644 "${HOME}/.config/cproxy/cproxy-command.env.example" "${HOME}/.config/cproxy/cproxy-command.env"
fi

systemctl --user daemon-reload
systemctl --user enable --now cproxy.service cproxy-h2-keepalive.service
systemctl --user enable --now cproxy-refresh.timer cproxy-subscription.timer cproxy-ai-rebalance.timer cproxy-ai-watch.timer

cat <<EOF
用户级 systemd 安装完成:
- ${HOME}/.config/systemd/user/cproxy.service
- ${HOME}/.config/systemd/user/cproxy-refresh.service
- ${HOME}/.config/systemd/user/cproxy-refresh.timer
- ${HOME}/.config/systemd/user/cproxy-subscription.service
- ${HOME}/.config/systemd/user/cproxy-subscription.timer
- ${HOME}/.config/systemd/user/cproxy-ai-rebalance.service
- ${HOME}/.config/systemd/user/cproxy-ai-rebalance.timer
- ${HOME}/.config/systemd/user/cproxy-ai-watch.service
- ${HOME}/.config/systemd/user/cproxy-ai-watch.timer
- ${HOME}/.config/cproxy/cproxy-command.env.example
- ${HOME}/.config/cproxy/cproxy-command.env

如需让其它用户级服务走代理，可继续运行:
  ${PROJECT_DIR}/systemd-user/generate-proxied-service.sh <service-name>
EOF
