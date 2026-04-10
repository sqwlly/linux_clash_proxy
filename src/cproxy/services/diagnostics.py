from __future__ import annotations

import socket
import time
from typing import Iterable
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener

from ..backend.api import APIBackend
from ..backend.models import AIProbeReport, AIProbeResult, ConnectivityCheckResult, ConnectivityReport, DelayCheckResult, GroupCheckReport
from ..backend.process import ProcessBackend
from ..config import AppPaths, read_config
from ..geodata import check_country_mmdb
from ..proxyenv import proxy_http_url

DEFAULT_TEST_URL = "https://cp.cloudflare.com/generate_204"
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
DEFAULT_AI_PROBE_TARGETS = [
    ("ChatGPT Web", "https://chatgpt.com"),
    ("OpenAI API", "https://api.openai.com/v1/models"),
]
DEFAULT_AI_PROBE_TIMEOUT = 8
DEFAULT_AI_PROBE_RETRIES = 2
DEFAULT_AI_PROBE_BACKOFFS = (0.2, 0.5)


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


def _config_str(config: dict, key: str, default: str) -> str:
    value = config.get(key, default)
    text = str(value).strip()
    return text or default


def _proxy_opener(paths: AppPaths):
    return build_opener(
        ProxyHandler(
            {
                "http": proxy_http_url(paths),
                "https": proxy_http_url(paths),
            }
        )
    )


def _probe_failure_detail(exc: Exception) -> str:
    if isinstance(exc, socket.timeout):
        return "超时"

    text = str(exc).strip()
    if "timed out" in text.lower():
        return "超时"
    if text:
        return text
    return "连接失败"


def _probe_target(opener, url: str, timeout: int) -> tuple[bool, str]:
    for attempt in range(DEFAULT_AI_PROBE_RETRIES + 1):
        request = Request(url, headers={"User-Agent": "cproxy/0.1.0"})
        try:
            with opener.open(request, timeout=timeout) as response:
                status = getattr(response, "status", response.getcode())
            return True, f"HTTP {status}" if status else "成功"
        except HTTPError as exc:
            ok = 400 <= exc.code < 500
            if ok or attempt == DEFAULT_AI_PROBE_RETRIES:
                return ok, f"HTTP {exc.code}"
            time.sleep(DEFAULT_AI_PROBE_BACKOFFS[attempt])
        except (URLError, OSError) as exc:
            if attempt == DEFAULT_AI_PROBE_RETRIES:
                return False, _probe_failure_detail(exc)
            time.sleep(DEFAULT_AI_PROBE_BACKOFFS[attempt])

    return False, "连接失败"


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

        opener = _proxy_opener(self.paths)

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

    def run_ai_probe(self) -> AIProbeReport:
        config = read_config(self.paths)
        timeout = _config_int(
            config,
            "ai-probe-timeout",
            _config_int(config, "connectivity-timeout", DEFAULT_AI_PROBE_TIMEOUT),
        )
        targets = [
            ("ChatGPT Web", _config_str(config, "ai-chatgpt-url", DEFAULT_AI_PROBE_TARGETS[0][1])),
            ("OpenAI API", _config_str(config, "ai-openai-api-url", DEFAULT_AI_PROBE_TARGETS[1][1])),
        ]
        opener = _proxy_opener(self.paths)

        results: list[AIProbeResult] = []
        for name, url in targets:
            ok, detail = _probe_target(opener, url, timeout)
            results.append(AIProbeResult(name=name, url=url, ok=ok, detail=detail))
        return AIProbeReport(results=results)
