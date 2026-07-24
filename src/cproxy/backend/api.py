from __future__ import annotations

import json
import os
import ssl
from importlib import import_module
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode, urlparse
from urllib.request import BaseHandler, HTTPSHandler, ProxyHandler, Request, build_opener

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
        if config.get("external-controller-unix"):
            raise APIUnavailableError(
                "错误: 当前不支持 external-controller-unix；"
                "Unix socket 控制面不会校验 secret，请改用 loopback HTTP/TLS controller"
            )

        tls_addr = config.get("external-controller-tls")
        if tls_addr:
            return self._controller_url(str(tls_addr), default_scheme="https")

        addr = config.get("external-controller", "127.0.0.1:9090")
        return self._controller_url(str(addr), default_scheme="http")

    def _controller_url(self, addr: str, default_scheme: str) -> str:
        if str(addr).startswith(("http://", "https://")):
            return str(addr)
        return f"{default_scheme}://{addr}"

    def api_secret(self) -> str:
        config = read_config(self.paths)
        credential_name = str(config.get("secret-systemd-credential", "") or "").strip()
        if credential_name:
            credentials_dir = os.environ.get("CREDENTIALS_DIRECTORY")
            if credentials_dir:
                secret = self._read_secret_file(Path(credentials_dir) / credential_name)
                if secret:
                    return secret

        secret_path = str(config.get("secret-file", "") or "").strip()
        if secret_path:
            secret = self._read_secret_file(Path(secret_path).expanduser())
            if secret:
                return secret

        keyring_service = str(config.get("secret-keyring-service", "") or "").strip()
        if keyring_service:
            keyring_user = str(config.get("secret-keyring-username", "controller") or "controller").strip()
            return self._read_keyring_secret(keyring_service, keyring_user)

        return str(config.get("secret", "") or "")

    def _read_secret_file(self, path: Path) -> str:
        try:
            return path.read_text(encoding="utf-8").strip()
        except OSError:
            return ""

    def _read_keyring_secret(self, service: str, username: str) -> str:
        try:
            keyring = import_module("keyring")
        except ImportError as exc:
            raise APIUnavailableError("错误: 已配置 secret-keyring-service，但当前 Python 环境未安装 keyring") from exc

        secret = keyring.get_password(service, username)
        if not secret:
            raise APIUnavailableError("错误: keyring 中未找到 controller secret")
        return str(secret)

    def request_timeout(self) -> int:
        config = read_config(self.paths)
        value = config.get("api-timeout", self.DEFAULT_TIMEOUT)
        try:
            return int(value)
        except (TypeError, ValueError):
            return self.DEFAULT_TIMEOUT

    def request(self, method: str, path: str, payload: dict | None = None, *, request_timeout: int | None = None) -> Any:
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
            context = self._tls_context(url)
            # Controller traffic must not recurse through the proxy being managed.
            handlers: list[BaseHandler] = [ProxyHandler({})]
            if context is not None:
                handlers.append(HTTPSHandler(context=context))
            effective_timeout = request_timeout if request_timeout is not None else self.request_timeout()
            handle = build_opener(*handlers).open(request, timeout=effective_timeout)
            with handle as response:
                response_body = response.read().decode("utf-8")
                if not response_body.strip():
                    return {}
                return json.loads(response_body)
        except Exception as exc:
            raise APIUnavailableError("错误: Mihomo API 不可访问，请检查 external-controller、secret 或服务状态") from exc

    def _tls_context(self, url: str) -> ssl.SSLContext | None:
        parsed = urlparse(url)
        if parsed.scheme != "https" or parsed.hostname not in {"127.0.0.1", "::1", "localhost"}:
            return None
        # Mihomo's loopback TLS controller uses a self-signed certificate by default.
        return ssl._create_unverified_context()

    def _to_proxy_group(self, name: str, payload: dict[str, Any]) -> ProxyGroup:
        history = payload.get("history") or []
        delay = history[-1].get("delay") if history else None
        return ProxyGroup(
            name=name,
            type=str(payload.get("type", "")),
            current=str(payload.get("now", "-")),
            candidates=[str(item) for item in payload.get("all", [])],
            alive=payload.get("alive"),
            delay=int(delay) if delay is not None and delay != "-" else None,
            source="api",
        )

    def get_groups(self) -> dict[str, ProxyGroup]:
        payload = self.request("GET", "/proxies").get("proxies", {})
        return {
            str(name): self._to_proxy_group(str(name), group)
            for name, group in payload.items()
            if isinstance(group, dict)
        }

    def version(self) -> dict[str, Any]:
        return self.request("GET", "/version")

    def get_config(self) -> dict[str, Any]:
        return self.request("GET", "/configs")

    def patch_config(self, patch: dict[str, Any]) -> dict[str, Any]:
        return self.request("PATCH", "/configs", patch)

    def reload_config(self, path: str) -> dict[str, Any]:
        return self.request("PUT", "/configs", {"path": path})

    def switch_group(self, group_name: str, target_name: str) -> None:
        self.request("PUT", f"/proxies/{quote(group_name, safe='')}", {"name": target_name})

    def delay_test(self, target_name: str, url: str, timeout: int, *, request_timeout: int | None = None) -> dict[str, Any]:
        query = urlencode({"url": url, "timeout": timeout})
        return self.request("GET", f"/proxies/{quote(target_name, safe='')}/delay?{query}", request_timeout=request_timeout)

    def get_connections(self) -> dict[str, Any]:
        return self.request("GET", "/connections")

    def close_connection(self, connection_id: str) -> None:
        self.request("DELETE", f"/connections/{quote(connection_id, safe='')}")

    def close_all_connections(self) -> None:
        self.request("DELETE", "/connections")

    def get_proxy_providers(self) -> dict[str, Any]:
        return self.request("GET", "/providers/proxies")

    def update_proxy_provider(self, name: str) -> None:
        self.request("PUT", f"/providers/proxies/{quote(name, safe='')}")

    def get_rule_providers(self) -> dict[str, Any]:
        return self.request("GET", "/providers/rules")

    def update_rule_provider(self, name: str) -> None:
        self.request("PUT", f"/providers/rules/{quote(name, safe='')}")
