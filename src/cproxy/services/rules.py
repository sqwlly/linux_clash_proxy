"""`cproxy rule` —— 免改 YAML 的用户自定义分流规则管理。

支持一条命令添加域名后缀、精确域名、关键词、IP-CIDR 或进程分流规则，
持久化于 `state/custom-rules.json`，并在 `render_runtime()` 时置顶注入到所有规则最前端，
确保不会被机场订阅刷新覆盖或被通用规则遮蔽。
"""

from __future__ import annotations

import ipaddress
import json
import os
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import urlparse

from ..config import AppPaths, custom_rules_file

RULE_TYPE_ALIASES: dict[str, str] = {
    "suffix": "DOMAIN-SUFFIX",
    "domain-suffix": "DOMAIN-SUFFIX",
    "domain": "DOMAIN",
    "exact": "DOMAIN",
    "keyword": "DOMAIN-KEYWORD",
    "kw": "DOMAIN-KEYWORD",
    "domain-keyword": "DOMAIN-KEYWORD",
    "cidr": "IP-CIDR",
    "ip-cidr": "IP-CIDR",
    "ip-cidr6": "IP-CIDR6",
    "process": "PROCESS-NAME",
    "proc": "PROCESS-NAME",
    "process-name": "PROCESS-NAME",
}

POLICY_SHORTHANDS: dict[str, str] = {
    "ai": "AI-MANUAL",
    "manual": "AI-MANUAL",
    "ai-manual": "AI-MANUAL",
    "codex": "AI-MANUAL",
    "claude": "AI-MANUAL",
    "chatgpt": "AI-MANUAL",
    "gemini": "AI-GEMINI",
    "ai-gemini": "AI-GEMINI",
    "google": "AI-GEMINI",
    "us": "AI-US",
    "ai-us": "AI-US",
    "sg": "AI-SG",
    "ai-sg": "AI-SG",
    "auto": "AI-AUTO",
    "ai-auto": "AI-AUTO",
    "direct": "DIRECT",
    "直连": "DIRECT",
    "reject": "REJECT",
    "block": "REJECT",
    "拦截": "REJECT",
    "proxy": "MATCH_DEFAULT",
    "default": "MATCH_DEFAULT",
    "默认": "MATCH_DEFAULT",
}


@dataclass(frozen=True)
class CustomRuleEntry:
    rule_type: str
    value: str
    policy: str
    comment: str = ""
    created_at: str = ""

    def format_mihomo_rule(self, match_group: str | None = None) -> str:
        resolved_policy = (match_group or "DIRECT") if self.policy == "MATCH_DEFAULT" else self.policy
        if self.rule_type in {"IP-CIDR", "IP-CIDR6"}:
            return f"{self.rule_type},{self.value},{resolved_policy},no-resolve"
        return f"{self.rule_type},{self.value},{resolved_policy}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule_type": self.rule_type,
            "value": self.value,
            "policy": self.policy,
            "rule": self.format_mihomo_rule(),
            "comment": self.comment,
            "created_at": self.created_at,
        }


def load_custom_rules(paths: AppPaths) -> list[CustomRuleEntry]:
    path = custom_rules_file(paths)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(raw, list):
        return []
    entries: list[CustomRuleEntry] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        rtype = str(item.get("rule_type") or "").strip().upper()
        val = str(item.get("value") or "").strip()
        pol = str(item.get("policy") or "").strip()
        if not (rtype and val and pol):
            continue
        entries.append(
            CustomRuleEntry(
                rule_type=rtype,
                value=val,
                policy=pol,
                comment=str(item.get("comment") or "").strip(),
                created_at=str(item.get("created_at") or "").strip(),
            )
        )
    return entries


def save_custom_rules(paths: AppPaths, entries: list[CustomRuleEntry]) -> None:
    path = custom_rules_file(paths)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    payload = [entry.to_dict() for entry in entries]
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _normalize_policy(policy: str | None) -> str:
    raw = str(policy or "ai").strip()
    if not raw:
        return "AI-MANUAL"
    return POLICY_SHORTHANDS.get(raw.lower(), raw)


def _infer_type_and_value(target: str, explicit_type: str | None = None) -> tuple[str, str]:
    raw = str(target or "").strip()
    if not raw:
        raise ValueError("错误: 规则匹配目标不能为空")

    if explicit_type:
        rtype = RULE_TYPE_ALIASES.get(explicit_type.strip().lower(), explicit_type.strip().upper())
    else:
        rtype = ""

    # 支持前缀简写：proc:curl / kw:openai / exact:api.foo.com
    if not rtype and ":" in raw and not raw.startswith(("http://", "https://")) and "/" not in raw:
        prefix, rest = raw.split(":", 1)
        if prefix.lower() in RULE_TYPE_ALIASES and rest.strip():
            rtype = RULE_TYPE_ALIASES[prefix.lower()]
            raw = rest.strip()

    if "://" in raw:
        parsed = urlparse(raw)
        raw = parsed.hostname or raw

    # 自动识别 IP 或 CIDR
    if not rtype:
        try:
            net = ipaddress.ip_network(raw, strict=False)
            rtype = "IP-CIDR6" if net.version == 6 else "IP-CIDR"
            raw = str(net)
        except ValueError:
            rtype = "DOMAIN-SUFFIX"

    if rtype in {"DOMAIN-SUFFIX", "DOMAIN", "DOMAIN-KEYWORD"}:
        raw = raw.lower().strip().lstrip(".")
        if rtype == "DOMAIN-SUFFIX" and raw.startswith("www.") and raw.count(".") >= 2:
            raw = raw[4:]
    elif rtype in {"IP-CIDR", "IP-CIDR6"}:
        net = ipaddress.ip_network(raw, strict=False)
        rtype = "IP-CIDR6" if net.version == 6 else "IP-CIDR"
        raw = str(net)

    return rtype, raw


def add_custom_rule(
    paths: AppPaths,
    target: str,
    policy: str | None = None,
    *,
    rule_type: str | None = None,
    comment: str = "",
) -> CustomRuleEntry:
    """添加或更新一条自定义分流规则（也支持直接传入完整规则串如 `DOMAIN-SUFFIX,foo.com,DIRECT`）。"""
    raw = str(target or "").strip()
    parts = [p.strip() for p in raw.split(",")]
    if len(parts) >= 3 and parts[0].upper() in set(RULE_TYPE_ALIASES.values()):
        rtype = parts[0].upper()
        val = parts[1]
        pol = _normalize_policy(policy or parts[2])
    else:
        rtype, val = _infer_type_and_value(raw, rule_type)
        pol = _normalize_policy(policy)

    new_entry = CustomRuleEntry(
        rule_type=rtype,
        value=val,
        policy=pol,
        comment=str(comment or "").strip(),
        created_at=datetime.now().isoformat(timespec="seconds"),
    )
    existing = [
        item
        for item in load_custom_rules(paths)
        if not (item.rule_type == rtype and item.value.lower() == val.lower())
    ]
    existing.insert(0, new_entry)
    save_custom_rules(paths, existing)
    return new_entry


def remove_custom_rule(
    paths: AppPaths,
    target: str | None = None,
    *,
    clear_all: bool = False,
) -> list[CustomRuleEntry]:
    existing = load_custom_rules(paths)
    if clear_all:
        save_custom_rules(paths, [])
        return existing
    needle = str(target or "").strip().lower()
    if not needle:
        return []
    removed: list[CustomRuleEntry] = []
    kept: list[CustomRuleEntry] = []
    for idx, entry in enumerate(existing, start=1):
        if (
            str(idx) == needle
            or entry.value.lower() == needle
            or entry.format_mihomo_rule().lower() == needle
            or needle in entry.value.lower()
        ):
            removed.append(entry)
        else:
            kept.append(entry)
    if removed:
        save_custom_rules(paths, kept)
    return removed
