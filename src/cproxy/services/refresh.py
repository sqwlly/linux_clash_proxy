from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlparse
from urllib.request import ProxyHandler, Request, build_opener, urlopen

import yaml

from .. import __version__
from ..backend.api import APIBackend, APIUnavailableError
from ..backend.models import GroupCheckReport
from ..backend.process import ProcessBackend
from ..backend.runtime import TEST_URL, RuntimeBackend
from ..config import AppPaths, config_file, read_config, runtime_file
from ..redaction import redact_text
from ..snapshots import snapshot_file
from .diagnostics import DiagnosticsService
from .nodelist import (
    REGION_LABELS,
    SUBSCRIPTION_REGION_ORDER,
    match_region,
    parse_subscription_payload,
)
from .query import QueryService

SUBSCRIPTION_MAX_BYTES = 4 * 1024 * 1024
SUBSCRIPTION_TIMEOUT = 20
API_READY_TIMEOUT = 5.0
API_READY_INTERVAL = 0.2

# 安全相关键：订阅内容一律剔除，防止恶意/被劫持订阅注入
# （例如 program-path 会让下次 cproxy start 执行攻击者指定的二进制）
SUBSCRIPTION_STRIP_KEYS = {
    "program-path",
    "api-timeout",
    "external-controller",
    "external-controller-tls",
    "external-controller-unix",
    "secret",
    "secret-file",
    "secret-systemd-credential",
    "secret-keyring-service",
    "secret-keyring-username",
    "audit-journald",
    "allow-lan",
    "bind-address",
}

# 本地优先键：本地配置已存在时保留本地值；本地缺失时接受订阅提供的值
LOCAL_PREFERRED_KEYS = {
    "mixed-port",
    "port",
    "mode",
    "log-level",
    "output-color",
    "output-icons",
    "test-url",
    "test-timeout",
    "connectivity-timeout",
    "connectivity-test-urls",
    "ip-check-urls",
    "ai-chatgpt-url",
    "ai-openai-api-url",
    "refresh-groups",
    "subscriptions",
    "profile",
}


@dataclass
class GroupSwitchResult:
    group: str
    current: str | None
    action: str
    target: str | None = None
    detail: str = ""


@dataclass
class RefreshReport:
    subscription: str
    subscription_detail: str = ""
    extra_subscriptions: list[ExtraSubscriptionResult] = field(default_factory=list)
    runtime_path: Path | None = None
    was_running: bool = False
    restarted: bool = False
    hot_reloaded: bool = False
    groups: list[GroupSwitchResult] = field(default_factory=list)


def _rebuild_groups_with_new_nodes(local_groups: list, old_node_names: list[str], new_node_names: list[str]) -> list:
    """nodelist 型订阅只提供 proxies：沿用本地分组结构，把组内旧节点成员
    整体替换为新节点名序列（在首个旧节点出现的位置展开），组引用与
    DIRECT/REJECT 等内置策略成员原位保留。组内没有旧节点时保持原样。"""
    old_set = set(old_node_names)
    rebuilt: list = []
    for group in local_groups:
        if not isinstance(group, dict) or not isinstance(group.get("proxies"), list):
            rebuilt.append(group)
            continue
        members: list = []
        inserted = False
        for member in group["proxies"]:
            if str(member) in old_set:
                if not inserted:
                    members.extend(new_node_names)
                    inserted = True
                continue
            members.append(member)
        rebuilt.append({**group, "proxies": members})
    return rebuilt


PANEL_INFO_NODE_MARKERS: tuple[str, ...] = (
    "剩余流量",
    "套餐到期",
    "过期时间",
    "到期时间",
    "流量重置",
    "有效期",
    "官网",
    "expire",
    "traffic",
)


def _is_panel_info_node(name: str) -> bool:
    """识别机场面板注入的信息节点（剩余流量/套餐到期等）。

    这类节点名携带动态数值（如“剩余流量：160.9 GB”），每次订阅都会改名，
    不能按“本地有而订阅没有”识别为用户自建节点，否则旧名字会以附加节点
    形式无限累积成死节点。"""
    lowered = name.lower()
    return any(marker in lowered for marker in PANEL_INFO_NODE_MARKERS)


def _preserve_local_extra_proxies(merged: dict, existing: dict) -> None:
    """完整型订阅覆盖 proxies/proxy-groups 时，保留本地手工添加的附加内容：
    - 附加节点：追加进 merged["proxies"]，并在同名组中按本地原有位置插回，
      防止每日订阅更新冲掉自建节点或改变其在 fallback 组中的优先级。
    - 附加分组：本地存在而订阅未提供的分组（自建分组、多订阅机场分组）
      整体追加到组列表末尾，其名称同样按本地位置插回同名组，保证主订阅
      组里挂的附加机场入口（如 "Mitce"）不随订阅更新丢失。
    面板信息节点（名字随流量/到期日变化）不视为自建节点，任其随订阅更替。"""
    local_proxies = [proxy for proxy in existing.get("proxies") or [] if isinstance(proxy, dict) and proxy.get("name")]
    merged_names = {
        str(proxy.get("name")) for proxy in merged.get("proxies") or [] if isinstance(proxy, dict) and proxy.get("name")
    }
    extra_names: set[str] = set()
    extra_proxies: list = []
    for proxy in local_proxies:
        name = str(proxy["name"])
        if name not in merged_names and not _is_panel_info_node(name):
            extra_proxies.append(proxy)
            extra_names.add(name)

    merged_group_names = {
        str(group.get("name"))
        for group in merged.get("proxy-groups") or []
        if isinstance(group, dict) and isinstance(group.get("name"), str)
    }
    extra_group_names: set[str] = set()
    extra_groups: list = []
    for group in existing.get("proxy-groups") or []:
        if not isinstance(group, dict) or not isinstance(group.get("name"), str):
            continue
        if group["name"] not in merged_group_names:
            extra_groups.append(group)
            extra_group_names.add(group["name"])

    if not extra_proxies and not extra_groups:
        return

    if extra_proxies:
        proxies: list = merged.get("proxies") or []
        proxies.extend(extra_proxies)
        merged["proxies"] = proxies
    if extra_groups:
        groups: list = merged.get("proxy-groups") or []
        groups.extend(extra_groups)
        merged["proxy-groups"] = groups

    member_extras = extra_names | extra_group_names
    local_group_members: dict[str, list] = {}
    for group in existing.get("proxy-groups") or []:
        if isinstance(group, dict) and isinstance(group.get("name"), str):
            local_group_members[group["name"]] = group.get("proxies") or []
    for group in merged.get("proxy-groups") or []:
        if not isinstance(group, dict) or not isinstance(group.get("name"), str):
            continue
        members = local_group_members.get(group["name"])
        if not isinstance(members, list):
            continue
        group_members: list = group.get("proxies") or []
        for index, member in enumerate(members):
            name = str(member)
            if name in member_extras and name not in group_members:
                group_members.insert(min(index, len(group_members)), name)
        group["proxies"] = group_members


def _download_subscription(paths: AppPaths, url: str, timeout: int) -> bytes:
    """下载订阅内容：优先显式走本机代理（订阅域名常被墙且 timer 环境无代理
    环境变量），代理不可达时回退直连（适用于未被墙的订阅）。
    代理已通但订阅站返回 HTTP 错误（4xx/5xx）时直接抛出，不做直连重试。"""
    request = Request(url, headers={"User-Agent": f"cproxy/{__version__}"})
    host = (urlparse(url).hostname or "").strip("[]").lower()
    loopback = host in ("127.0.0.1", "localhost", "::1")
    try:
        port = int((read_config(paths).get("mixed-port") or 7890))
    except Exception:
        port = 7890
    proxy_url = f"http://127.0.0.1:{port}"
    try:
        if loopback:
            # 本机地址（本地测试/镜像）无需经代理，直接请求
            with urlopen(request, timeout=timeout) as response:
                return response.read(SUBSCRIPTION_MAX_BYTES + 1)
        opener = build_opener(ProxyHandler({"http": proxy_url, "https": proxy_url}))
        with opener.open(request, timeout=timeout) as response:
            return response.read(SUBSCRIPTION_MAX_BYTES + 1)
    except HTTPError:
        raise
    except OSError:
        # 本机代理未运行/不可达：回退默认行为（按环境变量或直连）
        with urlopen(request, timeout=timeout) as response:
            return response.read(SUBSCRIPTION_MAX_BYTES + 1)


def update_source_from_subscription(paths: AppPaths, url: str, timeout: int = SUBSCRIPTION_TIMEOUT) -> Path:
    """下载订阅并合并进原始配置；订阅内容覆盖节点/规则，本地环境键保留。"""
    raw = _download_subscription(paths, url, timeout)
    if len(raw) > SUBSCRIPTION_MAX_BYTES:
        raise RuntimeError("错误: 订阅内容超过大小限制")

    data = yaml.safe_load(raw.decode("utf-8"))
    if not isinstance(data, dict) or not (data.get("proxies") or data.get("proxy-groups")):
        hint = ""
        if "flag=meta" not in url:
            hint = "；如果订阅提供商支持 Clash 格式，尝试在 URL 末尾追加 &flag=meta"
        if isinstance(data, dict) and not data.get("proxies"):
            raise RuntimeError(f"错误: 订阅返回了有效 YAML 但 proxies 为空{hint}")
        raise RuntimeError(f"错误: 订阅内容不是有效的 Clash/Mihomo 配置{hint}")

    path = config_file(paths)
    existing = read_config(paths)
    if isinstance(data.get("proxy-groups"), list):
        # 完整配置订阅：订阅数据先剔除安全相关键；本地已存在的值（含安全键与
        # 本地优先键）一律保留。区别仅在于本地缺失时：安全键保持缺失，本地优先键接受订阅值。
        merged = {key: value for key, value in data.items() if key not in SUBSCRIPTION_STRIP_KEYS}
        for key in SUBSCRIPTION_STRIP_KEYS | LOCAL_PREFERRED_KEYS:
            if key in existing:
                merged[key] = existing[key]
        _preserve_local_extra_proxies(merged, existing)
    else:
        # nodelist 型订阅只提供 proxies：以本地配置为基础，仅替换节点并沿用
        # 本地分组模板重建成员，dns/mode/rules 等其余本地键全部保留，
        # 避免 config.yaml 丢失 proxy-groups/rules/dns 导致 render/校验失败。
        old_node_names = [
            str(proxy["name"]) for proxy in existing.get("proxies") or [] if isinstance(proxy, dict) and proxy.get("name")
        ]
        new_node_names = [
            str(proxy["name"]) for proxy in data.get("proxies") or [] if isinstance(proxy, dict) and proxy.get("name")
        ]
        if not new_node_names:
            # 订阅未返回任何节点：直接落盘会把 url-test/fallback 组重建成
            # 空成员（非法配置），宁可失败交给上层回滚，也不产出坏 config。
            raise ValueError("nodelist 订阅未返回任何节点，已跳过合并")
        merged = dict(existing)
        merged["proxies"] = data.get("proxies") or []
        merged["proxy-groups"] = _rebuild_groups_with_new_nodes(
            existing.get("proxy-groups") or [], old_node_names, new_node_names
        )
    merged["subscription-url"] = url

    snapshot_file(paths, path, "config")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        yaml.safe_dump(merged, fh, allow_unicode=True, sort_keys=False)
    return path


def _config_groups(value: object) -> list[str]:
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, list):
        return [str(item) for item in value]
    return []


@dataclass
class ExtraSubscriptionResult:
    name: str
    status: str
    detail: str = ""


def _extra_subscriptions_from_config(config: dict) -> list[tuple[str, str]]:
    """读取 subscriptions 列表（附加机场订阅），返回 (名称, URL) 列表。"""
    subs = config.get("subscriptions")
    if not isinstance(subs, list):
        return []
    entries: list[tuple[str, str]] = []
    for item in subs:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        url = str(item.get("url") or "").strip()
        if name and url:
            entries.append((name, url))
    return entries


def _subscription_region_groups(sub_name: str, proxies: list) -> list:
    """为一家附加订阅生成分组：每个地区一个 url-test 组，外加一个以订阅名
    命名的 select 入口组。订阅名保留 `{name}` / `{name}-*` 分组命名空间。"""
    buckets: dict[str, list[str]] = {}
    for proxy in proxies:
        if isinstance(proxy, dict) and proxy.get("name"):
            buckets.setdefault(match_region(str(proxy["name"])), []).append(str(proxy["name"]))
    groups: list = []
    entry_members: list[str] = []
    for region in SUBSCRIPTION_REGION_ORDER:
        members = buckets.get(region)
        if not members:
            continue
        label = REGION_LABELS.get(region, region)
        entry_members.append(f"{sub_name}-{label}")
        groups.append(
            {
                "name": f"{sub_name}-{label}",
                "type": "url-test",
                "proxies": members,
                "url": TEST_URL,
                "interval": 300,
            }
        )
    if entry_members:
        groups.append({"name": sub_name, "type": "select", "proxies": entry_members})
    return groups


def apply_extra_subscriptions(paths: AppPaths) -> list[ExtraSubscriptionResult]:
    """更新 subscriptions 列表中的附加机场订阅：下载并解析（Clash YAML 或
    base64 分享链接 nodelist 均可），节点按 `{订阅名} ` 前缀重命名后并入
    config，并重建该订阅的地区分组。先全部下载成功再落盘，单项下载失败时
    保留该订阅原有节点与分组，不阻断其它订阅。"""
    config = read_config(paths)
    subs = _extra_subscriptions_from_config(config)
    if not subs:
        return []

    results: list[ExtraSubscriptionResult] = []
    downloaded: list[tuple[str, list]] = []
    for name, url in subs:
        try:
            raw = _download_subscription(paths, url, SUBSCRIPTION_TIMEOUT)
            data = parse_subscription_payload(raw)
            valid_proxies = [
                proxy
                for proxy in data.get("proxies") or []
                if isinstance(proxy, dict) and proxy.get("name") and not _is_panel_info_node(str(proxy["name"]))
            ]
            if not valid_proxies:
                raise ValueError("错误: 订阅未返回任何节点")
            downloaded.append((name, valid_proxies))
            results.append(ExtraSubscriptionResult(name, "已更新", f"{len(valid_proxies)} 个节点"))
        except Exception as exc:
            results.append(ExtraSubscriptionResult(name, "失败", redact_text(str(exc))))
    if not downloaded:
        return results

    sub_names = {name for name, _ in downloaded}
    proxies: list = [
        proxy
        for proxy in config.get("proxies") or []
        if not (
            isinstance(proxy, dict)
            and isinstance(proxy.get("name"), str)
            and any(proxy["name"] == n or proxy["name"].startswith(f"{n} ") for n in sub_names)
        )
    ]
    groups: list = [
        group
        for group in config.get("proxy-groups") or []
        if not (
            isinstance(group, dict)
            and isinstance(group.get("name"), str)
            and any(group["name"] == n or group["name"].startswith(f"{n}-") for n in sub_names)
        )
    ]
    for name, sub_proxies in downloaded:
        prefixed: list = []
        for proxy in sub_proxies:
            renamed = dict(proxy)
            renamed["name"] = f"{name} {str(proxy['name'])}"
            prefixed.append(renamed)
        proxies.extend(prefixed)
        groups.extend(_subscription_region_groups(name, prefixed))
    config["proxies"] = proxies
    config["proxy-groups"] = groups

    path = config_file(paths)
    snapshot_file(paths, path, "config")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        yaml.safe_dump(config, fh, allow_unicode=True, sort_keys=False)
    return results


class RefreshService:
    def __init__(self, paths: AppPaths):
        self.paths = paths
        self.process = ProcessBackend(paths)
        self.query = QueryService(paths)
        self.diagnostics = DiagnosticsService(paths)
        # 测试可注入假的 API 工厂以隔离热重载行为
        self._api_factory = APIBackend

    def refresh(self, subscription_url: str | None = None, groups: list[str] | None = None) -> RefreshReport:
        config = read_config(self.paths)
        url = (subscription_url or str(config.get("subscription-url") or "")).strip()
        if groups:
            target_groups = list(groups)
        else:
            target_groups = _config_groups(config.get("refresh-groups"))

        report = RefreshReport(subscription="跳过", subscription_detail="未配置订阅地址")
        # 订阅域名通常需经代理访问：代理未运行且存在旧 runtime 时先拉起，
        # 避免"代理挂了 → 拉不到订阅 → 无法自愈"的 bootstrap 死锁
        if url and not self.process.is_running() and runtime_file(self.paths).exists():
            try:
                self.process.start()
                # 等待 mihomo 就绪（监听 mixed-port/controller）再拉订阅，
                # 避免启动竞态导致订阅下载 Connection refused
                try:
                    self._wait_for_api()
                except Exception:
                    pass  # 等不到 API 时维持原行为（订阅走直连回退）
            except Exception:
                pass  # 旧 runtime 也起不来时维持原行为（订阅走直连回退）
        # 附加机场订阅（subscriptions 列表）先于主订阅应用：先落地各机场
        # 分组，主订阅合并时才能把这些分组与其入口引用（如 CyberGuard 组里
        # 挂的 "Mitce"）作为本地附加内容保留；单项失败保留旧节点不阻断
        report.extra_subscriptions = apply_extra_subscriptions(self.paths)

        if url:
            try:
                update_source_from_subscription(self.paths, url)
                report.subscription = "已更新"
                report.subscription_detail = redact_text(url)
            except Exception as exc:
                # 订阅失败不阻断后续 render/探测，避免定时任务因订阅站波动整体失效
                report.subscription = "失败"
                report.subscription_detail = redact_text(str(exc))

        report.runtime_path = RuntimeBackend(self.paths).render_runtime()

        report.was_running = self.process.is_running()
        if report.was_running:
            # 优先热重载：mihomo PUT /configs 重新加载配置但不中断既有连接，
            # 避免打断长会话/长任务；API 不可用时回退为进程重启
            try:
                self._api_factory(self.paths).reload_config(str(report.runtime_path))
                report.hot_reloaded = True
            except Exception:
                self.process.restart()
                report.restarted = True

        config_applied = report.restarted or report.hot_reloaded
        if target_groups and config_applied:
            self._wait_for_api()
        for name in target_groups:
            if config_applied:
                report.groups.append(self._probe_and_switch(name))
            else:
                report.groups.append(GroupSwitchResult(group=name, current=None, action="跳过", detail="代理未运行"))
        return report

    def _wait_for_api(self) -> None:
        deadline = time.monotonic() + API_READY_TIMEOUT
        while True:
            try:
                self.query.api.get_groups()
                return
            except APIUnavailableError:
                if time.monotonic() >= deadline:
                    raise RuntimeError("错误: 代理重启后 Mihomo API 在预期时间内未就绪")
                time.sleep(API_READY_INTERVAL)

    def _probe_and_switch(self, group_name: str) -> GroupSwitchResult:
        try:
            group = self.query.get_group(group_name, require_api=True)
        except SystemExit as exc:
            # QueryService 对未知组抛 SystemExit；逐组容错，不中断后续组的探测
            return GroupSwitchResult(group=group_name, current=None, action="失败", detail=str(exc))
        group_type = str(group.type or "").lower()
        if group_type not in {"select", "selector"}:
            return GroupSwitchResult(
                group=group_name,
                current=group.current,
                action="保持不变",
                detail=f"{group.type} 类型自动选路",
            )

        check: GroupCheckReport = self.diagnostics.test_group(group_name)
        ok_items = [item for item in check.results if item.ok and item.delay is not None]
        if not ok_items:
            return GroupSwitchResult(group=group_name, current=group.current, action="保持不变", detail="无可用节点")

        current_check = next((item for item in check.results if item.name == group.current), None)
        if current_check is not None and current_check.ok:
            return GroupSwitchResult(
                group=group_name,
                current=group.current,
                action="保持不变",
                detail=f"{current_check.delay}ms",
            )

        best = min(ok_items, key=lambda item: item.delay or 0)
        self.query.switch_group(group_name, best.name)
        return GroupSwitchResult(
            group=group_name,
            current=group.current,
            action="已切换",
            target=best.name,
            detail=f"{best.delay}ms",
        )
