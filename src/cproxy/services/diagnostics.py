from __future__ import annotations

import socket
import time
from collections.abc import Callable, Iterable
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener

from .. import __version__
from ..backend.api import APIBackend
from ..backend.models import (
    AIProbeReport,
    AIProbeResult,
    ConnectivityCheckResult,
    ConnectivityReport,
    DelayCheckResult,
    GroupCheckReport,
)
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
    ("Gemini API", "https://generativelanguage.googleapis.com/v1beta/models"),
]
DEFAULT_AI_PROBE_TIMEOUT = 8
DEFAULT_AI_PROBE_RETRIES = 2
DEFAULT_AI_PROBE_BACKOFFS = (0.2, 0.5)

# 语义探测判定结果：ok=可达且可用，geo_blocked=可达但被地区门禁（Gemini 常见），dead=不可达
PROBE_OK = "ok"
PROBE_GEO_BLOCKED = "geo_blocked"
PROBE_DEAD = "dead"

# Gemini/Google 地区门禁的错误标记（响应体小写后做子串匹配）
GEO_BLOCK_MARKERS = (
    "location is not supported",
    "location not supported",
    "not supported for the api use",
    "available in your country",
)

_ERROR_BODY_MAX_BYTES = 4096


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


def _read_error_body(exc: HTTPError) -> str:
    """有界读取 HTTP 错误响应体；读取失败返回空串（宁可当作可达，也不误报 geo-block）。"""
    try:
        return exc.read(_ERROR_BODY_MAX_BYTES).decode("utf-8", errors="replace").lower()
    except Exception:
        return ""


def _is_geo_blocked(body: str) -> bool:
    return any(marker in body for marker in GEO_BLOCK_MARKERS)


def _probe_target(opener, url: str, timeout: int) -> tuple[str, str]:
    """探测一个 AI 目标，返回 (判定, 详情)。判定 ∈ {ok, geo_blocked, dead}。

    4xx 不再一律算「可达」：读响应体识别 Gemini 的地区门禁（location not
    supported）；其余 4xx（401 缺 key / 405 等）仍算可达。5xx/超时/连接失败算 dead。
    """
    for attempt in range(DEFAULT_AI_PROBE_RETRIES + 1):
        request = Request(url, headers={"User-Agent": f"cproxy/{__version__}"})
        try:
            with opener.open(request, timeout=timeout) as response:
                status = getattr(response, "status", response.getcode())
            return PROBE_OK, f"HTTP {status}" if status else "成功"
        except HTTPError as exc:
            if 400 <= exc.code < 500:
                if _is_geo_blocked(_read_error_body(exc)):
                    return PROBE_GEO_BLOCKED, "位置不支持（地区门禁）"
                return PROBE_OK, f"HTTP {exc.code}"
            if attempt == DEFAULT_AI_PROBE_RETRIES:
                return PROBE_DEAD, f"HTTP {exc.code}"
            time.sleep(DEFAULT_AI_PROBE_BACKOFFS[attempt])
        except (URLError, OSError) as exc:
            if attempt == DEFAULT_AI_PROBE_RETRIES:
                return PROBE_DEAD, _probe_failure_detail(exc)
            time.sleep(DEFAULT_AI_PROBE_BACKOFFS[attempt])

    return PROBE_DEAD, "连接失败"


def ai_target_verdict(paths: AppPaths, url: str, timeout: int = DEFAULT_AI_PROBE_TIMEOUT) -> str:
    """经本机代理探测某 AI 目标，返回语义判定（ok/geo_blocked/dead）。供看门狗复用。"""
    return _probe_target(_proxy_opener(paths), url, timeout)[0]


class DiagnosticsService:
    def __init__(self, paths: AppPaths):
        self.paths = paths
        self.api = APIBackend(paths)
        self.process = ProcessBackend(paths)

    def test_group(
        self,
        group_name: str,
        *,
        on_progress: Callable[[int, int, str, int | None], None] | None = None,
    ) -> GroupCheckReport:
        """测组内每个节点的延迟。

        `on_progress(done, total, node_name, delay_ms)` 每测完一个节点回调一次；
        `delay_ms` 为 `None` 表示该节点测速失败。回调在调用方线程内同步执行，
        默认 `None` 时行为与输出和历史版本完全一致。
        """
        config = read_config(self.paths)
        groups = self.api.get_groups()
        group = groups.get(group_name)
        if not group:
            from .switch_tree import suggest_groups_from_names

            hint = suggest_groups_from_names(group_name, list(groups.keys()))
            raise SystemExit(f"错误: 未找到代理组或节点: {group_name}\n{hint}")

        url = str(config.get("test-url", DEFAULT_TEST_URL))
        timeout = _config_int(config, "test-timeout", DEFAULT_TEST_TIMEOUT)
        results: list[DelayCheckResult] = []
        members = group.candidates or [group_name]
        total = len(members)
        for index, member in enumerate(members, start=1):
            try:
                payload = self.api.delay_test(str(member), url, timeout)
                delay = payload.get("delay")
                results.append(DelayCheckResult(name=str(member), ok=True, delay=int(delay) if delay is not None else None))
            except Exception:
                results.append(DelayCheckResult(name=str(member), ok=False, delay=None))
            if on_progress is not None:
                on_progress(index, total, results[-1].name, results[-1].delay if results[-1].ok else None)
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

    def run_ai_probe(self, *, on_result: Callable[[AIProbeResult], None] | None = None) -> AIProbeReport:
        """逐个探测 AI 目标。

        `on_result(result)` 每出一个目标结果回调一次（供界面流式上表）；
        回调在调用方线程内同步执行。默认 `None` 时行为与输出和历史版本完全一致。
        """
        config = read_config(self.paths)
        timeout = _config_int(
            config,
            "ai-probe-timeout",
            _config_int(config, "connectivity-timeout", DEFAULT_AI_PROBE_TIMEOUT),
        )
        targets = [
            ("ChatGPT Web", _config_str(config, "ai-chatgpt-url", DEFAULT_AI_PROBE_TARGETS[0][1])),
            ("OpenAI API", _config_str(config, "ai-openai-api-url", DEFAULT_AI_PROBE_TARGETS[1][1])),
            ("Gemini API", _config_str(config, "ai-gemini-api-url", DEFAULT_AI_PROBE_TARGETS[2][1])),
        ]
        opener = _proxy_opener(self.paths)

        results: list[AIProbeResult] = []
        for name, url in targets:
            verdict, detail = _probe_target(opener, url, timeout)
            result = AIProbeResult(name=name, url=url, ok=(verdict == PROBE_OK), detail=detail)
            results.append(result)
            if on_result is not None:
                on_result(result)
        return AIProbeReport(results=results)
