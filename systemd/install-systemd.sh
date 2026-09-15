#!/bin/bash
#
# 安装系统级 systemd 单元。
#
# 默认只装仍在活跃使用的那个：`clash-proxy-traffic-collector`——它名字带 legacy 前缀，
# 但 ExecStart 实际调用 `/usr/local/bin/cproxy traffic collect --raw`，属于 cproxy 链路。
#
# legacy 单元（`clash-proxy.service` / `clash-proxy-refresh.*` / `clash-proxy-subscription.*`）
# 已于 2026-09-11 停用待退役，仅在显式 `--with-legacy` 时安装。默认不装是关键：
# 否则将来任何一次重跑都会把这些已停用的 unit 重新 enable 起来。
#
# 注意不要按名字前缀判断归属——`clash-proxy-` 横跨新旧两代。

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SYSTEMD_DIR="/etc/systemd/system"
DEFAULT_ENV_DIR="/etc/default"
WITH_LEGACY=0
DRY_RUN=0

while [ "$#" -gt 0 ]; do
    case "$1" in
        --with-legacy)
            WITH_LEGACY=1
            shift
            ;;
        --dry-run)
            DRY_RUN=1
            shift
            ;;
        -h|--help)
            cat <<EOF
Usage: $0 [--with-legacy] [--dry-run]

默认安装:
  clash-proxy-traffic-collector.{service,timer}   活跃组件，调用 cproxy traffic collect

Options:
  --with-legacy   额外安装已停用的 legacy 单元（clash-proxy.service / refresh / subscription）
  --dry-run       只打印将执行的动作，不写文件、不改 systemd 状态
  -h, --help      显示本帮助
EOF
            exit 0
            ;;
        *)
            echo "错误: 未知参数: $1" >&2
            exit 2
            ;;
    esac
done

run() {
    if [ "$DRY_RUN" -eq 1 ]; then
        echo "DRY-RUN $*"
    else
        "$@"
    fi
}

run install -m 644 "${PROJECT_DIR}/systemd/clash-proxy-traffic-collector.service" "${SYSTEMD_DIR}/clash-proxy-traffic-collector.service"
run install -m 644 "${PROJECT_DIR}/systemd/clash-proxy-traffic-collector.timer" "${SYSTEMD_DIR}/clash-proxy-traffic-collector.timer"

if [ "$WITH_LEGACY" -eq 1 ]; then
    run install -m 644 "${PROJECT_DIR}/systemd/clash-proxy.service" "${SYSTEMD_DIR}/clash-proxy.service"
    run install -m 644 "${PROJECT_DIR}/systemd/clash-proxy-refresh.service" "${SYSTEMD_DIR}/clash-proxy-refresh.service"
    run install -m 644 "${PROJECT_DIR}/systemd/clash-proxy-refresh.timer" "${SYSTEMD_DIR}/clash-proxy-refresh.timer"
    run install -m 644 "${PROJECT_DIR}/systemd/clash-proxy-refresh.path" "${SYSTEMD_DIR}/clash-proxy-refresh.path"
    run install -m 644 "${PROJECT_DIR}/systemd/clash-proxy-subscription.service" "${SYSTEMD_DIR}/clash-proxy-subscription.service"
    run install -m 644 "${PROJECT_DIR}/systemd/clash-proxy-subscription.timer" "${SYSTEMD_DIR}/clash-proxy-subscription.timer"
    run install -m 755 "${PROJECT_DIR}/systemd/clash-proxy-subscription.sh" "${PROJECT_DIR}/systemd/clash-proxy-subscription.sh"
    run install -m 644 "${PROJECT_DIR}/systemd/clash-proxy-command.env.example" "${DEFAULT_ENV_DIR}/clash-proxy-command.example"

    if [ ! -f "${DEFAULT_ENV_DIR}/clash-proxy-command" ]; then
        run install -m 644 "${DEFAULT_ENV_DIR}/clash-proxy-command.example" "${DEFAULT_ENV_DIR}/clash-proxy-command"
    fi
fi

run systemctl daemon-reload
run systemctl enable --now clash-proxy-traffic-collector.timer

if [ "$WITH_LEGACY" -eq 1 ]; then
    run systemctl enable --now clash-proxy.service
    run systemctl enable --now clash-proxy-refresh.timer
    run systemctl enable --now clash-proxy-refresh.path
    run systemctl enable --now clash-proxy-subscription.timer
fi

cat <<'EOF'
systemd 安装完成:
- clash-proxy-traffic-collector.service   （活跃：调用 cproxy traffic collect）
- clash-proxy-traffic-collector.timer
EOF

if [ "$WITH_LEGACY" -eq 1 ]; then
    cat <<'EOF'
- clash-proxy.service                     （legacy，已停用待退役）
- clash-proxy-refresh.{service,timer,path}
- clash-proxy-subscription.{service,timer}
- /etc/default/clash-proxy-command.example
- /etc/default/clash-proxy-command
EOF
else
    cat <<'EOF'

legacy 单元未安装（已停用待退役）。确需安装时加 --with-legacy。
EOF
fi

cat <<'EOF'

如需让其它服务走代理，可继续运行:
  /root/clash_proxy/systemd/generate-proxied-service.sh <service-name>
EOF
