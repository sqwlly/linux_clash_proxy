from __future__ import annotations

from typing import Any

import yaml

from ..audit import write_audit_event
from ..backend.api import APIBackend, APIUnavailableError
from ..backend.models import ConnectionEntry, ProviderEntry, ProxyGroup, QueryContext
from ..backend.runtime import RuntimeBackend
from ..config import AppPaths, runtime_file
from ..names import resolve_candidate


class QueryService:
    def __init__(self, paths: AppPaths):
        self.paths = paths
        self.api = APIBackend(paths)
        self.runtime = RuntimeBackend(paths)

    def load_context(self, require_api: bool = False, *, request_timeout: float | None = None) -> QueryContext:
        try:
            groups = self.api.get_groups(request_timeout=request_timeout)
            return QueryContext(groups=groups, api_available=True, runtime_available=False)
        except APIUnavailableError:
            if require_api:
                raise
            groups = self.runtime.get_groups()
            return QueryContext(groups=groups, api_available=False, runtime_available=True)

    def list_groups(self, *, request_timeout: float | None = None) -> list[ProxyGroup]:
        context = self.load_context(require_api=False, request_timeout=request_timeout)
        return list(context.groups.values())

    def match_rule_group(self) -> str | None:
        """`MATCH` 规则指向的组——它承载所有未命中前面规则的流量。

        选择器用它标注「默认路由」，好让用户分清该改哪个组才影响实际出口。
        读的是 runtime.yaml（规则就在那里），失败返回 None：标注只是辅助信息。
        """
        try:
            data = yaml.safe_load(runtime_file(self.paths).read_text(encoding="utf-8"))
        except Exception:
            return None
        rules = data.get("rules") if isinstance(data, dict) else None
        if not isinstance(rules, list):
            return None
        # MATCH 是最后一条，倒着找最快
        for rule in reversed(rules):
            text = str(rule)
            if text.startswith("MATCH,"):
                return text.split(",", 1)[1].strip() or None
        return None

    def node_delays(self) -> dict[str, int]:
        """各节点最近一次延迟（毫秒）。取不到就返回空。

        延迟只是选择器上的锦上添花，不该因为它查询失败就阻塞选择流程，
        所以这里吞掉异常而非上抛。
        """
        try:
            return self.api.get_delays()
        except Exception:
            return {}

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
        # 面板展示会经 normalize_name 剥掉 emoji 与 `丨`（`🇯🇵 Japan` → `Japan`），
        # 用户照屏幕上的名字输入是自然行为——按规范化名再解析一次候选，
        # 避免出现「照着显示敲却切不动」
        resolved = resolve_candidate(target_name, group.candidates)
        if resolved is None:
            raise SystemExit(
                f"错误: 目标 [{target_name}] 不在代理组 [{group_name}] 的候选列表中\n"
                f"提示: cproxy list-nodes {group_name} 查看候选"
            )
        target_name = resolved

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
            write_audit_event(
                self.paths, action="close_connection", target=connection_id,
                result="error", detail={"error": str(exc)},
            )
            raise
        write_audit_event(self.paths, action="close_connection", target=connection_id, result="ok")

    def close_all_connections(self) -> None:
        try:
            self.api.close_all_connections()
        except Exception as exc:
            write_audit_event(
                self.paths, action="close_all_connections", target="all",
                result="error", detail={"error": str(exc)},
            )
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
        raw_metadata = item.get("metadata")
        metadata = raw_metadata if isinstance(raw_metadata, dict) else {}
        raw_chains = item.get("chains")
        chains = raw_chains if isinstance(raw_chains, list) else []
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

    def _to_int(self, value: Any) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0
