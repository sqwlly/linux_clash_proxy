"""订阅用量信息：解析并持久化订阅响应的 ``subscription-userinfo`` 头。

机场在订阅响应头里返回账户用量（``upload=1; download=2; total=3; expire=4``，
字节数与 Unix 秒时间戳，字段任意子集），比面板信息节点（节点名携带动态数值）
更可靠且支持度更广。refresh 时逐订阅落盘到 state 目录，status 读取展示；
``--raw`` 与人读输出共用同一数据源。
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from ..config import AppPaths, read_config, subscription_info_file

# 主订阅在状态文件中的键；附加机场以其配置名（subscriptions[].name）为键
MAIN_SUBSCRIPTION_KEY = "main"
MAIN_SUBSCRIPTION_LABEL = "主订阅"

_PANEL_REMAINING_RE = re.compile(
    r"剩余(?:流量)?\s*[：:]?\s*(\d+(?:\.\d+)?)\s*(TiB|GiB|MiB|KiB|TB|GB|MB|KB)",
    re.IGNORECASE,
)
_PANEL_EXPIRE_DATE_RE = re.compile(
    r"(?:套餐到期|到期时间|过期时间|有效期|到期)\s*[：:]?\s*(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})"
)
_UNIT_MULTIPLIERS = {
    "kb": 1024,
    "kib": 1024,
    "mb": 1024**2,
    "mib": 1024**2,
    "gb": 1024**3,
    "gib": 1024**3,
    "tb": 1024**4,
    "tib": 1024**4,
}


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


def parse_panel_info_nodes(proxies: list | None) -> SubscriptionUsage | None:
    """从订阅的面板信息节点（如『剩余流量：577.12 GB』『套餐到期：2027-05-01』）提取余量与到期时间。

    当机场不返回 ``subscription-userinfo`` 响应头或头字段缺失时，变废为宝提取结构化用量。
    """
    if not isinstance(proxies, list):
        return None
    remaining_bytes: int | None = None
    expire_ts: int | None = None
    for item in proxies:
        name = str(item.get("name") if isinstance(item, dict) else item or "").strip()
        if not name:
            continue
        if remaining_bytes is None:
            match_rem = _PANEL_REMAINING_RE.search(name)
            if match_rem:
                amount = float(match_rem.group(1))
                unit = match_rem.group(2).lower()
                remaining_bytes = int(amount * _UNIT_MULTIPLIERS.get(unit, 1024**3))
        if expire_ts is None:
            match_exp = _PANEL_EXPIRE_DATE_RE.search(name)
            if match_exp:
                try:
                    dt = datetime(
                        int(match_exp.group(1)),
                        int(match_exp.group(2)),
                        int(match_exp.group(3)),
                        23,
                        59,
                        59,
                    )
                    expire_ts = int(dt.timestamp())
                except ValueError:
                    pass
    if remaining_bytes is None and expire_ts is None:
        return None
    return SubscriptionUsage(
        upload=0 if remaining_bytes is not None else None,
        download=0 if remaining_bytes is not None else None,
        total=remaining_bytes,
        expire=expire_ts,
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


def record_subscription_info(
    paths: AppPaths,
    key: str,
    header_value: str | None,
    proxies: list | None = None,
) -> bool:
    """记录一份订阅的用量；优先用响应头，缺失字段由面板信息节点补齐。"""
    usage = parse_userinfo_header(header_value)
    panel_usage = parse_panel_info_nodes(proxies) if proxies else None
    if usage is None:
        usage = panel_usage
    elif panel_usage is not None:
        if usage.total is None and panel_usage.total is not None:
            usage.total = usage.used + panel_usage.total
        if usage.expire is None and panel_usage.expire is not None:
            usage.expire = panel_usage.expire
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
    if isinstance(value, (int, float, str, bytes, bytearray)):
        return int(value)
    return int(str(value))


def _write_state(path: Path, data: dict) -> None:
    """原子落盘（临时文件 + rename），避免半截 JSON 留在 state 目录。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)
