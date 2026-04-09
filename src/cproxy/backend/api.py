from __future__ import annotations

import json
from typing import Any
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from ..config import AppPaths, read_config
from .models import ProxyGroup


class APIUnavailableError(RuntimeError):
    pass


class APIBackend:
    DEFAULT_TIMEOUT = 2

    def __init__(self, paths: AppPaths):
        self.paths = paths

    def controller_url(self) -> str:
        config = read_config(self.paths)
        addr = config.get("external-controller", "127.0.0.1:9090")
        if str(addr).startswith(("http://", "https://")):
            return str(addr)
        return f"http://{addr}"

    def api_secret(self) -> str:
        config = read_config(self.paths)
        return str(config.get("secret", "") or "")

    def request_timeout(self) -> int:
        config = read_config(self.paths)
        value = config.get("api-timeout", self.DEFAULT_TIMEOUT)
        try:
            return int(value)
        except (TypeError, ValueError):
            return self.DEFAULT_TIMEOUT

    def request(self, method: str, path: str, payload: dict | None = None) -> Any:
        url = f"{self.controller_url()}{path}"
        body = None
        headers: dict[str, str] = {}

        secret = self.api_secret()
        if secret:
            headers["Authorization"] = f"Bearer {secret}"

        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"

        request = Request(url, data=body, method=method, headers=headers)
        try:
            with urlopen(request, timeout=self.request_timeout()) as response:
                return json.loads(response.read().decode("utf-8"))
        except Exception as exc:
            raise APIUnavailableError("错误: Mihomo API 不可访问，请检查 external-controller、secret 或服务状态") from exc

    def _to_proxy_group(self, name: str, payload: dict[str, Any]) -> ProxyGroup:
        history = payload.get("history") or []
        delay = history[-1].get("delay") if history else None
        return ProxyGroup(
            name=name,
            type=str(payload.get("type", "")),
            current=str(payload.get("now", "-")),
            candidates=[str(item) for item in payload.get("all", [])],
            alive=payload.get("alive"),
            delay=int(delay) if delay not in (None, "-") else None,
            source="api",
        )

    def get_groups(self) -> dict[str, ProxyGroup]:
        payload = self.request("GET", "/proxies").get("proxies", {})
        return {
            str(name): self._to_proxy_group(str(name), group)
            for name, group in payload.items()
            if isinstance(group, dict)
        }

    def switch_group(self, group_name: str, target_name: str) -> None:
        self.request("PUT", f"/proxies/{quote(group_name, safe='')}", {"name": target_name})

    def delay_test(self, target_name: str, url: str, timeout: int) -> dict[str, Any]:
        query = urlencode({"url": url, "timeout": timeout})
        return self.request("GET", f"/proxies/{quote(target_name, safe='')}/delay?{query}")
