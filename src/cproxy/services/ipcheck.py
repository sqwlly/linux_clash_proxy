from __future__ import annotations

import http.client
import json
import math
import sys
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import ProxyHandler, Request, build_opener

from ..backend.api import APIBackend
from ..config import AppPaths
from ..proxyenv import proxy_http_url

IPOK_API = "https://ipok.io/api/ip"
DEFAULT_TIMEOUT = 30
DEFAULT_CHECK_GROUP = "CyberGuard"
MAX_IPOK_RESPONSE_BYTES = 1_048_576

RISK_BANDS: tuple[tuple[int, str], ...] = (
    (20, "纯净"),
    (40, "一般"),
    (60, "注意"),
)

SERVICE_STATUS_LABELS = {
    "available": "可用",
    "restricted": "受限",
    "blocked": "封锁",
    "unknown": "未知",
}


class IpCheckError(RuntimeError):
    pass


def risk_verdict(risk: int | None) -> str:
    if risk is None:
        return "未知"
    for threshold, label in RISK_BANDS:
        if risk < threshold:
            return label
    return "高风险"


def _to_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _to_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


@dataclass(frozen=True)
class SourceScore:
    source: str
    risk: int
    weight: float


@dataclass(frozen=True)
class ServiceStatus:
    key: str
    status: str

    @property
    def label(self) -> str:
        return SERVICE_STATUS_LABELS.get(self.status, self.status)


@dataclass(frozen=True)
class IpPurityReport:
    ip: str
    country: str
    city: str
    isp: str
    asn: str
    ip_type: str
    usage_type: str
    native_type: str
    risk: int | None
    verdict: str
    signals: list[str] = field(default_factory=list)
    sources: list[SourceScore] = field(default_factory=list)
    blocklist_listed: list[str] = field(default_factory=list)
    blocklist_checked: int = 0
    shared_users: str = ""
    shared_quality: str = ""
    datacenter: str = ""
    services: list[ServiceStatus] = field(default_factory=list)
    switched: bool = False
    switch_group: str = ""
    previous_selection: str = ""

    @property
    def ai_services(self) -> list[ServiceStatus]:
        return [item for item in self.services if item.key.lower() in {"chatgpt", "claude", "gemini", "copilot"}]


def _text(payload: dict[str, Any], *keys: str, default: str = "-") -> str:
    for key in keys:
        value = payload.get(key)
        if value not in (None, ""):
            return str(value)
    return default


def parse_report(payload: dict[str, Any]) -> IpPurityReport:
    if not isinstance(payload, dict):
        raise IpCheckError("错误: ipok.io 返回格式异常（期望 JSON 对象）")
    geo = _as_dict(payload.get("geo"))
    breakdown = _as_dict(payload.get("riskBreakdown"))
    blocklist = _as_dict(payload.get("blocklist"))
    shared = _as_dict(payload.get("sharedUsers"))
    intel = _as_dict(payload.get("intel"))
    datacenter = _as_dict(intel.get("datacenter"))
    risk = _to_int(payload.get("risk"))
    services = [
        ServiceStatus(str(item.get("key", "?")), str(item.get("status", "unknown")))
        for item in _as_list(payload.get("services"))
        if isinstance(item, dict)
    ]
    sources = [
        SourceScore(
            str(item.get("source", "?")),
            _to_int(item.get("risk")) or 0,
            _to_float(item.get("weight")) or 0.0,
        )
        for item in _as_list(breakdown.get("contributors"))
        if isinstance(item, dict)
    ]
    return IpPurityReport(
        ip=_text(geo, "ip"),
        country=_text(geo, "country"),
        city=_text(geo, "city"),
        isp=_text(geo, "isp"),
        asn=_text(geo, "asn"),
        ip_type=_text(payload, "ipType", default="unknown"),
        usage_type=_text(payload, "usageType", default="unknown"),
        native_type=_text(payload, "nativeType", default="unknown"),
        risk=risk,
        verdict=risk_verdict(risk),
        signals=[str(item) for item in _as_list(payload.get("signals"))],
        sources=sources,
        blocklist_listed=[str(item) for item in _as_list(blocklist.get("listed"))],
        blocklist_checked=_to_int(blocklist.get("checked")) or 0,
        shared_users=str(shared.get("range") or ""),
        shared_quality=str(shared.get("quality") or ""),
        datacenter=str(datacenter.get("name") or ""),
        services=services,
    )


class IpCheckService:
    def __init__(self, paths: AppPaths):
        self.paths = paths
        self.api = APIBackend(paths)

    def fetch_report(self, ip: str | None, timeout: int) -> dict[str, Any]:
        query = f"?{urlencode({'ip': ip})}" if ip else ""
        url = f"{IPOK_API}{query}"
        request = Request(url, headers={"Accept": "application/json", "User-Agent": "cproxy-ipcheck/1.0"})
        opener = build_opener(ProxyHandler({"http": proxy_http_url(self.paths), "https": proxy_http_url(self.paths)}))
        try:
            with opener.open(request, timeout=timeout) as response:
                raw = response.read(MAX_IPOK_RESPONSE_BYTES)
                payload = json.loads(raw.decode("utf-8"))
        except HTTPError as exc:
            if exc.code == 429:
                retry_after = exc.headers.get("Retry-After") if exc.headers else None
                hint = f"，请 {retry_after} 秒后重试" if retry_after else "（ipok 免费额度已用尽，UTC 日重置）"
                raise IpCheckError(f"错误: ipok.io 限流{hint}") from exc
            if exc.code == 400:
                raise IpCheckError(f"错误: ipok.io 拒绝了请求参数（检查 IP 格式: {ip or '-'}）") from exc
            raise IpCheckError(f"错误: ipok.io 返回 HTTP {exc.code}") from exc
        except (URLError, OSError, http.client.HTTPException, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise IpCheckError(f"错误: ipok.io 请求失败: {exc}") from exc
        if not isinstance(payload, dict):
            raise IpCheckError("错误: ipok.io 返回格式异常（期望 JSON 对象）")
        return payload

    def check(
        self,
        ip: str | None = None,
        group: str | None = None,
        node: str | None = None,
        timeout: int = DEFAULT_TIMEOUT,
        warn: Callable[[str], None] | None = None,
    ) -> IpPurityReport:
        timeout = max(1, timeout)
        if node and not group:
            group = DEFAULT_CHECK_GROUP
        previous: str | None = None
        switched = False
        try:
            if group and node:
                current = self.api.get_groups().get(group)
                if current is None:
                    raise IpCheckError(f"错误: 未找到代理组: {group}")
                if node not in current.candidates:
                    raise IpCheckError(f"错误: 组 {group} 中不存在节点: {node}")
                previous = current.current
                if previous != node:
                    self.api.switch_group(group, node)
                    switched = True
            payload = self.fetch_report(ip, timeout)
            report = parse_report(payload)
            if switched and previous:
                return replace(
                    report,
                    switched=True,
                    switch_group=group or "",
                    previous_selection=previous,
                )
            return report
        finally:
            if switched and previous:
                try:
                    self.api.switch_group(group or "", previous)
                except Exception as exc:  # noqa: BLE001 - 恢复失败不掩盖检测结果，但必须告知用户
                    message = (
                        f"警告: 检测后恢复 {group} -> {previous} 失败 ({exc})，请手动执行 "
                        f"cproxy switch {group} {previous}"
                    )
                    # 调用方可传入 warn 收集（如 Spinner 场景退出后再打印），默认直接写 stderr
                    (warn or (lambda line: print(line, file=sys.stderr)))(message)
