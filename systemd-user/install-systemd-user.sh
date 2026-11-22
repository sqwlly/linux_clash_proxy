#!/bin/bash

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
USER_SYSTEMD_DIR="${XDG_CONFIG_HOME:-${HOME}/.config}/systemd/user"
USER_CONFIG_DIR="${XDG_CONFIG_HOME:-${HOME}/.config}/cproxy"

mkdir -p "${USER_SYSTEMD_DIR}" "${USER_CONFIG_DIR}"

CPROXY_BIN="/usr/local/bin/cproxy"
if [ "$(id -u)" != "0" ]; then
    detected_bin="$(command -v cproxy 2>/dev/null || true)"
    if [ -z "${detected_bin}" ] && [ -x "${HOME}/.local/bin/cproxy" ]; then
        detected_bin="${HOME}/.local/bin/cproxy"
    fi
    if [ -n "${detected_bin}" ]; then
        CPROXY_BIN="${detected_bin}"
    fi
fi

install_unit() {
    local src="$1"
    local dst="$2"
    if [ "${CPROXY_BIN}" != "/usr/local/bin/cproxy" ] && [[ "${src}" == *.service ]]; then
        CPROXY_BIN_REPL="${CPROXY_BIN}" awk '{
            repl = ENVIRON["CPROXY_BIN_REPL"]
            target = "/usr/local/bin/cproxy"
            while ((idx = index($0, target)) > 0) {
                $0 = substr($0, 1, idx - 1) repl substr($0, idx + length(target))
            }
            print
        }' "${src}" > "${dst}"
        chmod 644 "${dst}"
    else
        install -m 644 "${src}" "${dst}"
    fi
}

install_unit "${PROJECT_DIR}/systemd-user/cproxy.service" "${USER_SYSTEMD_DIR}/cproxy.service"
install_unit "${PROJECT_DIR}/systemd-user/cproxy-h2-keepalive.service" "${USER_SYSTEMD_DIR}/cproxy-h2-keepalive.service"
install_unit "${PROJECT_DIR}/systemd-user/cproxy-refresh.service" "${USER_SYSTEMD_DIR}/cproxy-refresh.service"
install_unit "${PROJECT_DIR}/systemd-user/cproxy-refresh.timer" "${USER_SYSTEMD_DIR}/cproxy-refresh.timer"
install_unit "${PROJECT_DIR}/systemd-user/cproxy-subscription.service" "${USER_SYSTEMD_DIR}/cproxy-subscription.service"
install_unit "${PROJECT_DIR}/systemd-user/cproxy-subscription.timer" "${USER_SYSTEMD_DIR}/cproxy-subscription.timer"
install_unit "${PROJECT_DIR}/systemd-user/cproxy-ai-rebalance.service" "${USER_SYSTEMD_DIR}/cproxy-ai-rebalance.service"
install_unit "${PROJECT_DIR}/systemd-user/cproxy-ai-rebalance.timer" "${USER_SYSTEMD_DIR}/cproxy-ai-rebalance.timer"
install_unit "${PROJECT_DIR}/systemd-user/cproxy-ai-watch.service" "${USER_SYSTEMD_DIR}/cproxy-ai-watch.service"
install_unit "${PROJECT_DIR}/systemd-user/cproxy-ai-watch.timer" "${USER_SYSTEMD_DIR}/cproxy-ai-watch.timer"
install -m 644 "${PROJECT_DIR}/systemd-user/cproxy-command.env.example" "${USER_CONFIG_DIR}/cproxy-command.env.example"

if [ ! -f "${USER_CONFIG_DIR}/cproxy-command.env" ]; then
    install -m 644 "${USER_CONFIG_DIR}/cproxy-command.env.example" "${USER_CONFIG_DIR}/cproxy-command.env"
fi

systemctl --user daemon-reload
systemctl --user enable --now cproxy.service cproxy-h2-keepalive.service
systemctl --user enable --now cproxy-refresh.timer cproxy-subscription.timer cproxy-ai-rebalance.timer cproxy-ai-watch.timer

cat <<EOF
用户级 systemd 安装完成（cproxy 入口: ${CPROXY_BIN}）:
- ${USER_SYSTEMD_DIR}/cproxy.service
- ${USER_SYSTEMD_DIR}/cproxy-h2-keepalive.service
- ${USER_SYSTEMD_DIR}/cproxy-refresh.service
- ${USER_SYSTEMD_DIR}/cproxy-refresh.timer
- ${USER_SYSTEMD_DIR}/cproxy-subscription.service
- ${USER_SYSTEMD_DIR}/cproxy-subscription.timer
- ${USER_SYSTEMD_DIR}/cproxy-ai-rebalance.service
- ${USER_SYSTEMD_DIR}/cproxy-ai-rebalance.timer
- ${USER_SYSTEMD_DIR}/cproxy-ai-watch.service
- ${USER_SYSTEMD_DIR}/cproxy-ai-watch.timer
- ${USER_CONFIG_DIR}/cproxy-command.env.example
- ${USER_CONFIG_DIR}/cproxy-command.env

如需让其它用户级服务走代理，可继续运行:
  ${PROJECT_DIR}/systemd-user/generate-proxied-service.sh <service-name>
EOF
