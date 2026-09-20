from __future__ import annotations

import json
import os
import ssl
from importlib import import_module
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode, urlparse
from urllib.request import BaseHandler, HTTPSHandler, OpenerDirector, ProxyHandler, Request, build_opener

from ..config import AppPaths, read_config
from .models import ProxyGroup


class APIUnavailableError(RuntimeError):
    pass


class APIBackend:
    DEFAULT_TIMEOUT = 2

    def __init__(self, paths: AppPaths):
        self.paths = paths
        self._opener: OpenerDirector | None = None

    def controller_url(self) -> str:
        return self._controller_url_from_config(read_config(self.paths))

    def _controller_url_from_config(self, config: dict[str, Any]) -> str:
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
        return self._secret_from_config(read_config(self.paths))

    def _secret_from_config(self, config: dict[str, Any]) -> str:
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
        return self._timeout_from_config(read_config(self.paths))

    def _timeout_from_config(self, config: dict[str, Any]) -> int:
        value = config.get("api-timeout", self.DEFAULT_TIMEOUT)
        try:
            return int(value)
        except (TypeError, ValueError):
            return self.DEFAULT_TIMEOUT

    def request(self, method: str, path: str, payload: dict | None = None, *, request_timeout: float | None = None) -> Any:
        # 一次 request 里 controller / secret / timeout 以前各自 read_config，
        # 大 config.yaml 会把 localhost 的 5ms 调用拖成 300ms+。
        config = read_config(self.paths)
        url = f"{self._controller_url_from_config(config)}{path}"
        body = None
        headers: dict[str, str] = {}

        secret = self._secret_from_config(config)
        if secret:
            headers["Authorization"] = f"Bearer {secret}"

        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"

        request = Request(url, data=body, method=method, headers=headers)
        try:
            effective_timeout = request_timeout if request_timeout is not None else self._timeout_from_config(config)
            handle = self._http_opener(url).open(request, timeout=effective_timeout)
            with handle as response:
                response_body = response.read().decode("utf-8")
                if not response_body.strip():
                    return {}
                return json.loads(response_body)
        except Exception as exc:
            raise APIUnavailableError("错误: Mihomo API 不可访问，请检查 external-controller、secret 或服务状态") from exc

    def _http_opener(self, url: str) -> OpenerDirector:
        """同一进程内复用 opener，避免每发一个 DELETE 就新建 TCP。

        Controller 流量不能再套一层正在管理的代理，所以固定 `ProxyHandler({})`。
        """
        if self._opener is None:
            handlers: list[BaseHandler] = [ProxyHandler({})]
            context = self._tls_context(url)
            if context is not None:
                handlers.append(HTTPSHandler(context=context))
            self._opener = build_opener(*handlers)
        return self._opener

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

    def get_delays(self, *, request_timeout: float | None = None) -> dict[str, int]:
        """各代理项的最近一次延迟（毫秒），供选择器/列表在名字旁标注。

        只有被 url-test / fallback 组测过速的项才有 `history`；selector 组自身
        没有，所以这里取到的基本都是叶子节点。没数据的项直接不出现在结果里，
        调用方按「无延迟」处理即可。
        """
        payload = self.request("GET", "/proxies", request_timeout=request_timeout).get("proxies", {})
        delays: dict[str, int] = {}
        for name, item in payload.items():
            if not isinstance(item, dict):
                continue
            history = item.get("history") or []
            if not history:
                continue
            delay = history[-1].get("delay")
            if delay in (None, "-"):
                continue
            try:
                delays[str(name)] = int(delay)
            except (TypeError, ValueError):
                continue
        return delays

    def get_groups(self, *, request_timeout: float | None = None) -> dict[str, ProxyGroup]:
        payload = self.request("GET", "/proxies", request_timeout=request_timeout).get("proxies", {})
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

    def selector_now(self) -> dict[str, str]:
        """当前 Selector 组的选中项，供热重载后原样写回。"""
        snapshot: dict[str, str] = {}
        for name, group in self.get_groups().items():
            if str(group.type).lower() not in {"select", "selector"}:
                continue
            current = str(group.current or "").strip()
            if current and current != "-":
                snapshot[name] = current
        return snapshot

    def restore_selectors(self, snapshot: dict[str, str]) -> dict[str, str]:
        """把仍存在于候选列表里的选中项写回。节点已从订阅消失的项跳过。"""
        restored: dict[str, str] = {}
        if not snapshot:
            return restored
        groups = self.get_groups()
        for name, wanted in snapshot.items():
            group = groups.get(name)
            if group is None or str(group.type).lower() not in {"select", "selector"}:
                continue
            if wanted not in group.candidates:
                continue
            if group.current == wanted:
                continue
            self.switch_group(name, wanted)
            restored[name] = wanted
        if restored:
            self._drop_connections_using_groups(set(restored))
        return restored

    def _drop_connections_using_groups(self, group_names: set[str]) -> int:
        if not group_names:
            return 0
        try:
            payload = self.get_connections()
        except APIUnavailableError:
            return 0
        connections = payload.get("connections") if isinstance(payload, dict) else []
        if not isinstance(connections, list):
            return 0
        dropped = 0
        for item in connections:
            if not isinstance(item, dict):
                continue
            chains = item.get("chains") or []
            if not any(str(part) in group_names for part in chains):
                continue
            conn_id = str(item.get("id") or "")
            if not conn_id:
                continue
            try:
                self.close_connection(conn_id)
            except APIUnavailableError:
                continue
            dropped += 1
        return dropped

    def reload_config(self, path: str) -> dict[str, Any]:
        # PUT /configs?force=true 会把所有 selector 重置成 YAML 第一项。
        # Gemini/Antigravity 对出口 IP 粘滞，重载后若从美国跳回日本会立刻 400。
        snapshot: dict[str, str] = {}
        try:
            snapshot = self.selector_now()
        except APIUnavailableError:
            snapshot = {}
        result = self.request("PUT", "/configs?force=true", {"path": path})
        if snapshot:
            try:
                self.restore_selectors(snapshot)
            except APIUnavailableError:
                pass
        return result

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
