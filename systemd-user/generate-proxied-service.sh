#!/bin/bash

set -euo pipefail

if [ "$#" -ne 1 ]; then
    echo "用法: $0 <service-name>" >&2
    exit 1
fi

SERVICE_NAME="$1"

cat <<EOF
为服务 ${SERVICE_NAME} 创建如下 drop-in:

目录:
  %h/.config/systemd/user/${SERVICE_NAME}.service.d/

文件:
  %h/.config/systemd/user/${SERVICE_NAME}.service.d/proxy.conf

内容:
[Unit]
After=cproxy.service
Requires=cproxy.service

[Service]
EnvironmentFile=%h/.config/cproxy/cproxy-command.env
EOF
