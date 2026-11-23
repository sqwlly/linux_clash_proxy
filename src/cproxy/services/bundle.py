"""`cproxy export` / `cproxy import` —— 多机配置与策略 Bundle 一键导出与克隆。

将一台机器上调优好的：
- 用户级偏好配置（`subscription-url`、`subscriptions`、`ai-manual-prefer`、`ai-gemini-prefer`、`max-ai-multiplier`、`tun` 等）
- 自定义分流规则（`custom-rules.json`）
- 节点屏蔽列表（`node-bans.json`）
- 告警通知配置（`notify.json`）
- 实测稳定度排序（`ai-manual-order.json` / `ai-gemini-order.json`）
打包为单一可移植文件（支持 `--redact` 脱敏分享），并在目标机器上一键还原。
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml

from .. import __version__
from ..config import (
    AppPaths,
    ai_gemini_order_file,
    ai_manual_order_file,
    config_file,
    read_config,
)
from ..redaction import redact_value
from ..snapshots import snapshot_file
from .ban import BanEntry, _save_bans, load_active_bans
from .notify import load_notify_config, save_notify_config
from .rules import RULE_TYPE_ALIASES, CustomRuleEntry, load_custom_rules, save_custom_rules

PORTABLE_CONFIG_KEYS = (
    "subscription-url",
    "subscriptions",
    "ai-manual-prefer",
    "ai-gemini-prefer",
    "ai-claude-prefer",
    "max-ai-multiplier",
    "refresh-groups",
    "tun",
    "mixed-port",
    "mode",
    "log-level",
    "output-color",
    "output-icons",
)

_VALID_RULE_TYPES = frozenset(RULE_TYPE_ALIASES.values())


def _atomic_write_private(path: Path, content: str, *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    tmp_path = Path(tmp_name)
    try:
        if os.name != "nt":
            os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fd = -1
            fh.write(content)
        if os.name != "nt":
            os.chmod(tmp_path, mode)
        os.replace(tmp_path, path)
    except BaseException:
        if fd >= 0:
            os.close(fd)
        tmp_path.unlink(missing_ok=True)
        raise


def _contains_redacted_sentinel(value: Any) -> bool:
    if isinstance(value, str):
        return "[REDACTED]" in value or "?..." in value
    if isinstance(value, dict):
        return any(_contains_redacted_sentinel(v) for v in value.values())
    if isinstance(value, list):
        return any(_contains_redacted_sentinel(item) for item in value)
    return False


def _is_redacted_webhook(url: str) -> bool:
    return "[REDACTED]" in url or "***" in url or "?..." in url


def _validate_http_url(url: str, field_name: str) -> str:
    cleaned = str(url or "").strip()
    parsed = urlparse(cleaned)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise ValueError(f"错误: {field_name} 必须使用 http/https 协议且包含合法主机名")
    return cleaned


def _read_json_list(path: Path) -> list[str]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(raw, list):
            return [str(x) for x in raw if x]
    except (OSError, ValueError):
        pass
    return []


def _write_json_list(path: Path, items: list[str]) -> None:
    _atomic_write_private(path, json.dumps(items, ensure_ascii=False, indent=2), mode=0o600)


def export_bundle(paths: AppPaths, *, redact: bool = False) -> dict[str, Any]:
    """构建可移植的配置与策略 Bundle 字典。"""
    cfg = read_config(paths)
    portable_cfg: dict[str, Any] = {}
    for key in PORTABLE_CONFIG_KEYS:
        if key in cfg:
            portable_cfg[key] = cfg[key]

    if redact:
        portable_cfg = redact_value(portable_cfg)

    notify_cfg = load_notify_config(paths)
    bundle: dict[str, Any] = {
        "bundle_version": 1,
        "cproxy_version": __version__,
        "exported_at": datetime.now().isoformat(timespec="seconds"),
        "redacted": redact,
        "config": portable_cfg,
        "custom_rules": [item.to_dict() for item in load_custom_rules(paths)],
        "bans": [item.to_dict() for item in load_active_bans(paths)],
        "notify": notify_cfg.to_dict(redact=redact),
        "ai_manual_order": _read_json_list(ai_manual_order_file(paths)),
        "ai_gemini_order": _read_json_list(ai_gemini_order_file(paths)),
    }
    return bundle


def import_bundle(
    paths: AppPaths,
    source_path: Path,
    *,
    merge: bool = True,
) -> dict[str, Any]:
    """从 Bundle 文件（JSON 或 YAML）导入配置与策略。返回导入统计摘要。"""
    if not source_path.exists():
        raise FileNotFoundError(f"错误: 找不到导入文件: {source_path}")
    raw_text = source_path.read_text(encoding="utf-8")
    try:
        data = yaml.safe_load(raw_text)
    except Exception as exc:
        raise ValueError(f"错误: 无法解析导入文件: {exc}") from exc
    if not isinstance(data, dict) or "bundle_version" not in data:
        raise ValueError("错误: 非法的 cproxy bundle 文件（缺少 bundle_version）")

    # 预先校验 URL 与自定义规则，避免校验失败时留下部分写入
    incoming_cfg = data.get("config")
    validated_cfg_updates: dict[str, Any] = {}
    skipped_redacted_subscriptions = False
    if isinstance(incoming_cfg, dict) and incoming_cfg:
        for key, val in incoming_cfg.items():
            if key not in PORTABLE_CONFIG_KEYS:
                continue
            if key == "subscriptions" and isinstance(val, list):
                kept_entries: list[Any] = []
                for entry in val:
                    if _contains_redacted_sentinel(entry):
                        skipped_redacted_subscriptions = True
                        continue
                    if isinstance(entry, dict):
                        entry_url = str(entry.get("url") or "").strip()
                        if entry_url:
                            _validate_http_url(entry_url, "subscriptions[].url")
                    elif isinstance(entry, str) and entry.strip():
                        _validate_http_url(entry, "subscriptions[]")
                    kept_entries.append(entry)
                if kept_entries or not skipped_redacted_subscriptions:
                    validated_cfg_updates[key] = kept_entries
                continue
            if _contains_redacted_sentinel(val):
                continue
            if key == "subscription-url" and isinstance(val, str) and val.strip():
                _validate_http_url(val, "subscription-url")
            validated_cfg_updates[key] = val

    raw_rules = data.get("custom_rules")
    imported_rules: list[CustomRuleEntry] = []
    if isinstance(raw_rules, list):
        for item in raw_rules:
            if not isinstance(item, dict):
                continue
            rtype = str(item.get("rule_type") or "").strip().upper()
            val = str(item.get("value") or "").strip()
            pol = str(item.get("policy") or "").strip()
            if not (rtype and val and pol):
                continue
            if rtype not in _VALID_RULE_TYPES:
                raise ValueError(f"错误: 导入包含不支持的规则类型: {rtype}")
            if any(ch in val for ch in (",", "\r", "\n")) or any(ch in pol for ch in (",", "\r", "\n")):
                raise ValueError("错误: 导入的自定义规则 value/policy 不允许包含逗号或换行符")
            imported_rules.append(
                CustomRuleEntry(
                    rule_type=rtype,
                    value=val,
                    policy=pol,
                    comment=str(item.get("comment") or "").strip(),
                    created_at=str(item.get("created_at") or "").strip(),
                )
            )

    raw_notify = data.get("notify")
    validated_notify_url: str | None = None
    notify_desktop: bool | None = None
    if isinstance(raw_notify, dict):
        w_url = str(raw_notify.get("webhook_url") or "").strip()
        desktop = bool(raw_notify.get("desktop", False))
        if w_url and not _is_redacted_webhook(w_url):
            validated_notify_url = _validate_http_url(w_url, "notify.webhook_url")
        if validated_notify_url is not None or desktop:
            notify_desktop = desktop

    stats: dict[str, Any] = {
        "config_keys_updated": 0,
        "custom_rules_imported": 0,
        "bans_imported": 0,
        "orders_restored": False,
    }

    # 1. 合并便携配置项（跳过已被脱敏成 ?... 或 [REDACTED] 的字段）
    if isinstance(incoming_cfg, dict) and incoming_cfg:
        cfile = config_file(paths)
        existing_cfg = read_config(paths) if cfile.exists() else {}
        if cfile.exists():
            snapshot_file(paths, cfile, "config")
        updated = 0
        for key, val in validated_cfg_updates.items():
            if (
                key == "subscriptions"
                and skipped_redacted_subscriptions
                and isinstance(val, list)
                and isinstance(existing_cfg.get("subscriptions"), list)
            ):
                incoming_names = {
                    str(item.get("name") or "").strip()
                    for item in val
                    if isinstance(item, dict) and str(item.get("name") or "").strip()
                }
                preserved = [
                    item
                    for item in existing_cfg["subscriptions"]
                    if not (
                        isinstance(item, dict)
                        and str(item.get("name") or "").strip() in incoming_names
                    )
                ]
                existing_cfg[key] = preserved + val
            else:
                existing_cfg[key] = val
            updated += 1
        if updated > 0 or not cfile.exists():
            _atomic_write_private(
                cfile,
                yaml.safe_dump(existing_cfg, allow_unicode=True, sort_keys=False, default_flow_style=False),
                mode=0o600,
            )
        stats["config_keys_updated"] = updated

    # 2. 导入自定义规则
    if isinstance(raw_rules, list):
        if merge:
            current_rules = load_custom_rules(paths)
            seen = {(r.rule_type, r.value.lower()) for r in imported_rules}
            merged_rules = imported_rules + [
                r for r in current_rules if (r.rule_type, r.value.lower()) not in seen
            ]
            save_custom_rules(paths, merged_rules)
        else:
            save_custom_rules(paths, imported_rules)
        stats["custom_rules_imported"] = len(imported_rules)

    # 3. 导入节点屏蔽列表
    raw_bans = data.get("bans")
    if isinstance(raw_bans, list):
        from ..config import node_bans_file

        imported_bans: list[BanEntry] = []
        for item in raw_bans:
            if not isinstance(item, dict):
                continue
            pat = str(item.get("pattern") or "").strip()
            if not pat:
                continue
            exp = item.get("expires_at")
            imported_bans.append(
                BanEntry(
                    pattern=pat,
                    reason=str(item.get("reason") or "").strip(),
                    created_at=str(item.get("created_at") or "").strip(),
                    expires_at=float(exp) if isinstance(exp, (int, float)) else None,
                )
            )
        if merge:
            cur_bans = load_active_bans(paths)
            seen_pats = {b.pattern.lower() for b in imported_bans}
            merged_bans = imported_bans + [b for b in cur_bans if b.pattern.lower() not in seen_pats]
            _save_bans(node_bans_file(paths), merged_bans)
        else:
            _save_bans(node_bans_file(paths), imported_bans)
        stats["bans_imported"] = len(imported_bans)

    # 4. 导入通知配置（未脱敏时）
    if notify_desktop is not None or validated_notify_url is not None:
        save_notify_config(
            paths,
            webhook_url=validated_notify_url,
            desktop=notify_desktop,
        )

    # 5. 导入稳定度排序
    m_order = data.get("ai_manual_order")
    g_order = data.get("ai_gemini_order")
    if isinstance(m_order, list) and m_order:
        _write_json_list(ai_manual_order_file(paths), [str(x) for x in m_order if x])
        stats["orders_restored"] = True
    if isinstance(g_order, list) and g_order:
        _write_json_list(ai_gemini_order_file(paths), [str(x) for x in g_order if x])
        stats["orders_restored"] = True

    return stats
