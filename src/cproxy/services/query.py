from __future__ import annotations

from typing import Any

from ..audit import write_audit_event
from ..backend.api import APIBackend, APIUnavailableError
from ..backend.models import ConnectionEntry, ProviderEntry, ProxyGroup, QueryContext
from ..backend.runtime import RuntimeBackend
from ..config import AppPaths


class QueryService:
    def __init__(self, paths: AppPaths):
        self.paths = paths
        self.api = APIBackend(paths)
        self.runtime = RuntimeBackend(paths)

    def load_context(self, require_api: bool = False) -> QueryContext:
        try:
            groups = self.api.get_groups()
            return QueryContext(groups=groups, api_available=True, runtime_available=False)
        except APIUnavailableError:
            if require_api:
                raise
            groups = self.runtime.get_groups()
            return QueryContext(groups=groups, api_available=False, runtime_available=True)

    def list_groups(self) -> list[ProxyGroup]:
        context = self.load_context(require_api=False)
        return list(context.groups.values())

    def get_group(self, name: str, require_api: bool = False) -> ProxyGroup:
        context = self.load_context(require_api=require_api)
        group = context.groups.get(name)
        if not group:
            raise SystemExit(f"错误: 未找到代理组: {name}")
        return group

    def get_ai_status_groups(self) -> dict[str, ProxyGroup]:
        return self.load_context(require_api=True).groups

    def switch_group(self, group_name: str, target_name: str) -> ProxyGroup:
        groups = self.api.get_groups()
        group = groups.get(group_name)
        if not group:
            raise SystemExit(f"错误: 未找到代理组: {group_name}")

        group_type = str(group.type).lower()
        if group_type not in {"selector", "select"}:
            raise SystemExit(f"错误: 代理组 [{group_name}] 不是可手动切换的 Selector 类型")
        if target_name not in group.candidates:
            raise SystemExit(f"错误: 目标 [{target_name}] 不在代理组 [{group_name}] 的候选列表中")

        try:
            self.api.switch_group(group_name, target_name)
            updated = self.api.get_groups()[group_name]
        except Exception as exc:
            write_audit_event(
                self.paths,
                action="switch_group",
                target=group_name,
                result="error",
                detail={"selected": target_name, "error": str(exc)},
            )
            raise
        write_audit_event(
            self.paths,
            action="switch_group",
            target=group_name,
            result="ok",
            detail={"selected": target_name},
        )
        return updated

    def list_connections(self) -> list[ConnectionEntry]:
        payload = self.api.get_connections()
        connections = payload.get("connections", [])
        if not isinstance(connections, list):
            return []
        return [self._to_connection(item) for item in connections if isinstance(item, dict)]

    def close_connection(self, connection_id: str) -> None:
        try:
            self.api.close_connection(connection_id)
        except Exception as exc:
            write_audit_event(self.paths, action="close_connection", target=connection_id, result="error", detail={"error": str(exc)})
            raise
        write_audit_event(self.paths, action="close_connection", target=connection_id, result="ok")

    def close_all_connections(self) -> None:
        try:
            self.api.close_all_connections()
        except Exception as exc:
            write_audit_event(self.paths, action="close_all_connections", target="all", result="error", detail={"error": str(exc)})
            raise
        write_audit_event(self.paths, action="close_all_connections", target="all", result="ok")

    def list_proxy_providers(self) -> list[ProviderEntry]:
        payload = self.api.get_proxy_providers()
        providers = payload.get("providers", {})
        if not isinstance(providers, dict):
            return []
        entries = []
        for name, item in providers.items():
            if isinstance(item, dict):
                entries.append(self._to_provider(str(name), item))
        return entries

    def update_proxy_provider(self, name: str) -> None:
        try:
            self.api.update_proxy_provider(name)
        except Exception as exc:
            write_audit_event(self.paths, action="update_proxy_provider", target=name, result="error", detail={"error": str(exc)})
            raise
        write_audit_event(self.paths, action="update_proxy_provider", target=name, result="ok")

    def _to_connection(self, item: dict[str, Any]) -> ConnectionEntry:
        metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
        chains = item.get("chains") if isinstance(item.get("chains"), list) else []
        return ConnectionEntry(
            id=str(item.get("id", "")),
            host=str(metadata.get("host") or metadata.get("destinationIP") or metadata.get("remoteDestination") or "-"),
            process=str(metadata.get("process") or metadata.get("processPath") or "-"),
            rule=str(item.get("rule") or item.get("rulePayload") or "-"),
            proxy_chain=[str(part) for part in chains],
            upload=self._to_int(item.get("upload")),
            download=self._to_int(item.get("download")),
        )

    def _to_provider(self, name: str, item: dict[str, Any]) -> ProviderEntry:
        proxies = item.get("proxies")
        return ProviderEntry(
            name=name,
            type=str(item.get("type") or "-"),
            vehicle=str(item.get("vehicleType") or item.get("vehicle") or item.get("path") or item.get("url") or "-"),
            proxy_count=len(proxies) if isinstance(proxies, list) else 0,
            updated_at=str(item.get("updatedAt") or item.get("updated") or item.get("updateAt") or "-"),
        )

    def _to_int(self, value: object) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0
