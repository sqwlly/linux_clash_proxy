"""`cproxy why <domain_or_url>` —— 域名/URL/IP 分流规则与出口链路追查服务。

给定域名、完整 URL 或 IP（可选附加 `--process` 进程名），按当前生效的运行配置
顺序匹配 `rules`，结合 Mihomo `/proxies` 下钻出完整策略链路与最终出口节点延迟，
并附带当前活跃连接中的实测命中情况。
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from ..backend.api import APIBackend, APIUnavailableError
from ..backend.models import ProxyGroup
from ..backend.runtime import SPECIAL_POLICY_LEAVES, RuntimeBackend
from ..config import AppPaths, config_file, load_yaml_file, runtime_file
from .ops import _process_basename


def _strip_port(hostport: str) -> str:
    text = hostport.strip()
    if text.startswith("[") and "]" in text:
        return text[1 : text.index("]")]
    if text.count(":") == 1:
        host, port = text.rsplit(":", 1)
        if port.isdigit():
            return host
    return text


@dataclass
class ActiveHostConnection:
    process: str
    destination: str
    rule: str
    chain: list[str]
    download: int = 0
    upload: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "process": self.process,
            "destination": self.destination,
            "rule": self.rule,
            "chain": self.chain,
            "download": self.download,
            "upload": self.upload,
        }


@dataclass
class WhyReport:
    query: str
    host: str
    process: str | None
    matched_rule: str
    rule_index: int
    policy_group: str
    chain: list[str]
    exit_node: str
    exit_delay: int | None = None
    exit_alive: bool | None = None
    source: str = "runtime"
    active_connections: list[ActiveHostConnection] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "host": self.host,
            "process": self.process,
            "matched_rule": self.matched_rule,
            "rule_index": self.rule_index,
            "policy_group": self.policy_group,
            "chain": self.chain,
            "exit_node": self.exit_node,
            "exit_delay": self.exit_delay,
            "exit_alive": self.exit_alive,
            "source": self.source,
            "active_connections": [item.to_dict() for item in self.active_connections],
        }


def extract_host(query: str) -> str:
    """从 URL、host:port 或纯域名/IP 中提取规范化小写主机名。"""
    raw = str(query or "").strip()
    if not raw:
        raise ValueError("错误: 请提供要追查的域名、URL 或 IP")
    try:
        if "://" in raw:
            parsed = urlparse(raw)
            host = parsed.hostname or ""
        elif any(ch in raw for ch in ("/", "?", "#", "@")):
            parsed = urlparse(f"https://{raw}")
            host = parsed.hostname or ""
        else:
            host = _strip_port(raw)
    except ValueError as exc:
        raise ValueError(f"错误: 无法从输入中解析主机名: {query}") from exc
    host = host.strip().strip("[]").lower().rstrip(".")
    if not host:
        raise ValueError(f"错误: 无法从输入中解析主机名: {query}")
    return host


def _parse_ip(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        return None


def _safe_int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _match_domain_pattern(pattern: str, host: str) -> bool:
    entry = pattern.strip().lower()
    if not entry:
        return False
    if entry.startswith("+."):
        suffix = entry[2:].lstrip(".")
        return bool(suffix) and (host == suffix or host.endswith(f".{suffix}"))
    if entry.startswith("*."):
        suffix = entry[2:].lstrip(".")
        if not suffix or not host.endswith(f".{suffix}"):
            return False
        prefix = host[: -(len(suffix) + 1)]
        return bool(prefix) and "." not in prefix
    if entry.startswith("."):
        suffix = entry.lstrip(".")
        return bool(suffix) and host.endswith(f".{suffix}")
    return host == entry


def _resolve_ruleset_path(
    paths: AppPaths,
    provider_name: str,
    provider_cfg: dict[str, Any] | None = None,
) -> Path | None:
    """安全定位本地 ruleset 文件，严禁目录穿越或逃逸出 `ruleset/` 目录。"""
    name = str(provider_name or "").strip()
    if not name or "/" in name or "\\" in name or ".." in name or Path(name).name != name:
        return None
    ruleset_dir = (paths.data_dir / "ruleset").resolve()
    candidates: list[Path] = []
    if isinstance(provider_cfg, dict):
        raw_path = str(provider_cfg.get("path") or "").strip()
        if raw_path and "\\" not in raw_path:
            rel = Path(raw_path)
            if not rel.is_absolute():
                candidates.append(paths.data_dir / rel)
    candidates.extend(
        [
            paths.data_dir / "ruleset" / f"{name}.yaml",
            paths.data_dir / "ruleset" / f"{name}.yml",
        ]
    )
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        if not resolved.is_relative_to(ruleset_dir):
            continue
        if resolved.is_file():
            return resolved
    return None


def _match_ruleset(
    paths: AppPaths,
    provider_name: str,
    host: str,
    ip_obj: Any,
    *,
    rule_providers: dict[str, Any] | None = None,
) -> bool:
    """尝试在本地 ruleset 文件（支持 classical / domain / ipcidr）中匹配域名或 IP。"""
    provider_cfg = (
        rule_providers.get(provider_name)
        if isinstance(rule_providers, dict) and isinstance(rule_providers.get(provider_name), dict)
        else None
    )
    ruleset_path = _resolve_ruleset_path(paths, provider_name, provider_cfg)
    if ruleset_path is None:
        return False
    try:
        data = load_yaml_file(ruleset_path)
    except Exception:
        return False
    payload = data.get("payload") if isinstance(data, dict) else None
    if not isinstance(payload, list):
        return False
    behavior = str((provider_cfg or {}).get("behavior") or "classical").strip().lower()
    for entry in payload:
        if not isinstance(entry, str):
            continue
        cleaned = entry.strip()
        if not cleaned:
            continue
        if behavior == "domain":
            if _match_domain_pattern(cleaned, host):
                return True
        elif behavior == "ipcidr":
            if ip_obj is not None:
                try:
                    if ip_obj in ipaddress.ip_network(cleaned, strict=False):
                        return True
                except ValueError:
                    continue
        else:
            if _rule_matches_target(cleaned, host, ip_obj, process=None, paths=None):
                return True
    return False


def _rule_matches_target(
    rule: str,
    host: str,
    ip_obj: ipaddress.IPv4Address | ipaddress.IPv6Address | None,
    *,
    process: str | None,
    paths: AppPaths | None,
    rule_providers: dict[str, Any] | None = None,
) -> bool:
    parts = [item.strip() for item in rule.split(",")]
    if not parts:
        return False
    rtype = parts[0].upper()
    if rtype == "MATCH":
        return True
    if len(parts) < 2:
        return False
    pattern = parts[1]
    pattern_lower = pattern.lower()

    if rtype == "DOMAIN":
        return host == pattern_lower
    if rtype == "DOMAIN-SUFFIX":
        suffix = pattern_lower.lstrip(".")
        return host == suffix or host.endswith(f".{suffix}")
    if rtype == "DOMAIN-KEYWORD":
        return pattern_lower in host
    if rtype in {"IP-CIDR", "IP-CIDR6"} and ip_obj is not None:
        try:
            net = ipaddress.ip_network(pattern, strict=False)
            return ip_obj in net
        except ValueError:
            return False
    if rtype == "PROCESS-NAME" and process:
        proc_base = _process_basename({"process": process}).lower()
        return proc_base != "-" and proc_base == pattern_lower
    if rtype == "PROCESS-NAME-REGEX" and process:
        proc_base = _process_basename({"process": process})
        try:
            return (proc_base != "-" and bool(re.search(pattern, proc_base))) or bool(re.search(pattern, process))
        except re.error:
            return False
    if rtype == "RULE-SET" and paths is not None:
        return _match_ruleset(paths, pattern, host, ip_obj, rule_providers=rule_providers)
    if rtype == "GEOIP" and pattern.upper() == "CN":
        if host.endswith(".cn"):
            return True
    return False


def _rule_target_group(rule: str) -> str:
    parts = [item.strip() for item in rule.split(",")]
    if not parts:
        return "DIRECT"
    rtype = parts[0].upper()
    if rtype == "MATCH":
        return parts[1] if len(parts) >= 2 and parts[1] else "DIRECT"
    if len(parts) >= 3 and parts[2]:
        return parts[2]
    return "DIRECT"


def _resolve_chain(
    policy_group: str,
    groups: dict[str, ProxyGroup],
) -> tuple[list[str], str, int | None, bool | None]:
    """从策略组沿 `current` 逐级下钻到最终出口节点。"""
    if policy_group in SPECIAL_POLICY_LEAVES:
        return [policy_group], policy_group, None, True

    chain: list[str] = [policy_group]
    seen: set[str] = {policy_group}
    curr = policy_group
    while len(chain) <= 10:
        grp = groups.get(curr)
        if grp is None or not grp.candidates:
            break
        nxt = str(grp.current or "").strip()
        if not nxt or nxt == "-" or nxt in seen:
            break
        chain.append(nxt)
        seen.add(nxt)
        curr = nxt

    leaf = chain[-1]
    leaf_entry = groups.get(leaf)
    delay = leaf_entry.delay if leaf_entry is not None else None
    alive = leaf_entry.alive if leaf_entry is not None else None
    if delay is None and policy_group in groups:
        delay = groups[policy_group].delay
    if alive is None and policy_group in groups:
        alive = groups[policy_group].alive
    return chain, leaf, delay, alive


def _collect_active_host_connections(
    api: APIBackend,
    host: str,
) -> list[ActiveHostConnection]:
    try:
        payload = api.get_connections()
    except Exception:
        return []
    raw_conns = payload.get("connections") if isinstance(payload, dict) else []
    if not isinstance(raw_conns, list) or not raw_conns:
        return []

    matched: list[ActiveHostConnection] = []
    for item in raw_conns:
        if not isinstance(item, dict):
            continue
        meta = item.get("metadata") or {}
        if not isinstance(meta, dict):
            continue
        conn_host = str(meta.get("host") or meta.get("sniffHost") or "").strip().lower()
        dst_ip = str(meta.get("destinationIP") or meta.get("remoteDestination") or "").strip().lower()
        target_str = conn_host or dst_ip
        if not target_str:
            continue
        if not (
            target_str == host
            or target_str.endswith(f".{host}")
            or host.endswith(f".{target_str}")
            or dst_ip == host
        ):
            continue

        proc = _process_basename(meta)
        rule_name = str(item.get("rule") or "").strip()
        rule_payload = str(item.get("rulePayload") or "").strip()
        rule_desc = f"{rule_name},{rule_payload}" if rule_payload else (rule_name or "-")

        raw_chains_field = item.get("chains")
        raw_chains = (
            [str(c) for c in raw_chains_field if c] if isinstance(raw_chains_field, list) else []
        )
        # mihomo chains 是倒序（[叶子节点, ..., 入口策略组]），翻转为正序展示
        chain = list(reversed(raw_chains))
        matched.append(
            ActiveHostConnection(
                process=proc,
                destination=target_str,
                rule=rule_desc,
                chain=chain,
                download=_safe_int(item.get("download")),
                upload=_safe_int(item.get("upload")),
            )
        )
    return matched


def explain_route(
    paths: AppPaths,
    query: str,
    *,
    process: str | None = None,
) -> WhyReport:
    """追查指定域名/URL/IP 的分流规则命中与最终出口链路。"""
    host = extract_host(query)
    ip_obj = _parse_ip(host)

    rfile = runtime_file(paths)
    cfile = config_file(paths)
    try:
        data = load_yaml_file(rfile) if rfile.exists() else load_yaml_file(cfile)
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {}

    raw_rules = data.get("rules")
    rules = [str(item) for item in raw_rules if isinstance(item, str)] if isinstance(raw_rules, list) else []
    raw_providers = data.get("rule-providers")
    rule_providers = raw_providers if isinstance(raw_providers, dict) else {}
    api = APIBackend(paths)
    source = "runtime"
    try:
        groups = api.get_groups(request_timeout=1.5)
        source = "live"
    except APIUnavailableError:
        try:
            groups = RuntimeBackend(paths).get_groups()
        except Exception:
            groups = {}

    active_conns = _collect_active_host_connections(api, host) if source == "live" else []
    effective_process = process
    if not effective_process and active_conns:
        for conn in active_conns:
            if conn.process and conn.process != "-":
                effective_process = conn.process
                break

    matched_rule = "MATCH,DIRECT"
    rule_index = 0
    policy_group = "DIRECT"

    for idx, rule in enumerate(rules, start=1):
        if _rule_matches_target(
            rule,
            host,
            ip_obj,
            process=effective_process,
            paths=paths,
            rule_providers=rule_providers,
        ):
            matched_rule = rule
            rule_index = idx
            policy_group = _rule_target_group(rule)
            break

    chain, exit_node, exit_delay, exit_alive = _resolve_chain(policy_group, groups)
    return WhyReport(
        query=query,
        host=host,
        process=effective_process,
        matched_rule=matched_rule,
        rule_index=rule_index,
        policy_group=policy_group,
        chain=chain,
        exit_node=exit_node,
        exit_delay=exit_delay,
        exit_alive=exit_alive,
        source=source,
        active_connections=active_conns,
    )
