from __future__ import annotations

from pathlib import Path

import re
import yaml

from ..config import AppPaths, config_file, runtime_file
from ..snapshots import snapshot_file
from .api import APIBackend
from .models import ProxyGroup

AI_MANUAL_GROUP = "AI-MANUAL"
AI_AUTO_GROUP = "AI-AUTO"
AI_US_GROUP = "AI-US"
AI_SG_GROUP = "AI-SG"
AI_REGION_US = "🇺🇸 United States"
AI_REGION_SG = "🇸🇬 Singapore"
TEST_URL = "https://cp.cloudflare.com/generate_204"
SECRET_PROVIDER_KEYS = {
    "secret-file",
    "secret-systemd-credential",
    "secret-keyring-service",
    "secret-keyring-username",
}

# 当原始订阅没有提供标准区域组时，根据节点名称自动归纳生成。
REGION_PATTERNS = {
    AI_REGION_US: (r"🇺🇸", r"United States", r"美国", r"\bUS\b", r"\bUSA\b"),
    AI_REGION_SG: (r"🇸🇬", r"Singapore", r"新加坡", r"\bSG\b"),
}


def _proxy_matches_region(proxy_name: str, patterns: tuple[str, ...]) -> bool:
    return any(re.search(pat, proxy_name, re.IGNORECASE) for pat in patterns)


def _ensure_region_groups(
    groups: list[dict],
    group_map: dict[str, dict],
    proxies: list[dict] | None,
    required_names: tuple[str, ...],
) -> None:
    proxies = proxies or []
    for name in required_names:
        if name in group_map:
            continue
        patterns = REGION_PATTERNS.get(name)
        if not patterns:
            continue
        members = [
            str(proxy["name"])
            for proxy in proxies
            if isinstance(proxy, dict)
            and proxy.get("name")
            and _proxy_matches_region(str(proxy["name"]), patterns)
        ]
        if members:
            group = {"name": name, "type": "select", "proxies": members}
            groups.append(group)
            group_map[name] = group


def _sanitize_dns_fallback_filter(data: dict) -> None:
    # 部分订阅的 dns.fallback-filter.geosite 依赖 GeoSite.dat；该文件在本地
    # 时常损坏或下载超时，会导致 mihomo 启动卡住。清理该字段以保持启动稳定。
    dns = data.get("dns")
    if not isinstance(dns, dict):
        return
    fallback_filter = dns.get("fallback-filter")
    if isinstance(fallback_filter, dict):
        fallback_filter.pop("geosite", None)


class RuntimeBackend:
    def __init__(self, paths: AppPaths):
        self.paths = paths

    def get_groups(self) -> dict[str, ProxyGroup]:
        path = runtime_file(self.paths)
        if not path.exists():
            raise FileNotFoundError(f"runtime config not found: {path}")

        with path.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}

        groups: dict[str, ProxyGroup] = {}
        for item in data.get("proxy-groups", []):
            if not isinstance(item, dict):
                continue
            name = item.get("name")
            if not name:
                continue
            proxies = [str(proxy) for proxy in item.get("proxies", [])]
            groups[str(name)] = ProxyGroup(
                name=str(name),
                type=str(item.get("type", "")),
                current=proxies[0] if proxies else "-",
                candidates=proxies,
                source="runtime",
            )
        return groups

    def render_runtime(self) -> Path:
        source_path = config_file(self.paths)
        target_path = runtime_file(self.paths)

        with source_path.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}

        _sanitize_dns_fallback_filter(data)

        secret = APIBackend(self.paths).api_secret()
        if secret:
            data["secret"] = secret
        for key in SECRET_PROVIDER_KEYS:
            data.pop(key, None)

        groups = data.get("proxy-groups") or []
        if not isinstance(groups, list):
            raise ValueError("proxy-groups 必须是列表")

        group_map = {group["name"]: group for group in groups if isinstance(group, dict) and group.get("name")}
        _ensure_region_groups(groups, group_map, data.get("proxies"), (AI_REGION_US, AI_REGION_SG))

        for required in (AI_REGION_US, AI_REGION_SG):
            if required not in group_map:
                raise ValueError(f"原始配置缺少必需的区域组: {required}")

        us_proxies = group_map[AI_REGION_US].get("proxies") or []
        sg_proxies = group_map[AI_REGION_SG].get("proxies") or []
        if not us_proxies or not sg_proxies:
            raise ValueError("美国或新加坡区域组未包含任何节点")

        ai_groups = [
            {"name": AI_US_GROUP, "type": "fallback", "proxies": us_proxies, "url": TEST_URL, "interval": 300},
            {"name": AI_SG_GROUP, "type": "fallback", "proxies": sg_proxies, "url": TEST_URL, "interval": 300},
            {"name": AI_AUTO_GROUP, "type": "fallback", "proxies": [AI_US_GROUP, AI_SG_GROUP], "url": TEST_URL, "interval": 300},
            {"name": AI_MANUAL_GROUP, "type": "select", "proxies": [AI_AUTO_GROUP, AI_US_GROUP, AI_SG_GROUP, AI_REGION_US, AI_REGION_SG]},
        ]

        managed_names = {AI_MANUAL_GROUP, AI_AUTO_GROUP, AI_US_GROUP, AI_SG_GROUP}
        filtered_groups = [group for group in groups if not (isinstance(group, dict) and group.get("name") in managed_names)]

        insert_after = None
        for idx, group in enumerate(filtered_groups):
            if isinstance(group, dict) and group.get("name") in {"Auto", "SSRDOG"}:
                insert_after = idx
                break

        if insert_after is None:
            filtered_groups = ai_groups + filtered_groups
        else:
            filtered_groups = filtered_groups[: insert_after + 1] + ai_groups + filtered_groups[insert_after + 1 :]

        ai_rules = [
            f"DOMAIN-SUFFIX,openai.com,{AI_MANUAL_GROUP}",
            f"DOMAIN-SUFFIX,chatgpt.com,{AI_MANUAL_GROUP}",
            f"DOMAIN-SUFFIX,oaistatic.com,{AI_MANUAL_GROUP}",
            f"DOMAIN-SUFFIX,oaiusercontent.com,{AI_MANUAL_GROUP}",
            f"DOMAIN-SUFFIX,anthropic.com,{AI_MANUAL_GROUP}",
            f"DOMAIN-SUFFIX,claude.ai,{AI_MANUAL_GROUP}",
            f"DOMAIN,gemini.google.com,{AI_MANUAL_GROUP}",
            f"DOMAIN,aistudio.google.com,{AI_MANUAL_GROUP}",
            f"DOMAIN,ai.google.dev,{AI_MANUAL_GROUP}",
            f"DOMAIN,generativelanguage.googleapis.com,{AI_MANUAL_GROUP}",
        ]
        mainland_direct = ["GEOIP,CN,DIRECT,no-resolve"]
        rules = data.get("rules") or []

        clean_rules = [rule for rule in rules if rule not in ai_rules and rule not in mainland_direct]
        insert_index = None
        for idx, rule in enumerate(clean_rules):
            if rule == "RULE-SET,ChinaMax,DIRECT" or (isinstance(rule, str) and rule.startswith("MATCH,")):
                insert_index = idx
                break
        if insert_index is None:
            clean_rules.extend(ai_rules)
        else:
            clean_rules = clean_rules[:insert_index] + ai_rules + clean_rules[insert_index:]

        match_index = None
        for idx, rule in enumerate(clean_rules):
            if isinstance(rule, str) and rule.startswith("MATCH,"):
                match_index = idx
                break
        if match_index is None:
            clean_rules.extend(mainland_direct)
        else:
            clean_rules = clean_rules[:match_index] + mainland_direct + clean_rules[match_index:]

        data["proxy-groups"] = filtered_groups
        data["rules"] = clean_rules

        rendered = yaml.safe_dump(data, allow_unicode=True, sort_keys=False, default_flow_style=False)
        target_path.parent.mkdir(parents=True, exist_ok=True)
        # 内容未变化时不重写也不留快照，避免定时任务把快照历史掏空
        if target_path.exists() and target_path.read_text(encoding="utf-8") == rendered:
            return target_path
        # 覆盖运行配置前自动留快照，供 cproxy rollback 回滚
        snapshot_file(self.paths, target_path, "runtime")
        target_path.write_text(rendered, encoding="utf-8")
        return target_path
