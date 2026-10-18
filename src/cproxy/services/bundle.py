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
from datetime import datetime
from pathlib import Path
from typing import Any

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
from .rules import CustomRuleEntry, load_custom_rules, save_custom_rules

PORTABLE_CONFIG_KEYS = (
    "subscription-url",
    "subscriptions",
    "ai-manual-prefer",
    "ai-gemini-prefer",
    "max-ai-multiplier",
    "refresh-groups",
    "tun",
    "mixed-port",
    "mode",
    "log-level",
    "output-color",
    "output-icons",
)


def _read_json_list(path: Path) -> list[str]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(raw, list):
            return [str(x) for x in raw if x]
    except (OSError, ValueError):
        pass
    return []


def _write_json_list(path: Path, items: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


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

    stats: dict[str, Any] = {
        "config_keys_updated": 0,
        "custom_rules_imported": 0,
        "bans_imported": 0,
        "orders_restored": False,
    }

    # 1. 合并便携配置项（跳过已被脱敏成 ... 或 [REDACTED] 的字段）
    incoming_cfg = data.get("config")
    if isinstance(incoming_cfg, dict) and incoming_cfg:
        cfile = config_file(paths)
        existing_cfg = read_config(paths) if cfile.exists() else {}
        if cfile.exists():
            snapshot_file(paths, cfile, "config")
        updated = 0
        for key, val in incoming_cfg.items():
            if key not in PORTABLE_CONFIG_KEYS:
                continue
            val_str = str(val)
            if "[REDACTED]" in val_str or val_str.endswith("?..."):
                continue
            existing_cfg[key] = val
            updated += 1
        if updated > 0 or not cfile.exists():
            cfile.parent.mkdir(parents=True, exist_ok=True)
            tmp = cfile.with_suffix(".tmp")
            tmp.write_text(
                yaml.safe_dump(existing_cfg, allow_unicode=True, sort_keys=False, default_flow_style=False),
                encoding="utf-8",
            )
            os.replace(tmp, cfile)
        stats["config_keys_updated"] = updated

    # 2. 导入自定义规则
    raw_rules = data.get("custom_rules")
    if isinstance(raw_rules, list):
        imported_rules: list[CustomRuleEntry] = []
        for item in raw_rules:
            if not isinstance(item, dict):
                continue
            rtype = str(item.get("rule_type") or "").strip().upper()
            val = str(item.get("value") or "").strip()
            pol = str(item.get("policy") or "").strip()
            if rtype and val and pol:
                imported_rules.append(
                    CustomRuleEntry(
                        rule_type=rtype,
                        value=val,
                        policy=pol,
                        comment=str(item.get("comment") or "").strip(),
                        created_at=str(item.get("created_at") or "").strip(),
                    )
                )
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
    raw_notify = data.get("notify")
    if isinstance(raw_notify, dict):
        w_url = str(raw_notify.get("webhook_url") or "").strip()
        desktop = bool(raw_notify.get("desktop", False))
        if (w_url and "***" not in w_url and "?..." not in w_url) or desktop:
            save_notify_config(
                paths,
                webhook_url=w_url if ("***" not in w_url and "?..." not in w_url) else None,
                desktop=desktop,
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
