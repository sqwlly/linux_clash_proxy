"""订阅用量信息：解析并持久化订阅响应的 ``subscription-userinfo`` 头。

机场在订阅响应头里返回账户用量（``upload=1; download=2; total=3; expire=4``，
字节数与 Unix 秒时间戳，字段任意子集），比面板信息节点（节点名携带动态数值）
更可靠且支持度更广。refresh 时逐订阅落盘到 state 目录，status 读取展示；
``--raw`` 与人读输出共用同一数据源。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from ..config import AppPaths, read_config, subscription_info_file

# 主订阅在状态文件中的键；附加机场以其配置名（subscriptions[].name）为键
MAIN_SUBSCRIPTION_KEY = "main"
MAIN_SUBSCRIPTION_LABEL = "主订阅"


@dataclass
class SubscriptionUsage:
    upload: int | None = None
    download: int | None = None
    total: int | None = None
    expire: int | None = None
    updated_at: str = ""

    @property
    def used(self) -> int:
        return (self.upload or 0) + (self.download or 0)

    @property
    def remaining(self) -> int | None:
        """剩余字节；total 缺失或已超用时返回 None / 0 以下按 0 截断。"""
        if self.total is None:
            return None
        return max(0, self.total - self.used)

    def to_dict(self) -> dict:
        return {
            "upload": self.upload,
            "download": self.download,
            "total": self.total,
            "expire": self.expire,
            "updated_at": self.updated_at,
        }


def parse_userinfo_header(value: str | None) -> SubscriptionUsage | None:
    """解析 ``subscription-userinfo`` 头；无有效字段时返回 None。

    容忍字段乱序、子集、多余空白与未知键；非数字值直接跳过。
    """
    if not value or not value.strip():
        return None
    fields: dict[str, int] = {}
    for part in value.split(";"):
        key, _, raw = part.strip().partition("=")
        key = key.strip().lower()
        raw = raw.strip()
        if key and raw.isdigit():
            fields[key] = int(raw)
    if not any(key in fields for key in ("upload", "download", "total", "expire")):
        return None
    return SubscriptionUsage(
        upload=fields.get("upload"),
        download=fields.get("download"),
        total=fields.get("total"),
        # expire=0 是「未设置」的常见写法，与缺失同等对待
        expire=fields.get("expire") or None,
        updated_at=datetime.now().isoformat(timespec="seconds"),
    )


def load_subscription_info(paths: AppPaths) -> dict[str, SubscriptionUsage]:
    """读取全部已记录的订阅用量；文件缺失/损坏时返回空 dict，绝不抛错。"""
    path = subscription_info_file(paths)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    entries: dict[str, SubscriptionUsage] = {}
    for key, item in data.items():
        if not isinstance(item, dict):
            continue
        try:
            entries[str(key)] = SubscriptionUsage(
                upload=_opt_int(item.get("upload")),
                download=_opt_int(item.get("download")),
                total=_opt_int(item.get("total")),
                expire=_opt_int(item.get("expire")),
                updated_at=str(item.get("updated_at") or ""),
            )
        except (TypeError, ValueError):
            continue
    return entries


def record_subscription_info(paths: AppPaths, key: str, header_value: str | None) -> bool:
    """记录一份订阅的用量；头缺失/不可解析时保留原有记录并返回 False。

    读-改-写非事务：理论上 timer 与手动 refresh 并发时可能丢失一次写入
    （文件不致损坏，原子 rename 保证），refresh 由 systemd 串行触发，可接受。
    """
    usage = parse_userinfo_header(header_value)
    if usage is None:
        return False
    entries = load_subscription_info(paths)
    entries[key] = usage
    _write_state(subscription_info_file(paths), {name: item.to_dict() for name, item in entries.items()})
    return True


def display_entries(paths: AppPaths) -> list[tuple[str, SubscriptionUsage]]:
    """按展示顺序取出订阅用量：主订阅在前，附加机场按配置顺序。

    只返回当前配置仍引用的订阅（主订阅需配置 subscription-url），
    已删除机场的陈旧记录不展示。
    """
    entries = load_subscription_info(paths)
    if not entries:
        return []
    ordered: list[tuple[str, SubscriptionUsage]] = []
    try:
        config = read_config(paths)
        has_primary = bool(str(config.get("subscription-url") or "").strip())
        raw_names = [
            str(item.get("name") or "").strip()
            for item in config.get("subscriptions") or []
            if isinstance(item, dict)
        ]
        extra_names = list(dict.fromkeys(name for name in raw_names if name))
    except Exception:
        # 配置读取失败时退化为仅展示主订阅记录，不让状态面板崩掉
        has_primary, extra_names = True, []
    if has_primary and MAIN_SUBSCRIPTION_KEY in entries:
        ordered.append((MAIN_SUBSCRIPTION_LABEL, entries[MAIN_SUBSCRIPTION_KEY]))
    for name in extra_names:
        if name in entries:
            ordered.append((name, entries[name]))
    return ordered


def _opt_int(value: object) -> int | None:
    if value is None:
        return None
    return int(value)


def _write_state(path: Path, data: dict) -> None:
    """原子落盘（临时文件 + rename），避免半截 JSON 留在 state 目录。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)
