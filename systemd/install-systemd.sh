#!/bin/bash
#
# 安装系统级 systemd 单元。
#
# 只装 `clash-proxy-traffic-collector`——它名字带 legacy 前缀，但 ExecStart 实际调用
# `/usr/local/bin/cproxy traffic collect --raw`，属于 **cproxy 链路**。
#
# 注意不要按名字前缀判断归属——`clash-proxy-` 横跨新旧两代（本仓库出过的真实误判）。
# 其余 `clash-proxy.*` 单元曾属 legacy `proxy.sh` 工作流，已于 2026-10-17 随退役阶段 3 删除。

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SYSTEMD_DIR="/etc/systemd/system"
DRY_RUN=0

while [ "$#" -gt 0 ]; do
    case "$1" in
        --dry-run)
            DRY_RUN=1
            shift
            ;;
        -h|--help)
            cat <<EOF
Usage: $0 [--dry-run]

安装:
  clash-proxy-traffic-collector.{service,timer}   活跃组件，调用 cproxy traffic collect

Options:
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

run systemctl daemon-reload
run systemctl enable --now clash-proxy-traffic-collector.timer

cat <<'EOF'
systemd 安装完成:
- clash-proxy-traffic-collector.service   （活跃：调用 cproxy traffic collect）
- clash-proxy-traffic-collector.timer
EOF
