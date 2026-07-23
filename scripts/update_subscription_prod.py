#!/usr/bin/env python
"""更新生产入口（/root/clash_proxy）的订阅。

复用 cproxy.services.refresh.update_source_from_subscription 的安全合并逻辑：
剔除安全相关键、保留本地优先键、写入前自动留快照。仅负责"拉订阅 → 合并 →
写回 config.yaml"，render / 重启 / 回滚由 systemd/clash-proxy-subscription.sh
按生产入口（proxy.sh + clash-proxy.service）的方式处理。

退出码：
  0  订阅已更新（config.yaml 已写入新内容）
  3  未配置 subscription-url，跳过
  1  其它错误（下载失败、内容非法等）
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent

from cproxy.config import AppPaths, config_file, read_config  # noqa: E402
from cproxy.redaction import redact_text  # noqa: E402
from cproxy.services.refresh import update_source_from_subscription  # noqa: E402


def prod_paths(project_dir: Path) -> AppPaths:
    # state_dir 借用 .data/cproxy 仅用于落地订阅前的配置快照（与 git 隔离）
    return AppPaths(
        config_dir=project_dir,
        data_dir=project_dir,
        state_dir=project_dir / ".data" / "cproxy",
    )


def main() -> int:
    paths = prod_paths(PROJECT_DIR)
    config = read_config(paths)
    url = str(config.get("subscription-url") or "").strip()
    if not url:
        print("跳过: 生产配置未设置 subscription-url")
        return 3

    try:
        update_source_from_subscription(paths, url)
    except Exception as exc:
        print(f"错误: 订阅更新失败: {redact_text(str(exc))}", file=sys.stderr)
        return 1

    print(f"订阅已更新: {redact_text(url)} -> {config_file(paths)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
