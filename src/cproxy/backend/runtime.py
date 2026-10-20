from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

import yaml

from ..config import AppPaths, ai_gemini_order_file, ai_manual_order_file, config_file, runtime_file
from ..services.nodelist import (
    SUBSCRIPTION_REGION_ORDER,
    SWITCH_REGION_LABELS,
    is_panel_info_node,
    match_region,
)
from ..snapshots import snapshot_file
from .api import APIBackend
from .models import ProxyGroup

AI_MANUAL_GROUP = "AI-MANUAL"
AI_AUTO_GROUP = "AI-AUTO"
AI_US_GROUP = "AI-US"
AI_SG_GROUP = "AI-SG"
AI_GEMINI_GROUP = "AI-GEMINI"
AI_REGION_JP = "🇯🇵 Japan"
AI_REGION_US = "🇺🇸 United States"
AI_REGION_SG = "🇸🇬 Singapore"
TEST_URL = "https://cp.cloudflare.com/generate_204"
# 实测 agy Gemini 可用的地区（美/日/新/港常被 Google 判 location not supported；韩/台/英低延迟区排欧陆/南美前）
AI_GEMINI_REGION_ORDER = ("KR", "TW", "GB", "DE", "FR", "BR", "AE", "AU")
AI_POLICY_GROUPS = frozenset({AI_MANUAL_GROUP, AI_GEMINI_GROUP, AI_AUTO_GROUP})
# AI 面板（`cproxy ai-status` / `cproxy incident`）要展示的组，及展示顺序。
# 单一来源：此前 cli_render 与 services.ops 各硬编码了一份同样的 5 元组。
AI_STATUS_GROUPS = (AI_MANUAL_GROUP, AI_AUTO_GROUP, AI_US_GROUP, AI_SG_GROUP, AI_GEMINI_GROUP)
CHINAMAX_RULE = "RULE-SET,ChinaMax,DIRECT"
CHINAMAX_PROVIDER = {
    "type": "file",
    "behavior": "classical",
    "path": "./ruleset/ChinaMax.yml",
}
SECRET_PROVIDER_KEYS = {
    "secret-file",
    "secret-systemd-credential",
    "secret-keyring-service",
    "secret-keyring-username",
}

# AI 域名全集（后缀与精确），用于识别任意订阅商的 AI 冲突规则
AI_DOMAINS = frozenset((
    "openai.com", "chatgpt.com", "oaistatic.com", "oaiusercontent.com",
    "sora.com", "anthropic.com", "claude.ai", "claudeusercontent.com",
    "x.ai", "grok.com", "openai.azure.com", "githubcopilot.com",
    "meta.ai", "llama.com", "meta.com",
    "challenges.cloudflare.com",
    "google.com", "googleapis.com", "googleusercontent.com", "gstatic.com",
    "google.dev", "appspot.com", "goog", "gemini.google.com", "aistudio.google.com",
    "ai.google.dev", "generativelanguage.googleapis.com", "cdn.auth0.com",
))
AI_CONFLICT_KEYWORDS = frozenset((
    "openai", "chatgpt", "oaistatic", "oaiusercontent", "anthropic",
    "claude", "claudeusercontent", "gemini", "copilot", "grok",
    "google", "antigravity",
))
AI_PROCESS_NAMES = frozenset(("agy",))


def _is_ai_conflict_rule(rule: object, ai_group: str = AI_MANUAL_GROUP) -> bool:
    """订阅规则若把 AI 域名/关键字指向非 AI 策略组，则视为冲突需移除。

    合法目标为 AI-MANUAL / AI-GEMINI。不限定订阅商组名（SSRDOG/PROXY/其它均可），
    避免换订阅后失效。
    """
    if not isinstance(rule, str):
        return False
    parts = [item.strip() for item in rule.split(",")]
    if len(parts) < 3:
        return False
    rtype = parts[0].upper()
    domain = parts[1].lower()
    if rtype in ("DOMAIN", "DOMAIN-SUFFIX"):
        hit = domain in AI_DOMAINS
    elif rtype == "DOMAIN-KEYWORD":
        hit = domain in AI_CONFLICT_KEYWORDS
    elif rtype in ("PROCESS-NAME", "PROCESS-NAME-REGEX"):
        hit = domain in AI_PROCESS_NAMES or domain.startswith("^agy")
    else:
        return False
    # 末段为目标组（no-resolve 等修饰符位于中间）
    allowed = AI_POLICY_GROUPS if ai_group in AI_POLICY_GROUPS else frozenset({ai_group})
    return hit and parts[-1] not in allowed

EMOJI_REGION_GROUPS = frozenset({AI_REGION_JP, AI_REGION_US, AI_REGION_SG})
AI_REGION_CODE_BY_GROUP = {
    AI_REGION_JP: "JP",
    AI_REGION_US: "US",
    AI_REGION_SG: "SG",
}


def is_stream_fragile_proxy(proxy: dict | None, name: str = "") -> bool:
    """判定节点在长连接 SSE 流（如 Gemini streamGenerateContent）下是否易断流。

    除名字显式标 IEPL / IPLC / 内网专线外，部分机场不写专线标签但底层用裸 TCP + xtls-rprx-vision，
    在 15~60s 工具执行空闲期极易被沿途专线网关静默掐断（EOF / bad record MAC）；
    而 ws / grpc 传输层或非 vision 节点在长连接保持上明显更稳。
    """
    node_name = str(name or (proxy or {}).get("name") or "")
    upper_name = node_name.upper()
    if any(kw in upper_name for kw in ("IEPL", "IPLC", "内网专线")):
        return True
    if not isinstance(proxy, dict):
        return False
    flow = str(proxy.get("flow") or "").strip().lower()
    network = str(proxy.get("network") or "tcp").strip().lower()
    return flow == "xtls-rprx-vision" and network not in {"ws", "grpc"}


def _prefer_non_iepl_nodes(proxies: list, proxy_map: dict[str, dict] | None = None) -> list:
    """区域组把非易断流节点（1X / ws / grpc 等）排在 IEPL 及裸 vision 节点前面。

    Antigravity Cloud Code 的 streamGenerateContent 既按出口 IP 校验地区，又对
    长连接保活敏感；把非 IEPL / 非裸 vision 节点排前面作为热重载/冷启动默认出口。
    """
    names = [str(item) for item in proxies if item]
    fragile = [
        name
        for name in names
        if is_stream_fragile_proxy((proxy_map or {}).get(name), name)
    ]
    fragile_set = set(fragile)
    other = [name for name in names if name not in fragile_set]
    return other + fragile


AUTO_POLICY_NAMES = frozenset({AI_AUTO_GROUP, "自动选择", "Auto", "故障转移"})
SPECIAL_POLICY_LEAVES = frozenset({"DIRECT", "REJECT", "REJECT-DROP", "PASS", "COMPATIBLE"})
SELECTABLE_GROUP_TYPES = frozenset({"selector", "select"})
AUTO_GROUP_TYPES = frozenset({"url-test", "urltest", "fallback", "load-balance", "loadbalance"})
RESERVED_MATCH_NAMES = frozenset(
    {
        AI_MANUAL_GROUP,
        AI_AUTO_GROUP,
        AI_US_GROUP,
        AI_SG_GROUP,
        AI_GEMINI_GROUP,
        AI_REGION_JP,
        AI_REGION_US,
        AI_REGION_SG,
        "GLOBAL",
        "DIRECT",
        "REJECT",
        "PASS",
    }
)


def ai_standby_peer(active_name: str) -> str:
    """与当前 AI 出口成对的备用组：自动池用 AI-US/AI-SG，手动按节点地区映射到另一侧 fallback。"""
    pairs = {
        AI_US_GROUP: AI_SG_GROUP,
        AI_SG_GROUP: AI_US_GROUP,
        AI_REGION_US: AI_SG_GROUP,
        AI_REGION_SG: AI_US_GROUP,
    }
    if active_name in pairs:
        return pairs[active_name]
    code = match_region(active_name)
    if code == "US":
        return AI_SG_GROUP
    if code == "SG":
        return AI_US_GROUP
    return AI_US_GROUP


# AI 组下钻的深度上限（AI-MANUAL → AI-AUTO → AI-US → 叶子，共 3 跳）。
# 上限只是防御性兜底；真正的环守卫是 resolve_ai_leaf 的 seen 集。
_AI_LEAF_MAX_DEPTH = 8


def resolve_ai_leaf(groups: dict[str, ProxyGroup], name: str, seen: frozenset[str] = frozenset()) -> str:
    """沿 `now` 逐层下钻，返回最终的真实节点名。

    兜底组的 `now` 既可能是**嵌套组**（AI-AUTO）也可能是**真实节点**（手动钉住的某个
    节点）。按「一定是组名」的假设去查表会拿到「节点条目」（无 `now`）从而打出空出口，
    这是本函数要消除的核心失真。

    判据：真实节点条目在 /proxies 里恒无候选（见 api._to_proxy_group），所以
    「候选非空 ⇒ 是组」可靠。`seen` 集防自指组（某组 `now` 指回祖先）无限下钻。
    """
    if not name or name == "-" or name in seen or len(seen) >= _AI_LEAF_MAX_DEPTH:
        return name
    group = groups.get(name)
    if group is None or not group.candidates:
        return name
    nxt = str(group.current or "")
    if not nxt or nxt == "-":
        return name
    return resolve_ai_leaf(groups, nxt, seen | {name})


def ai_exit_metrics(groups: dict[str, ProxyGroup], name: str) -> tuple[str, int | None, bool | None]:
    """某组的出口三要素 `(node, delay, alive)`。

    `node` 沿 `now` 下钻到真实节点（兜底组与自动池都适用）。延迟/健康优先取该组自身的
    history——AI-AUTO/AI-US/AI-SG 这类会被 mihomo 健康检查的组有值；AI-MANUAL/AI-GEMINI
    是 fixed fallback，mihomo 不检查它们（`history` 恒空 ⇒ 组自身 delay 为 None），这时
    回退到出口节点自身的 history，避免展示成空。
    """
    group = groups.get(name)
    node = resolve_ai_leaf(groups, name)
    delay = group.delay if group is not None else None
    alive = group.alive if group is not None else None
    leaf = groups.get(node)
    if delay is None and leaf is not None:
        delay = leaf.delay
    if alive is None and leaf is not None:
        alive = leaf.alive
    return node, delay, alive


@dataclass(frozen=True)
class AIExit:
    """单个 AI 出口：承载它的组 + 下钻后的真实节点 + 延迟/健康。"""

    group: str
    node: str
    delay: int | None
    alive: bool | None


# mihomo 健康检查的超时哨兵值：探测未在 `timeout` 内返回时，`history` 里记的就是它。
# 本项目生成的组配置不设 `timeout`，故取 mihomo 默认的 5000ms；`0` 表示「未测得」。
# 两者都不是真实延迟——直接按毫秒展示会把「探测超时」渲染成「很快」。
HEALTH_CHECK_TIMEOUT_MS = 5000


def delay_state(delay: int | None) -> str:
    """延迟的三态：`ok` / `timeout` / `unknown`。

    后端只给状态，具体措辞由各前端（CLI 面板 / TUI）决定，避免 UI 文案下沉到运行时。
    """
    if delay is None or delay <= 0:
        return "unknown"
    if delay >= HEALTH_CHECK_TIMEOUT_MS:
        return "timeout"
    return "ok"


@dataclass(frozen=True)
class AIRoute:
    """AI 出口的统一解析结果——`ai-status` 面板、`--raw`/`--json` 与 TUI 共用同一份。

    `pool` 是直接承载通用出口候选的那一层：自动模式为 AI-AUTO 当前选中的区域组，
    手动模式为 AI-MANUAL。
    """

    present: bool
    auto_mode: bool
    pool: str
    active: AIExit
    standby: AIExit
    gemini: AIExit | None


def resolve_ai_route(groups: dict[str, ProxyGroup]) -> AIRoute:
    """解析 AI 出口（备用/Gemini 一并），供 CLI 与 TUI 共用。

    这是「AI-MANUAL 的 now 可能是嵌套组、也可能是真实节点」这一不变式的单一定义点。
    备用组的映射分两种口径（见 `ai_standby_peer`）：自动模式按**池组**取显式成对
    （AI-US↔AI-SG）；手动模式按**出口节点的地区**映射到另一侧，否则钉在美国节点时
    会把「备用」算成它自己所在的 AI-US。
    """
    manual = groups.get(AI_MANUAL_GROUP)
    auto = groups.get(AI_AUTO_GROUP)
    target = str(manual.current) if manual is not None else "-"
    auto_mode = target == AI_AUTO_GROUP
    pool = str(auto.current) if auto_mode and auto is not None else AI_MANUAL_GROUP

    active_node, active_delay, active_alive = ai_exit_metrics(groups, pool)

    standby_group = ai_standby_peer(pool if auto_mode else active_node)
    standby_node, standby_delay, standby_alive = ai_exit_metrics(groups, standby_group)

    gemini: AIExit | None = None
    if AI_GEMINI_GROUP in groups:
        gemini_node, gemini_delay, gemini_alive = ai_exit_metrics(groups, AI_GEMINI_GROUP)
        gemini = AIExit(AI_GEMINI_GROUP, gemini_node, gemini_delay, gemini_alive)

    return AIRoute(
        present=manual is not None and auto is not None,
        auto_mode=auto_mode,
        pool=pool,
        active=AIExit(pool, active_node, active_delay, active_alive),
        standby=AIExit(standby_group, standby_node, standby_delay, standby_alive),
        gemini=gemini,
    )


def ai_manual_toggle_target(candidates: list[str], active_name: str) -> str | None:
    """手动模式下在 AI-MANUAL 的美/新节点之间切换。"""
    want = "SG" if match_region(active_name) == "US" else "US"
    for name in candidates:
        if name in {AI_AUTO_GROUP, AI_US_GROUP, AI_SG_GROUP}:
            continue
        if match_region(name) == want:
            return name
    return None


def _match_group_name(rules: list | None) -> str | None:
    if not isinstance(rules, list):
        return None
    for rule in reversed(rules):
        text = str(rule)
        if text.startswith("MATCH,"):
            return text.split(",", 1)[1].strip() or None
    return None


def _extra_subscription_names(data: dict) -> list[str]:
    names: list[str] = []
    for item in data.get("subscriptions") or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if name:
            names.append(name)
    return names


def _is_extra_node(name: str, extra_names: list[str] | set[str]) -> bool:
    for extra in extra_names:
        if name == extra or name.startswith(f"{extra} "):
            return True
    return False


def _drop_named_groups(groups: list, names: set[str]) -> list:
    kept: list = []
    for group in groups:
        if not isinstance(group, dict):
            kept.append(group)
            continue
        if str(group.get("name") or "") in names:
            continue
        members = group.get("proxies")
        if isinstance(members, list):
            group["proxies"] = [member for member in members if str(member) not in names]
        kept.append(group)
    return kept


def _proxy_map(proxies: list | None) -> dict[str, dict]:
    return {
        str(proxy["name"]): proxy
        for proxy in proxies or []
        if isinstance(proxy, dict) and proxy.get("name")
    }


def _collect_ai_region_nodes(
    proxies: list | None,
    group_map: dict[str, dict],
    extra_names: list[str],
) -> dict[str, list[str]]:
    """收集 AI 用的日/美/新 + 韩/英节点；不创建第二套国家 selector。

    韩/英仅进 AI-MANUAL 的 codex 候选池（实测 KR/GB 的 IEPL 对 codex 稳定），
    AI-US / AI-SG / AI-AUTO 仍只用美/新，避免污染自动池的地区语义。
    """
    extra_set = set(extra_names)
    proxy_by_name = _proxy_map(proxies)
    buckets: dict[str, list[str]] = {"JP": [], "US": [], "SG": [], "KR": [], "GB": []}
    seen: dict[str, set[str]] = {code: set() for code in buckets}

    def add(code: str, name: str) -> None:
        if (
            not name
            or name in seen[code]
            or is_panel_info_node(name)
            or name in extra_set
            or _is_extra_node(name, extra_names)
            or name in group_map
        ):
            return
        seen[code].add(name)
        buckets[code].append(name)

    for proxy in proxies or []:
        if not isinstance(proxy, dict) or not proxy.get("name"):
            continue
        name = str(proxy["name"])
        code = match_region(name)
        if code in buckets:
            add(code, name)

    for group_name, code in AI_REGION_CODE_BY_GROUP.items():
        region = group_map.get(group_name)
        if not isinstance(region, dict):
            continue
        for member in region.get("proxies") or []:
            add(code, str(member))

    return {code: _prefer_non_iepl_nodes(names, proxy_by_name) for code, names in buckets.items()}


def _collect_gemini_nodes(
    proxies: list | None,
    group_map: dict[str, dict],
    extra_names: list[str],
    prefer: list | None = None,
) -> list[str]:
    """收集 Gemini 专用出口：优先韩/台/欧/巴东等实测可用地区，1X/ws/grpc 排 IEPL/裸vision 前。

    prefer（config.yaml 的 ai-gemini-prefer）按给定顺序把已入选的节点提到最前，
    用于把实测更稳的节点钉成 fallback 默认出口；未入选（改名/不在优选地区）
    的名字静默忽略，不绕过地区筛选。
    """
    extra_set = set(extra_names)
    proxy_by_name = _proxy_map(proxies)
    buckets: dict[str, list[str]] = {code: [] for code in AI_GEMINI_REGION_ORDER}
    seen: set[str] = set()

    def add(code: str, name: str) -> None:
        if (
            code not in buckets
            or not name
            or name in seen
            or is_panel_info_node(name)
            or name in extra_set
            or _is_extra_node(name, extra_names)
            or name in group_map
        ):
            return
        seen.add(name)
        buckets[code].append(name)

    for proxy in proxies or []:
        if not isinstance(proxy, dict) or not proxy.get("name"):
            continue
        name = str(proxy["name"])
        add(match_region(name), name)

    ordered: list[str] = []
    for code in AI_GEMINI_REGION_ORDER:
        ordered.extend(_prefer_non_iepl_nodes(buckets[code], proxy_by_name))
    ordered = _prefer_non_iepl_nodes(ordered, proxy_by_name)
    for name in reversed([str(item) for item in prefer or []]):
        if name in ordered:
            ordered.remove(name)
            ordered.insert(0, name)
    return ordered


def _collect_extra_ai_nodes(
    proxies: list | None,
    group_map: dict[str, dict],
    extra_names: list[str],
    regions: tuple[str, ...],
) -> list[str]:
    """收集附加机场（subscriptions）中属于指定 AI 地区的真实节点，作为主机场全挂时的跨供应商热备。

    只从 proxies 列表收集以附加订阅名为前缀的真实节点，不碰主订阅节点与分组名。
    """
    if not extra_names:
        return []
    proxy_by_name = _proxy_map(proxies)
    buckets: dict[str, list[str]] = {code: [] for code in regions}
    seen: set[str] = set()
    for proxy in proxies or []:
        if not isinstance(proxy, dict) or not proxy.get("name"):
            continue
        name = str(proxy["name"])
        if (
            not name
            or name in seen
            or is_panel_info_node(name)
            or name in group_map
            or not _is_extra_node(name, extra_names)
        ):
            continue
        code = match_region(name)
        if code in buckets:
            seen.add(name)
            buckets[code].append(name)
    ordered: list[str] = []
    for code in regions:
        ordered.extend(_prefer_non_iepl_nodes(buckets[code], proxy_by_name))
    return _prefer_non_iepl_nodes(ordered, proxy_by_name)


def _load_order_file(path: Path) -> list[str]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    return [str(item) for item in data if item]


def _load_ai_manual_order(paths: AppPaths) -> list[str]:
    """读取 rebalance 持久化的 AI-MANUAL 稳定度排序；缺失/损坏时返回空列表（回退静态序）。"""
    return _load_order_file(ai_manual_order_file(paths))


def _load_ai_gemini_order(paths: AppPaths) -> list[str]:
    """读取 rebalance 持久化的 AI-GEMINI 稳定度排序；缺失/损坏时返回空列表（回退静态序）。"""
    return _load_order_file(ai_gemini_order_file(paths))


DEFAULT_MAX_AI_MULTIPLIER = 3.0
_STAGGERED_AUTO_INTERVALS = (317, 323, 331, 337, 347, 349, 353, 359)


def _deprioritize_high_multiplier(
    nodes: list[str],
    max_mult: float = DEFAULT_MAX_AI_MULTIPLIER,
    prefer: list | None = None,
) -> list[str]:
    """将高于阈值（默认 >= 3.0X）的『高倍率刺客』节点沉底，除非用户已在 prefer 中显式钉选。"""
    if max_mult <= 0:
        return list(nodes)
    from ..services.node_tags import parse_node_tag

    pinned = {str(item) for item in (prefer or []) if item}
    normal: list[str] = []
    high: list[str] = []
    for name in nodes:
        if name in pinned or parse_node_tag(name).multiplier < max_mult:
            normal.append(name)
        else:
            high.append(name)
    return normal + high


def _order_manual_candidates(
    leaves: list[str],
    prefer: list | None,
    order: list | None,
    *,
    max_mult: float = DEFAULT_MAX_AI_MULTIPLIER,
) -> list[str]:
    """按「用户钉选 → 实测稳定度序 → 静态地区兜底」排 AI-MANUAL 叶子候选，并将高倍率节点沉底。

    AI-MANUAL 是 fallback：mihomo 按列表顺序找第一个存活节点，顺序即兜底优先级。
    prefer 是 config 的 ai-manual-prefer（手动钉选），order 是 cproxy ai-rebalance
    的实测稳定度序，静态兜底沿用美/日/新 + 非 IEPL 优先。
    """
    leaves_set = {str(name) for name in leaves}
    ordered: list[str] = []
    seen: set[str] = set()

    def add(name: str) -> None:
        if not name or name in seen or name not in leaves_set:
            return
        seen.add(name)
        ordered.append(name)

    for name in [str(item) for item in prefer or []]:
        add(name)
    for name in [str(item) for item in order or []]:
        add(name)
    for name in leaves:
        add(name)
    return _deprioritize_high_multiplier(ordered, max_mult=max_mult, prefer=prefer)


def _strip_panel_members(group: dict) -> None:
    members = group.get("proxies") or []
    group["proxies"] = [member for member in members if not is_panel_info_node(str(member))]


def _sanitize_auto_groups(groups: list) -> None:
    """清理自动测速/故障转移组：剔除面板伪节点、升级 HTTPS 测速 URL、并对同秒 300s 周期错峰。

    1. unified-delay 开启时 mihomo 会对测速 URL 连发两次 HEAD；部分机场在第一次明文 HTTP
       返回 204 后截断第二次请求，统一升级到 HTTPS `TEST_URL` 可彻底规避。
    2. 机场自带的 `自动选择`/`故障转移` 默认均为 `interval: 300`，若与 `AI-MANUAL` (300s)
       同秒触发，会对共享节点（如英国/韩国节点）产生 4 路并发测速踩踏，导致排在队尾的测速
       超时并引发 fallback 误切换。将非 AI 组的 300s 周期错峰为互质素数周期可根除碰撞。
    """
    managed_ai = {AI_US_GROUP, AI_SG_GROUP, AI_AUTO_GROUP, AI_MANUAL_GROUP, AI_GEMINI_GROUP}
    stagger_idx = 0
    for group in groups:
        if not isinstance(group, dict):
            continue
        name = str(group.get("name") or "")
        gtype = str(group.get("type") or "").lower()
        if name not in AUTO_POLICY_NAMES and gtype not in AUTO_GROUP_TYPES:
            continue
        _strip_panel_members(group)
        url = group.get("url")
        if isinstance(url, str) and url.strip().lower().startswith("http://"):
            group["url"] = TEST_URL
        if name not in managed_ai and group.get("interval") == 300:
            group["interval"] = _STAGGERED_AUTO_INTERVALS[stagger_idx % len(_STAGGERED_AUTO_INTERVALS)]
            stagger_idx += 1


def _normalize_ip_cidr_rule(rule: object) -> object:
    """为裸 3 段 IP-CIDR / IP-CIDR6 规则补齐 `,no-resolve`。

    订阅规则常在 GEOIP,CN,DIRECT,no-resolve 之前放一串裸 `IP-CIDR,192.168.0.0/16,DIRECT` 等，
    缺省 `no-resolve` 会迫使 mihomo 在匹配到该条时先对域名发起本地 DNS 解析，既拖慢首包又使
    后续 `no-resolve` 失效。
    """
    if not isinstance(rule, str):
        return rule
    parts = [item.strip() for item in rule.split(",")]
    if len(parts) == 3 and parts[0].upper() in {"IP-CIDR", "IP-CIDR6"}:
        return f"{parts[0]},{parts[1]},{parts[2]},no-resolve"
    return rule


def _collect_match_nodes(
    match_group: dict,
    group_map: dict[str, dict],
    proxies: list | None,
    extra_names: list[str],
) -> list[str]:
    extra_set = set(extra_names)
    members = [str(item) for item in match_group.get("proxies") or []]
    nodes: list[str] = []
    seen: set[str] = set()

    def add(name: str) -> None:
        if (
            not name
            or name in seen
            or is_panel_info_node(name)
            or name in SPECIAL_POLICY_LEAVES
            or name in AUTO_POLICY_NAMES
            or name in extra_set
            or _is_extra_node(name, extra_names)
            or name in group_map
        ):
            return
        seen.add(name)
        nodes.append(name)

    for member in members:
        if member in extra_set:
            continue
        child = group_map.get(member)
        if child is not None:
            child_type = str(child.get("type") or "").lower()
            if member in AUTO_POLICY_NAMES or child_type in AUTO_GROUP_TYPES:
                for nested in child.get("proxies") or []:
                    add(str(nested))
            continue
        if member in AUTO_POLICY_NAMES or member in SPECIAL_POLICY_LEAVES:
            continue
        add(member)
    if not nodes:
        for proxy in proxies or []:
            if isinstance(proxy, dict) and proxy.get("name"):
                add(str(proxy["name"]))
    return nodes


def _reshape_match_country_groups(data: dict, groups: list, group_map: dict[str, dict]) -> None:
    """给 MATCH 指向的主订阅 select 组注入独立国家 selector，并剔除面板信息节点。

    不复用 AI 的 `🇯🇵 Japan`：默认流量与 AI 出口必须能各自选日本节点。
    附加订阅入口从 MATCH 成员里拿掉，保持与主订阅同级。
    """
    match_name = _match_group_name(data.get("rules"))
    extra_names = _extra_subscription_names(data)
    if not match_name or match_name in RESERVED_MATCH_NAMES or match_name in extra_names:
        return
    match_group = group_map.get(match_name)
    if not isinstance(match_group, dict):
        return
    if str(match_group.get("type") or "").lower() not in SELECTABLE_GROUP_TYPES:
        return

    extra_set = set(extra_names)
    members = [str(item) for item in match_group.get("proxies") or [] if str(item) not in extra_set]
    for member in members:
        child = group_map.get(member)
        if child is not None and (
            member in AUTO_POLICY_NAMES or str(child.get("type") or "").lower() in AUTO_GROUP_TYPES
        ):
            _strip_panel_members(child)

    nodes = _collect_match_nodes(match_group, group_map, data.get("proxies"), extra_names)
    region_group_names: list[str] = []
    if nodes:
        buckets: dict[str, list[str]] = {}
        for node in nodes:
            buckets.setdefault(match_region(node), []).append(node)
        for region in SUBSCRIPTION_REGION_ORDER:
            region_members = buckets.get(region) or []
            if not region_members:
                continue
            label = SWITCH_REGION_LABELS.get(region, region)
            if label == match_name or label in extra_set or label in RESERVED_MATCH_NAMES:
                continue
            existing = group_map.get(label)
            if existing is None:
                injected = {"name": label, "type": "select", "proxies": list(region_members)}
                groups.append(injected)
                group_map[label] = injected
            elif str(existing.get("type") or "").lower() in SELECTABLE_GROUP_TYPES and existing is not match_group:
                existing["proxies"] = list(region_members)
            else:
                continue
            region_group_names.append(label)

    auto_members: list[str] = []
    special_members: list[str] = []
    leftover_nested: list[str] = []
    for member in members:
        if is_panel_info_node(member):
            continue
        child = group_map.get(member)
        child_type = str(child.get("type") or "").lower() if child is not None else ""
        if member in AUTO_POLICY_NAMES or child_type in AUTO_GROUP_TYPES:
            auto_members.append(member)
        elif member in SPECIAL_POLICY_LEAVES:
            special_members.append(member)
        elif member in region_group_names:
            continue
        elif child is not None:
            if member in {AI_US_GROUP, AI_SG_GROUP, AI_REGION_JP, AI_REGION_US, AI_REGION_SG}:
                continue
            leftover_nested.append(member)

    rebuilt: list[str] = []
    for member in auto_members + leftover_nested + region_group_names + special_members:
        if member not in rebuilt:
            rebuilt.append(member)
    match_group["proxies"] = rebuilt


def _sanitize_dns_fallback_filter(data: dict) -> None:
    # 部分订阅的 dns.fallback-filter.geosite 依赖 GeoSite.dat；该文件在本地
    # 时常损坏或下载超时，会导致 mihomo 启动卡住。清理该字段以保持启动稳定。
    dns = data.get("dns")
    if not isinstance(dns, dict):
        return
    fallback_filter = dns.get("fallback-filter")
    if isinstance(fallback_filter, dict):
        fallback_filter.pop("geosite", None)


_WS_TLS_PROBE_CACHE: dict[tuple[str, int, str], tuple[float, int | None]] = {}
_WS_CDN_IP_RANK_CACHE: dict[tuple[tuple[str, ...], int, str], tuple[float, list[str]]] = {}
_WS_TLS_CACHE_TTL = 300.0


def _tls_handshake_latency_ms(ip: str, port: int, sni: str, timeout: float = 2.5) -> int | None:
    import socket
    import ssl
    import time

    now = time.time()
    cache_key = (ip, port, sni)
    cached = _WS_TLS_PROBE_CACHE.get(cache_key)
    if cached is not None and (now - cached[0]) < _WS_TLS_CACHE_TTL:
        return cached[1]
    t0 = time.time()
    sock: socket.socket | None = None
    tls_sock: ssl.SSLSocket | None = None
    latency: int | None = None
    try:
        sock = socket.create_connection((ip, port), timeout=timeout)
        ctx = ssl.create_default_context()
        tls_sock = ctx.wrap_socket(sock, server_hostname=sni or ip)
        latency = max(1, round((time.time() - t0) * 1000))
    except Exception:
        latency = None
    finally:
        if tls_sock is not None:
            try:
                tls_sock.close()
            except OSError:
                pass
        elif sock is not None:
            try:
                sock.close()
            except OSError:
                pass
    _WS_TLS_PROBE_CACHE[cache_key] = (time.time(), latency)
    return latency


def _ws_cdn_ip_log_penalties(
    ips: tuple[str, ...],
    port: int,
    paths: AppPaths | None = None,
) -> tuple[dict[str, int], dict[str, int], set[str]]:
    """从 mihomo 运行日志与当前 runtime.yaml 提取各候选 Anycast IP 的真实拨号超时统计与在用状态。

    返回 `(recent_errors_20m, total_errors, active_ips)`：
    - `recent_errors_20m`: 最近 20 分钟内该 IP 发生 `connect error` / `i/o timeout` 的次数；
    - `total_errors`: 日志尾部（512KB）内该 IP 累计拨号错误次数；
    - `active_ips`: 当前 `runtime.yaml` 中已钉选在用的 IP 集合。
    """
    from datetime import datetime, timezone

    recent_errors: dict[str, int] = {ip: 0 for ip in ips}
    total_errors: dict[str, int] = {ip: 0 for ip in ips}
    active_ips: set[str] = set()
    if paths is None:
        if os.environ.get("PYTEST_CURRENT_TEST"):
            return recent_errors, total_errors, active_ips
        from ..config import default_paths

        paths = default_paths()
    try:
        rt_path = runtime_file(paths)
        if rt_path.exists():
            rt_text = rt_path.read_text(encoding="utf-8", errors="ignore")
            for ip in ips:
                if ip in rt_text:
                    active_ips.add(ip)
    except OSError:
        pass
    try:
        from ..config import log_file

        lpath = log_file(paths)
        if not lpath.exists():
            return recent_errors, total_errors, active_ips
        size = lpath.stat().st_size
        with lpath.open("rb") as fh:
            if size > 2097152:
                fh.seek(size - 2097152)
            tail_text = fh.read().decode("utf-8", errors="ignore")
        now_ts = datetime.now(timezone.utc).timestamp()
        for line in tail_text.splitlines():
            if "connect error:" not in line and "i/o timeout" not in line:
                continue
            matched_ips = [
                ip
                for ip in ips
                if f"error: {ip}:{port} connect error:" in line or f"dial tcp {ip}:{port}:" in line
            ]
            if not matched_ips:
                continue
            is_recent = False
            if line.startswith('time="') and len(line) >= 31:
                raw_ts = line[6:31]
                try:
                    # 截取秒级 ISO8601 如 2026-10-20T12:05:41+08:00 或带纳秒的时间戳
                    ts_end = line.find('"', 6)
                    iso_str = line[6:ts_end] if ts_end > 6 else raw_ts
                    if "." in iso_str:
                        head, tail = iso_str.split(".", 1)
                        tz_idx = max(tail.find("+"), tail.find("-"), tail.find("Z"))
                        if tz_idx >= 0:
                            iso_str = head + "." + tail[: min(6, tz_idx)] + tail[tz_idx:]
                    dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
                    if (now_ts - dt.timestamp()) <= 1200.0:
                        is_recent = True
                except ValueError:
                    pass
            for ip in matched_ips:
                total_errors[ip] = total_errors.get(ip, 0) + 1
                if is_recent:
                    recent_errors[ip] = recent_errors.get(ip, 0) + 1
    except OSError:
        pass
    return recent_errors, total_errors, active_ips


def _resolve_healthy_ws_cdn_ips(
    domain: str,
    port: int,
    sni: str,
    *,
    rounds: int = 3,
    paths: AppPaths | None = None,
    extra_domains: tuple[str, ...] = (),
) -> list[str]:
    """当 CDN 域名（含原 server 与 ws-opts.headers.Host）解析出多个 IPv4 A 记录时，
    结合多轮并发 TLS 握手与 mihomo 真实拨号超时日志动态评估，
    按（近20m无超时突增、握手成功率↓、近20m错误分桶↑、最大延迟<1200ms、在用健康IP黏性、历史累计超时分桶↑、网段与延迟分桶↑）排序。
    既杜绝周期性刷新在双 Anycast IP 间来回横跳切回偶发黑洞 IP，又能在当前 IP 真正劣化时自动无缝故障转移。
    """
    import ipaddress
    import socket
    import time
    from concurrent.futures import ThreadPoolExecutor

    try:
        ipaddress.ip_address(domain)
        return []
    except ValueError:
        pass
    resolved_ips: set[str] = set()
    for candidate_domain in (domain, *extra_domains):
        if not candidate_domain:
            continue
        try:
            ipaddress.ip_address(candidate_domain)
            resolved_ips.add(candidate_domain)
            continue
        except ValueError:
            pass
        try:
            infos = socket.getaddrinfo(candidate_domain, port, socket.AF_INET, socket.SOCK_STREAM)
            for item in infos:
                if item and len(item) >= 5 and item[4]:
                    resolved_ips.add(str(item[4][0]))
        except OSError:
            continue
    ips = tuple(sorted(resolved_ips))
    if len(ips) < 2:
        return []
    now = time.time()
    rank_key = (ips, port, sni)
    cached_rank = _WS_CDN_IP_RANK_CACHE.get(rank_key)
    if cached_rank is not None and (now - cached_rank[0]) < _WS_TLS_CACHE_TTL:
        return list(cached_rank[1])

    samples_by_ip: dict[str, list[int]] = {ip: [] for ip in ips}
    eff_rounds = max(1, int(rounds))
    for r in range(eff_rounds):
        if r > 0:
            for ip in ips:
                _WS_TLS_PROBE_CACHE.pop((ip, port, sni), None)
        with ThreadPoolExecutor(max_workers=min(4, len(ips))) as pool:
            latencies = list(pool.map(lambda ip: (ip, _tls_handshake_latency_ms(ip, port, sni)), ips))
        for ip, ms in latencies:
            if ms is not None:
                samples_by_ip[ip].append(ms)

    recent_errors, total_errors, active_ips = _ws_cdn_ip_log_penalties(ips, port, paths=paths)
    alive_ips = [ip for ip, samples in samples_by_ip.items() if samples]
    if not alive_ips:
        return []
    min_total_err = min(total_errors.get(ip, 0) for ip in alive_ips)

    scored: list[tuple[tuple[int, int, int, int, int, int, int, int, int, int, str], str]] = []
    for ip in alive_ips:
        samples = samples_by_ip[ip]
        succ = len(samples)
        max_ms = max(samples)
        avg_ms = round(sum(samples) / succ)
        rec_err = recent_errors.get(ip, 0)
        tot_err = total_errors.get(ip, 0)
        is_active_healthy = (
            ip in active_ips
            and succ >= max(1, eff_rounds - 1)
            and max_ms < 1800
            and rec_err < 3
            and tot_err <= min_total_err + 5
        )
        prefer_non_104 = 0 if not ip.startswith("104.") else 1
        sort_key = (
            0 if rec_err < 3 else 1,
            0 if tot_err <= min_total_err + 5 else 1,
            0 if is_active_healthy else 1,
            -succ,
            rec_err // 3,
            tot_err // 5,
            0 if max_ms < 1200 else 1,
            prefer_non_104,
            avg_ms // 100,
            max_ms // 100,
            ip,
        )
        scored.append((sort_key, ip))
    scored.sort(key=lambda item: item[0])
    result = [ip for _k, ip in scored]
    _WS_CDN_IP_RANK_CACHE[rank_key] = (time.time(), result)
    return list(result)


def _extract_proxy_servers(raw_yaml: str) -> dict[str, str]:
    if not raw_yaml:
        return {}
    try:
        data = yaml.safe_load(raw_yaml)
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    result: dict[str, str] = {}
    for proxy in data.get("proxies") or []:
        if isinstance(proxy, dict) and proxy.get("name") and proxy.get("server"):
            result[str(proxy["name"])] = str(proxy["server"])
    return result


def refresh_ws_cdn_ips_if_changed(paths: AppPaths) -> bool:
    """清空 CDN TLS 探测缓存并重新探测渲染；仅当节点底层 CDN Anycast IP 真正发生切换时才热重载 mihomo。"""
    _WS_TLS_PROBE_CACHE.clear()
    _WS_CDN_IP_RANK_CACHE.clear()
    rt_path = runtime_file(paths)
    old_text = rt_path.read_text(encoding="utf-8") if rt_path.exists() else ""
    old_servers = _extract_proxy_servers(old_text)
    RuntimeBackend(paths).render_runtime()
    new_text = rt_path.read_text(encoding="utf-8") if rt_path.exists() else ""
    new_servers = _extract_proxy_servers(new_text)
    if old_servers and new_servers and old_servers != new_servers:
        from .process import ProcessBackend

        ProcessBackend(paths).reload_if_running(rt_path)
        return True
    if old_text and new_text and old_text != new_text:
        from .process import ProcessBackend

        ProcessBackend(paths).mark_runtime_applied(rt_path)
    return False


def _resolve_healthy_ws_cdn_ip(domain: str, port: int, sni: str) -> str | None:
    """返回当前最优的可完成 TLS 握手的健康 CDN Anycast IP。"""
    healthy = _resolve_healthy_ws_cdn_ips(domain, port, sni)
    return healthy[0] if healthy else None


def _optimize_ws_cdn_servers(
    proxies: list | None,
    *,
    probe_tls: bool = False,
    paths: AppPaths | None = None,
) -> None:
    """将 443 端口 Cloudflare WebSocket 节点的拥塞共享入口域名替换为节点独立 ws-opts.headers.Host，
    并在启用 probe_tls 时自动剔除 TLS 黑洞 IP、将健康 Anycast IP 确定性分摊到各 WebSocket 节点。

    1. 部分机场（如 cyberguard.*）把全部 1X WebSocket 节点的 `server` 指向同一个旧优选域名
       （解析到单一高延迟 IP `172.64.146.118`）；而节点自带的 `ws-opts.headers.Host`
       （如 `cgkr1.cyberguard-u1s1.art`，属 `servername` 的合法子域）解析到双栈 Anycast 池。
    2. 当 CDN 子域返回多个 Anycast IPv4（例如同时返回 `104.21.87.131` 与 `172.67.143.95`）时，
       自动过滤掉 TLS 握手超时的黑洞 IP，并将存活的健康 IP 按确定性顺序分摊钉选到各节点，
       既消除 mihomo 每次建连的出站 DNS 阻塞与配置文件哈希抖动，又避免多组探测流量打在同一单点 IP 上触发 GFW 限流。
    """
    if not isinstance(proxies, list):
        return
    if probe_tls:
        _WS_TLS_PROBE_CACHE.clear()
        _WS_CDN_IP_RANK_CACHE.clear()
    ws_idx = 0
    for proxy in proxies:
        if not isinstance(proxy, dict):
            continue
        if str(proxy.get("network") or "").strip().lower() != "ws":
            continue
        try:
            port = int(proxy.get("port") or 0)
        except (TypeError, ValueError):
            continue
        if port != 443:
            continue
        server = str(proxy.get("server") or "").strip()
        sni = str(proxy.get("servername") or proxy.get("sni") or "").strip().lower()
        ws_opts = proxy.get("ws-opts")
        headers = ws_opts.get("headers") if isinstance(ws_opts, dict) else None
        ws_host = str(
            (headers.get("Host") or headers.get("host") or "") if isinstance(headers, dict) else ""
        ).strip()
        if not server or not sni or not ws_host:
            continue
        if ws_host.lower() != server.lower() and ws_host.lower().endswith("." + sni):
            proxy["server"] = ws_host
        if probe_tls:
            target_domain = str(proxy.get("server") or "")
            healthy_ips = _resolve_healthy_ws_cdn_ips(
                target_domain,
                port,
                sni,
                paths=paths,
                extra_domains=(server,) if server and server.lower() != target_domain.lower() else (),
            )
            if healthy_ips:
                proxy["server"] = healthy_ips[0]
        ws_idx += 1


class RuntimeBackend:
    def __init__(self, paths: AppPaths):
        self.paths = paths

    def mixed_port(self) -> int:
        for candidate in (runtime_file(self.paths), config_file(self.paths)):
            if not candidate.exists():
                continue
            try:
                data = yaml.safe_load(candidate.read_text(encoding="utf-8")) or {}
            except Exception:
                continue
            if isinstance(data, dict):
                port = data.get("mixed-port") or data.get("port")
                if isinstance(port, int) and port > 0:
                    return port
        return 7890

    def get_groups(self) -> dict[str, ProxyGroup]:
        path = runtime_file(self.paths)
        if not path.exists():
            raise FileNotFoundError(f"runtime config not found: {path}")

        with path.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}

        groups: dict[str, ProxyGroup] = {}
        for item in data.get("proxy-groups", []):
            if not isinstance(item, dict):
                continue
            name = item.get("name")
            if not name:
                continue
            proxies = [str(proxy) for proxy in item.get("proxies", [])]
            groups[str(name)] = ProxyGroup(
                name=str(name),
                type=str(item.get("type", "")),
                current=proxies[0] if proxies else "-",
                candidates=proxies,
                source="runtime",
            )
        return groups

    def render_runtime(self) -> Path:
        source_path = config_file(self.paths)
        target_path = runtime_file(self.paths)

        with source_path.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}

        _sanitize_dns_fallback_filter(data)
        _optimize_ws_cdn_servers(
            data.get("proxies"),
            probe_tls=not bool(os.environ.get("PYTEST_CURRENT_TEST")),
            paths=self.paths,
        )

        # 若 dns.ipv6 关闭（或未显式开启），同步关闭顶层 ipv6，防止 mihomo 出站拨号对双栈 Cloudflare
        # 优选域名尝试连接不可达的 IPv6 地址（[2606:4700:...]:443 network is unreachable）拖慢建连。
        if not (isinstance(data.get("dns"), dict) and data["dns"].get("ipv6") is True):
            data["ipv6"] = False

        # 关闭 tcp-concurrent：避免出站拨号前先在本机跑 DNS（223.5.5.5 + 1.1.1.1/8.8.8.8 fallback）
        # 造成每次新建 TLS 连接卡顿 4~5s，或把经默认池解析出的日本/香港边缘 IP 喂给韩国/新加坡 AI 节点。
        data["tcp-concurrent"] = False

        # 每个连接都查找发起进程，供 /connections API 与流量统计按进程归因
        if not data.get("find-process-mode"):
            data["find-process-mode"] = "always"

        # 开启 15s TCP Keep-Alive（mihomo 默认 KeepAliveIdle/Interval 为 0，会退回内核默认 7200s），
        # 防止 agy / codex 在本地执行工具或等待用户输入（15~60s 空闲）期间连接被沿途 NAT/网关静默回收。
        data["disable-keep-alive"] = False
        if not data.get("keep-alive-idle"):
            data["keep-alive-idle"] = 15
        if not data.get("keep-alive-interval"):
            data["keep-alive-interval"] = 15

        rule_providers = data.get("rule-providers")
        if not isinstance(rule_providers, dict):
            rule_providers = {}
        if "ChinaMax" not in rule_providers:
            rule_providers["ChinaMax"] = dict(CHINAMAX_PROVIDER)
        data["rule-providers"] = rule_providers

        secret = APIBackend(self.paths).api_secret()
        if secret:
            data["secret"] = secret
        for key in SECRET_PROVIDER_KEYS:
            data.pop(key, None)

        groups = data.get("proxy-groups") or []
        if not isinstance(groups, list):
            raise ValueError("proxy-groups 必须是列表")

        from ..services.ban import filter_unbanned_nodes, load_active_bans

        active_bans = load_active_bans(self.paths)
        group_map = {group["name"]: group for group in groups if isinstance(group, dict) and group.get("name")}
        extra_names = _extra_subscription_names(data)
        region_nodes = _collect_ai_region_nodes(data.get("proxies"), group_map, extra_names)
        us_proxies = filter_unbanned_nodes(region_nodes["US"], active_bans, keep_fallback=True)
        sg_proxies = filter_unbanned_nodes(region_nodes["SG"], active_bans, keep_fallback=True)
        jp_proxies = filter_unbanned_nodes(region_nodes["JP"], active_bans, keep_fallback=False)
        kr_proxies = filter_unbanned_nodes(region_nodes["KR"], active_bans, keep_fallback=False)
        gb_proxies = filter_unbanned_nodes(region_nodes["GB"], active_bans, keep_fallback=False)
        raw_max_mult = data.get("max-ai-multiplier")
        try:
            max_ai_mult = float(raw_max_mult) if raw_max_mult is not None else DEFAULT_MAX_AI_MULTIPLIER
        except (TypeError, ValueError):
            max_ai_mult = DEFAULT_MAX_AI_MULTIPLIER

        gemini_prefer = data.get("ai-gemini-prefer")
        if not isinstance(gemini_prefer, list):
            gemini_prefer = []
        gemini_order = _load_ai_gemini_order(self.paths)
        gemini_leaves = _collect_gemini_nodes(data.get("proxies"), group_map, extra_names, gemini_prefer)
        gemini_leaves = filter_unbanned_nodes(gemini_leaves, active_bans, keep_fallback=True)
        # 兼容偏科机场：缺少 US 或 SG 任一地区（或仅含日/韩/台/欧节点）时，优雅借用其它可用 AI 地区池兜底
        fallback_ai_pool = list(us_proxies or sg_proxies or jp_proxies or kr_proxies or gb_proxies or gemini_leaves)
        if not us_proxies and fallback_ai_pool:
            us_proxies = list(fallback_ai_pool)
        if not sg_proxies and fallback_ai_pool:
            sg_proxies = list(sg_proxies or jp_proxies or kr_proxies or gb_proxies or us_proxies)
        if not us_proxies or not sg_proxies:
            raise ValueError("未找到美国或新加坡节点，无法生成 AI 出口")
        if not gemini_leaves:
            # 订阅缺少韩/台/欧等优选区时，退回日美新，避免 render 直接失败
            gemini_leaves = list(jp_proxies) + list(us_proxies) + list(sg_proxies)
        raw_extra_gemini = _collect_extra_ai_nodes(data.get("proxies"), group_map, extra_names, AI_GEMINI_REGION_ORDER)
        if not raw_extra_gemini:
            raw_extra_gemini = _collect_extra_ai_nodes(data.get("proxies"), group_map, extra_names, ("SG", "JP", "US"))
        extra_gemini_nodes = filter_unbanned_nodes(
            raw_extra_gemini,
            active_bans,
            keep_fallback=False,
        )
        gemini_proxies = _order_manual_candidates(
            gemini_leaves, gemini_prefer, gemini_order, max_mult=max_ai_mult
        ) + _deprioritize_high_multiplier(
            [n for n in extra_gemini_nodes if n not in gemini_leaves], max_mult=max_ai_mult
        )

        groups = _drop_named_groups(groups, set(EMOJI_REGION_GROUPS))
        group_map = {group["name"]: group for group in groups if isinstance(group, dict) and group.get("name")}

        # AI-MANUAL 是 fallback：固定节点挂掉后 mihomo 自动切到列表下一个存活节点，
        # 列表顺序即兜底优先级——按「用户钉选 → 实测稳定度 → 静态地区」排。
        # 附加机场 AI 地区节点排在主机场节点之后作跨供应商热备，AI-AUTO 垫底作最终兜底。
        manual_prefer = data.get("ai-manual-prefer")
        if not isinstance(manual_prefer, list):
            manual_prefer = []
        manual_order = _load_ai_manual_order(self.paths)
        us_ordered = _order_manual_candidates(us_proxies, manual_prefer, manual_order, max_mult=max_ai_mult)
        sg_ordered = _order_manual_candidates(sg_proxies, manual_prefer, manual_order, max_mult=max_ai_mult)
        manual_leaves: list[str] = []
        for pool in (us_proxies, jp_proxies, sg_proxies, kr_proxies, gb_proxies):
            for node_name in pool:
                if node_name not in manual_leaves:
                    manual_leaves.append(node_name)
        manual_leaves = _prefer_non_iepl_nodes(manual_leaves, _proxy_map(data.get("proxies")))
        extra_manual_nodes = filter_unbanned_nodes(
            _collect_extra_ai_nodes(data.get("proxies"), group_map, extra_names, ("US", "JP", "SG", "KR", "GB")),
            active_bans,
            keep_fallback=False,
        )
        manual_ordered = _order_manual_candidates(manual_leaves, manual_prefer, manual_order, max_mult=max_ai_mult)
        manual_candidates = (
            manual_ordered
            + _deprioritize_high_multiplier(
                [n for n in extra_manual_nodes if n not in manual_ordered], max_mult=max_ai_mult
            )
            + [AI_AUTO_GROUP]
        )

        # 互质错峰测速间隔：避免多个 fallback 组在同一毫秒并发测速同一节点引发队尾超时与误切换
        ai_groups = [
            {"name": AI_US_GROUP, "type": "fallback", "proxies": us_ordered, "url": TEST_URL, "interval": 283},
            {"name": AI_SG_GROUP, "type": "fallback", "proxies": sg_ordered, "url": TEST_URL, "interval": 293},
            {"name": AI_AUTO_GROUP, "type": "fallback", "proxies": [AI_US_GROUP, AI_SG_GROUP], "url": TEST_URL, "interval": 307},
            {"name": AI_MANUAL_GROUP, "type": "fallback", "proxies": manual_candidates, "url": TEST_URL, "interval": 300},
            {
                "name": AI_GEMINI_GROUP,
                "type": "fallback",
                "proxies": gemini_proxies,
                "url": TEST_URL,
                "interval": 313,
            },
        ]

        managed_names = {AI_MANUAL_GROUP, AI_AUTO_GROUP, AI_US_GROUP, AI_SG_GROUP, AI_GEMINI_GROUP}
        filtered_groups = [group for group in groups if not (isinstance(group, dict) and group.get("name") in managed_names)]

        insert_after = None
        for idx, group in enumerate(filtered_groups):
            if isinstance(group, dict) and group.get("name") in {"Auto", "SSRDOG"}:
                insert_after = idx
                break

        if insert_after is None:
            filtered_groups = ai_groups + filtered_groups
        else:
            filtered_groups = filtered_groups[: insert_after + 1] + ai_groups + filtered_groups[insert_after + 1 :]

        ai_rules = [
            # Google / Gemini / Antigravity —— 必须排在 PROCESS-NAME,agy 之前，
            # 否则 agy 全部出站被 AI-MANUAL（美日新）吃掉，Gemini 仍 400。
            # 精确子域不够：agy 实际打 daily-cloudcode-pa.googleapis.com、
            # oauth2.googleapis.com、antigravity.google、antigravity-unleash.goog。
            f"DOMAIN-SUFFIX,google.com,{AI_GEMINI_GROUP}",
            # 拦截 Google Clearcut 客户端遥测大包上传（5 个并发 agy 进程向 play.googleapis.com/log 持续上传
            # 数十 MB 缓冲日志，会彻底吃满阿里云 ECS 1Mbps 出站公网带宽，导致 daily-cloudcode-pa.googleapis.com
            # 的 1.3MB 上下文 POST 请求因上行饥饿超过 60s 被服务端 EOF 掐断；本地 REJECT 零公网带宽消耗）。
            "DOMAIN,play.googleapis.com,REJECT",
            f"DOMAIN-SUFFIX,googleapis.com,{AI_GEMINI_GROUP}",
            f"DOMAIN-SUFFIX,googleusercontent.com,{AI_GEMINI_GROUP}",
            f"DOMAIN-SUFFIX,gstatic.com,{AI_GEMINI_GROUP}",
            f"DOMAIN-SUFFIX,google.dev,{AI_GEMINI_GROUP}",
            f"DOMAIN-SUFFIX,appspot.com,{AI_GEMINI_GROUP}",
            f"DOMAIN-KEYWORD,antigravity,{AI_GEMINI_GROUP}",
            f"DOMAIN-KEYWORD,gemini,{AI_GEMINI_GROUP}",
            f"DOMAIN-SUFFIX,goog,{AI_GEMINI_GROUP}",
            # agy 非 Google 出站（github / playwright CDN 等）仍走通用 AI 出口；
            # 热更新后旧会话的二进制名变成 agy.<timestamp>.old，用正则一并兜底
            f"PROCESS-NAME,agy,{AI_MANUAL_GROUP}",
            f"PROCESS-NAME-REGEX,^agy\\.[0-9]+\\.old,{AI_MANUAL_GROUP}",
            # OpenAI
            f"DOMAIN-SUFFIX,openai.com,{AI_MANUAL_GROUP}",
            f"DOMAIN-SUFFIX,chatgpt.com,{AI_MANUAL_GROUP}",
            f"DOMAIN-SUFFIX,oaistatic.com,{AI_MANUAL_GROUP}",
            f"DOMAIN-SUFFIX,oaiusercontent.com,{AI_MANUAL_GROUP}",
            f"DOMAIN-SUFFIX,sora.com,{AI_MANUAL_GROUP}",
            f"DOMAIN,cdn.auth0.com,{AI_MANUAL_GROUP}",
            # Anthropic
            f"DOMAIN-SUFFIX,anthropic.com,{AI_MANUAL_GROUP}",
            f"DOMAIN-SUFFIX,claude.ai,{AI_MANUAL_GROUP}",
            f"DOMAIN-SUFFIX,claudeusercontent.com,{AI_MANUAL_GROUP}",
            # xAI / Meta AI / Azure OpenAI / GitHub Copilot
            f"DOMAIN-SUFFIX,x.ai,{AI_MANUAL_GROUP}",
            f"DOMAIN-SUFFIX,grok.com,{AI_MANUAL_GROUP}",
            f"DOMAIN-SUFFIX,meta.ai,{AI_MANUAL_GROUP}",
            f"DOMAIN-SUFFIX,llama.com,{AI_MANUAL_GROUP}",
            f"DOMAIN-SUFFIX,meta.com,{AI_MANUAL_GROUP}",
            f"DOMAIN-SUFFIX,openai.azure.com,{AI_MANUAL_GROUP}",
            f"DOMAIN-SUFFIX,githubcopilot.com,{AI_MANUAL_GROUP}",
            # 人机验证与通用 AI 出口保持一致
            f"DOMAIN-SUFFIX,challenges.cloudflare.com,{AI_MANUAL_GROUP}",
        ]
        # 大流量开发下载源与国产大模型 API 直连，避免绕海外节点增高延迟或耗尽代理套餐流量
        bulk_download_direct = [
            "DOMAIN-SUFFIX,pytorch.org,DIRECT",
            "DOMAIN-SUFFIX,pypi.org,DIRECT",
            "DOMAIN-SUFFIX,pythonhosted.org,DIRECT",
            "DOMAIN-SUFFIX,npmjs.org,DIRECT",
            "DOMAIN-SUFFIX,npmmirror.com,DIRECT",
            "DOMAIN-SUFFIX,moonshot.cn,DIRECT",
            "DOMAIN-SUFFIX,minimaxi.com,DIRECT",
            "DOMAIN-SUFFIX,minimax.io,DIRECT",
            "DOMAIN-SUFFIX,deepseek.com,DIRECT",
            "DOMAIN-SUFFIX,dashscope.aliyuncs.com,DIRECT",
        ]
        mainland_direct = ["GEOIP,CN,DIRECT,no-resolve"]
        rules = data.get("rules") or []

        from ..services.rules import load_custom_rules

        match_grp = _match_group_name(rules)
        user_custom_rules = [
            entry.format_mihomo_rule(match_group=match_grp)
            for entry in load_custom_rules(self.paths)
        ]

        # 订阅自带或历史遗留的有害/失效规则，重渲染时移除
        stale_rules = {
            # Cursor 后端国内无法直连，删除后回落 MATCH 走代理
            "DOMAIN-SUFFIX,cursor.sh,DIRECT",
            # 被墙域名直连导致浏览器安全浏览反复超时，删除后回落 MATCH 走代理
            "DOMAIN,safebrowsing.googleapis.com,DIRECT",
            # 裸 GEOIP 位于注入规则之前会强制域名真实解析（首连延迟 + DNS 泄漏），
            # 末尾 no-resolve 版本已兜底
            "GEOIP,CN,DIRECT",
        }

        clean_rules = [
            _normalize_ip_cidr_rule(rule)
            for rule in rules
            if rule not in user_custom_rules
            and rule not in ai_rules
            and rule not in bulk_download_direct
            and not _is_ai_conflict_rule(rule)
            and rule not in stale_rules
            and rule not in mainland_direct
            and rule != CHINAMAX_RULE
        ]

        # 用户自定义规则置顶，随后是 AI 与下载直连规则：确保不被任何订阅规则遮蔽
        front_rules = user_custom_rules + [
            r for r in (ai_rules + bulk_download_direct) if r not in user_custom_rules
        ]

        match_index = None
        for idx, rule in enumerate(clean_rules):
            if isinstance(rule, str) and rule.startswith("MATCH,"):
                match_index = idx
                break

        tail_rules = [CHINAMAX_RULE] + mainland_direct
        if match_index is None:
            clean_rules = front_rules + clean_rules + tail_rules
        else:
            clean_rules = front_rules + clean_rules[:match_index] + tail_rules + clean_rules[match_index:]

        group_map = {
            group["name"]: group
            for group in filtered_groups
            if isinstance(group, dict) and group.get("name")
        }
        _reshape_match_country_groups(data, filtered_groups, group_map)
        _sanitize_auto_groups(filtered_groups)

        data["proxy-groups"] = filtered_groups
        data["rules"] = clean_rules

        rendered = yaml.safe_dump(data, allow_unicode=True, sort_keys=False, default_flow_style=False)
        target_path.parent.mkdir(parents=True, exist_ok=True)
        # 内容未变化时不重写也不留快照，避免定时任务把快照历史掏空
        if target_path.exists() and target_path.read_text(encoding="utf-8") == rendered:
            return target_path
        # 覆盖运行配置前自动留快照，供 cproxy rollback 回滚
        snapshot_file(self.paths, target_path, "runtime")
        tmp_path = target_path.with_suffix(".tmp")
        tmp_path.write_text(rendered, encoding="utf-8")
        os.replace(tmp_path, target_path)
        return target_path
