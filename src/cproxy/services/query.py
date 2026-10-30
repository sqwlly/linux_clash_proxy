from __future__ import annotations

from typing import Any

from ..audit import write_audit_event
from ..backend.api import APIBackend, APIUnavailableError
from ..backend.models import ConnectionEntry, ProviderEntry, ProxyGroup, QueryContext
from ..backend.runtime import (
    AI_AUTO_GROUP,
    AI_GEMINI_GROUP,
    AI_MANUAL_GROUP,
    AI_PROCESS_NAMES,
    AI_REGION_JP,
    AI_REGION_SG,
    AI_REGION_US,
    AI_SG_GROUP,
    AI_US_GROUP,
    RuntimeBackend,
)
from ..config import AppPaths, load_yaml_file, runtime_file
from ..names import match_candidates_fuzzy, normalize_name, resolve_candidate, resolve_group_alias

AI_SWITCH_DROP_GROUPS = frozenset(
    {
        AI_MANUAL_GROUP,
        AI_AUTO_GROUP,
        AI_US_GROUP,
        AI_SG_GROUP,
        AI_GEMINI_GROUP,
        AI_REGION_JP,
        AI_REGION_US,
        AI_REGION_SG,
    }
)


class QueryService:
    def __init__(self, paths: AppPaths):
        self.paths = paths
        self.api = APIBackend(paths)
        self.runtime = RuntimeBackend(paths)
        # 最近一次 /proxies 快照：同一次 `cproxy switch` 里复用，避免选组后再打两遍
        self.groups_snapshot: dict[str, ProxyGroup] = {}
        self.last_switch_from: str | None = None

    def load_context(self, require_api: bool = False, *, request_timeout: float | None = None) -> QueryContext:
        try:
            groups = self.api.get_groups(request_timeout=request_timeout)
            self.groups_snapshot = groups
            return QueryContext(groups=groups, api_available=True, runtime_available=False)
        except APIUnavailableError:
            if require_api:
                raise
            groups = self.runtime.get_groups()
            self.groups_snapshot = groups
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
            data = load_yaml_file(runtime_file(self.paths))
        except Exception:
            return None
        if not isinstance(data, dict):
            return None
        rules = data.get("rules")
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
        所以这里吞掉异常而非上抛。同一次会话里若已有 /proxies 快照，直接从
        里面抽 history，不再打第二遍 API。
        """
        if self.groups_snapshot:
            return {name: group.delay for name, group in self.groups_snapshot.items() if group.delay is not None}
        try:
            return self.api.get_delays()
        except Exception:
            return {}

    def get_group(self, name: str, require_api: bool = False) -> ProxyGroup:
        if not require_api and name in self.groups_snapshot:
            return self.groups_snapshot[name]
        context = self.load_context(require_api=require_api)
        group = context.groups.get(name)
        if not group:
            raise SystemExit(f"错误: 未找到代理组: {name}")
        return group

    def get_ai_status_groups(self) -> dict[str, ProxyGroup]:
        return self.load_context(require_api=True).groups

    def switch_group(self, group_name: str, target_name: str, *, drop_connections: bool = True) -> ProxyGroup:
        groups = self.api.get_groups()
        self.groups_snapshot = groups
        group = groups.get(group_name)
        if not group:
            aliased = resolve_group_alias(group_name, groups.keys(), match_group=self.match_rule_group())
            if aliased:
                group_name = aliased
                group = groups.get(group_name)
        if not group:
            raise SystemExit(f"错误: 未找到代理组: {group_name}")

        group_type = str(group.type).lower()
        if group_type not in {"selector", "select", "fallback"}:
            raise SystemExit(f"错误: 代理组 [{group_name}] 不是可手动切换的 Selector 类型")
        # 面板展示会经 normalize_name 剥掉 emoji 与 `丨`（`🇯🇵 Japan` → `Japan`），
        # 用户照屏幕上的名字或模糊简写（如 kr01）输入是自然行为——按规范化与模糊规则解析候选
        resolved = resolve_candidate(target_name, group.candidates)
        if resolved is None:
            fuzzy_hits = match_candidates_fuzzy(target_name, group.candidates)
            ambiguous_note = ""
            if len(fuzzy_hits) > 1:
                preview = "、".join(normalize_name(item) for item in fuzzy_hits[:6])
                ambiguous_note = f"\n匹配到多个候选: {preview}"
            raise SystemExit(
                f"错误: 目标 [{target_name}] 不在代理组 [{group_name}] 的候选列表中{ambiguous_note}\n"
                f"提示: cproxy list-nodes {group_name} 查看候选"
            )
        target_name = resolved
        self.last_switch_from = group.current

        try:
            if group_type == "fallback" and target_name != AI_AUTO_GROUP:
                target_entry = groups.get(target_name)
                delay_fn = getattr(self.api, "delay_test", None)
                if target_entry is not None and target_entry.alive is False and callable(delay_fn):
                    try:
                        from ..backend.runtime import TEST_URL

                        delay_fn(target_name, TEST_URL, 5000, request_timeout=6)
                    except Exception:
                        pass
            self.api.switch_group(group_name, target_name)
            updated_groups = self.api.get_groups()
            self.groups_snapshot = updated_groups
            updated = updated_groups[group_name]
            keep = target_name if target_name != AI_AUTO_GROUP else None
            dropped = self._drop_stale_ai_connections(group_name, keep_leaf=keep) if drop_connections else 0
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
            detail={"selected": target_name, "dropped": dropped},
        )
        return updated

    def switch_path(self, path: list[tuple[str, str]]) -> ProxyGroup:
        """按序切换多级 selector 路径（内层组到外层组），返回最后一步更新后的组。"""
        if not path:
            raise SystemExit("错误: 切换路径为空")
        group: ProxyGroup | None = None
        for group_name, target_name in path:
            group = self.switch_group(group_name, target_name)
        assert group is not None
        return group

    def _drop_stale_ai_connections(self, group_name: str, *, keep_leaf: str | None = None) -> int:
        """切换 AI 出口后立刻掐掉旧 TCP，避免同一会话混用新旧出口 IP。

        Google Cloud Code / Gemini 会按请求 IP 做地区校验；mihomo 换组不会
        自动拆掉已建立的长连接，必须显式关闭。若指定 `keep_leaf`（被动 fallback
        巡检场景），则保留已落在新叶子（`chains[0] == keep_leaf`）上的新连接，
        只掐掉滞留在旧叶子上的陈旧连接。
        对于 `AI-GEMINI` 与 `AI-MANUAL`，若旧叶子为非静态 IEPL 的安全节点（如 1X WebSocket），
        仅关闭闲置连接与后台轮询，绝不强杀正在上传大包上下文、等待模型思考或正在流式接收响应的活跃长连接。
        """
        if group_name not in AI_SWITCH_DROP_GROUPS:
            return 0
        try:
            payload = self.api.get_connections()
        except Exception:
            return 0
        connections = payload.get("connections") if isinstance(payload, dict) else []
        if not isinstance(connections, list):
            return 0
        socket_idle_map: dict[str, tuple[int, int]] | None = None
        dropped = 0
        for item in connections:
            if not isinstance(item, dict) or not self._connection_follows_ai_switch(item, group_name):
                continue
            chains = item.get("chains") or []
            leaf = str(chains[0]) if isinstance(chains, list) and chains else ""
            if keep_leaf and leaf == keep_leaf:
                continue
            if group_name in (AI_GEMINI_GROUP, AI_MANUAL_GROUP) and leaf:
                upload = self._to_int(item.get("upload"))
                download = self._to_int(item.get("download"))
                raw_meta = item.get("metadata")
                meta: dict[str, Any] = raw_meta if isinstance(raw_meta, dict) else {}
                host = str(meta.get("host") or "").lower()
                src_port = str(meta.get("sourcePort") or "")
                from .ai_watch import (
                    AI_STREAM_HOSTS,
                    _unpack_socket_timings,
                    get_local_proxy_socket_idle_ms,
                    is_inflight_ai_connection,
                    track_connections_idle_ms,
                )
                from .probe import is_static_iepl_node

                if (
                    (upload >= 18_000 or (0 < upload < 18_000 and download < 4_000))
                    and host in AI_STREAM_HOSTS
                    and src_port.isdigit()
                ):
                    if socket_idle_map is None:
                        socket_idle_map = get_local_proxy_socket_idle_ms(self.runtime.mixed_port())
                        if not socket_idle_map:
                            socket_idle_map = track_connections_idle_ms(self.paths, connections)
                    unpacked = _unpack_socket_timings(socket_idle_map.get(src_port), src_port)
                    if unpacked is not None:
                        if (
                            upload < 18_000
                            and not is_static_iepl_node(leaf, self.paths)
                            and (unpacked[2] > 0 or min(unpacked[0], unpacked[1]) <= 15_000)
                        ):
                            continue
                        if is_inflight_ai_connection(
                            upload,
                            download,
                            unpacked[0],
                            unpacked[1],
                            on_stale_leaf=True,
                            recv_q=unpacked[2],
                            send_q=unpacked[3],
                        ):
                            continue
            conn_id = str(item.get("id") or "")
            if not conn_id:
                continue
            try:
                self.api.close_connection(conn_id)
                dropped += 1
            except Exception:
                continue
        return dropped

    @staticmethod
    def _connection_follows_ai_switch(item: dict[str, Any], group_name: str) -> bool:
        chains = item.get("chains") or []
        chain_names = {str(part) for part in chains} if isinstance(chains, list) else set()
        if "H2-KEEPALIVE" in chain_names:
            return False
        if group_name == AI_GEMINI_GROUP:
            return AI_GEMINI_GROUP in chain_names
        if AI_GEMINI_GROUP in chain_names:
            return False
        if group_name in chain_names or AI_MANUAL_GROUP in chain_names:
            return True
        raw_metadata = item.get("metadata")
        metadata: dict[str, Any] = raw_metadata if isinstance(raw_metadata, dict) else {}
        process = str(metadata.get("process") or metadata.get("processPath") or "").lower()
        base = process.replace("\\", "/").rsplit("/", 1)[-1].split()[0] if process else ""
        if base.startswith("agy.") and base.endswith(".old"):
            base = "agy"
        return base in AI_PROCESS_NAMES

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
