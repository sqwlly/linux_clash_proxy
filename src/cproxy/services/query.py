from __future__ import annotations

from ..backend.api import APIBackend, APIUnavailableError
from ..backend.models import ProxyGroup, QueryContext
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

        self.api.switch_group(group_name, target_name)
        return self.api.get_groups()[group_name]
