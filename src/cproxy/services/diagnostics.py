from __future__ import annotations

from typing import Iterable
from urllib.request import ProxyHandler, build_opener

from ..backend.api import APIBackend
from ..backend.models import ConnectivityCheckResult, ConnectivityReport, DelayCheckResult, GroupCheckReport
from ..backend.process import ProcessBackend
from ..config import AppPaths, read_config
from ..geodata import check_country_mmdb
from ..proxyenv import proxy_http_url

DEFAULT_TEST_URL = "http://cp.cloudflare.com/generate_204"
DEFAULT_TEST_TIMEOUT = 5000
DEFAULT_CONNECTIVITY_URLS = [
    "https://www.google.com",
    "https://github.com",
]
DEFAULT_IP_CHECK_URLS = [
    "https://api.ip.sb/ip",
    "https://ifconfig.me/ip",
    "https://icanhazip.com",
]


def _config_list(config: dict, key: str, defaults: Iterable[str]) -> list[str]:
    value = config.get(key)
    if isinstance(value, list) and value:
        return [str(item) for item in value]
    return list(defaults)


def _config_int(config: dict, key: str, default: int) -> int:
    value = config.get(key, default)
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


class DiagnosticsService:
    def __init__(self, paths: AppPaths):
        self.paths = paths
        self.api = APIBackend(paths)
        self.process = ProcessBackend(paths)

    def test_group(self, group_name: str) -> GroupCheckReport:
        config = read_config(self.paths)
        groups = self.api.get_groups()
        group = groups.get(group_name)
        if not group:
            raise SystemExit(f"错误: 未找到代理组或节点: {group_name}")

        url = str(config.get("test-url", DEFAULT_TEST_URL))
        timeout = _config_int(config, "test-timeout", DEFAULT_TEST_TIMEOUT)
        results: list[DelayCheckResult] = []
        members = group.candidates or [group_name]
        for member in members:
            try:
                payload = self.api.delay_test(str(member), url, timeout)
                delay = payload.get("delay")
                results.append(DelayCheckResult(name=str(member), ok=True, delay=int(delay)))
            except Exception:
                results.append(DelayCheckResult(name=str(member), ok=False, delay=None))
        return GroupCheckReport(group_name=group_name, results=results)

    def run_connectivity_test(self) -> ConnectivityReport:
        if not self.process.is_running():
            raise RuntimeError("错误: 代理未运行，无法测试")

        config = read_config(self.paths)
        timeout = _config_int(config, "connectivity-timeout", 5)
        connectivity_urls = _config_list(config, "connectivity-test-urls", DEFAULT_CONNECTIVITY_URLS)
        ip_urls = _config_list(config, "ip-check-urls", DEFAULT_IP_CHECK_URLS)

        opener = build_opener(
            ProxyHandler(
                {
                    "http": proxy_http_url(self.paths),
                    "https": proxy_http_url(self.paths),
                }
            )
        )

        results: list[ConnectivityCheckResult] = []
        results.append(check_country_mmdb(self.paths))

        for url in connectivity_urls:
            try:
                with opener.open(url, timeout=timeout) as response:
                    response.read()
                results.append(ConnectivityCheckResult(name=url, ok=True, detail="成功"))
            except Exception:
                results.append(ConnectivityCheckResult(name=url, ok=False, detail="失败"))

        exit_ip: str | None = None
        for url in ip_urls:
            try:
                with opener.open(url, timeout=timeout) as response:
                    value = response.read().decode("utf-8").strip()
                if value:
                    exit_ip = value
                    break
            except Exception:
                continue

        results.append(
            ConnectivityCheckResult(
                name="出口 IP",
                ok=exit_ip is not None,
                detail=exit_ip or "获取失败",
            )
        )
        return ConnectivityReport(results=results, exit_ip=exit_ip)
