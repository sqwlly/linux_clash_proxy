#!/bin/bash

set -euo pipefail

if [ "$#" -ne 1 ]; then
    echo "用法: $0 <service-name>" >&2
    exit 1
fi

service_name="$1"
service_unit="$service_name"
if [[ "$service_unit" != *.service ]]; then
    service_unit="${service_unit}.service"
fi

service_basename="${service_unit%.service}"
dropin_dir="/etc/systemd/system/${service_unit}.d"
dropin_file="${dropin_dir}/proxy.conf"

cat <<EOF
目标服务: ${service_basename}
建议创建目录:
  ${dropin_dir}

建议写入文件:
  ${dropin_file}

文件内容如下:
[Unit]
After=clash-proxy.service
Requires=clash-proxy.service

[Service]
EnvironmentFile=/etc/default/clash-proxy-command

应用命令:
  sudo mkdir -p "${dropin_dir}"
  sudo tee "${dropin_file}" >/dev/null <<'UNIT'
[Unit]
After=clash-proxy.service
Requires=clash-proxy.service

[Service]
EnvironmentFile=/etc/default/clash-proxy-command
UNIT
  sudo systemctl daemon-reload
  sudo systemctl restart "${service_unit}"
EOF
