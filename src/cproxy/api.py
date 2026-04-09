from __future__ import annotations

import json
from typing import Any
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from .config import AppPaths, read_config


def controller_url(paths: AppPaths) -> str:
    config = read_config(paths)
    addr = config.get("external-controller", "127.0.0.1:9090")
    if str(addr).startswith(("http://", "https://")):
        return str(addr)
    return f"http://{addr}"


def api_secret(paths: AppPaths) -> str:
    config = read_config(paths)
    return str(config.get("secret", "") or "")


def api_request(paths: AppPaths, method: str, path: str, payload: dict | None = None) -> Any:
    url = f"{controller_url(paths)}{path}"
    body = None
    headers: dict[str, str] = {}

    secret = api_secret(paths)
    if secret:
        headers["Authorization"] = f"Bearer {secret}"

    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"

    request = Request(url, data=body, method=method, headers=headers)
    with urlopen(request, timeout=5) as response:
        return json.loads(response.read().decode("utf-8"))


def proxies_data(paths: AppPaths) -> dict[str, Any]:
    return api_request(paths, "GET", "/proxies").get("proxies", {})


def update_proxy(paths: AppPaths, group_name: str, target_name: str) -> None:
    api_request(paths, "PUT", f"/proxies/{quote(group_name, safe='')}", {"name": target_name})


def delay_test(paths: AppPaths, target_name: str, url: str, timeout: int) -> dict[str, Any]:
    query = urlencode({"url": url, "timeout": timeout})
    return api_request(paths, "GET", f"/proxies/{quote(target_name, safe='')}/delay?{query}")
