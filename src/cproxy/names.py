"""代理名／节点名的规范化、组名别名与模糊候选解析。

面板展示与用户输入共用同一套规则：`🇯🇵 Japan` → `Japan`、`A丨B` → `A B`。
同时支持组名简写（如 `ai` → `AI-MANUAL`、`gemini` → `AI-GEMINI`）与
节点模糊简写（如 `kr01` → `🇰🇷韩国 01 | IEPL | 1.7X | CM`）。

放在这一层（而不是 `output.py`）是为了让服务层能用它做候选匹配，又不必反向依赖
CLI 契约层——`output.py` 会顺带拖入 probe 依赖，服务层不该背这个。
"""

from __future__ import annotations

import re
from collections.abc import Iterable

# 常用代理组简写别名（大小写不敏感）
GROUP_ALIASES: dict[str, str] = {
    "ai": "AI-MANUAL",
    "manual": "AI-MANUAL",
    "ai-manual": "AI-MANUAL",
    "ai出口": "AI-MANUAL",
    "ai 出口": "AI-MANUAL",
    "codex": "AI-MANUAL",
    "claude": "AI-MANUAL",
    "chatgpt": "AI-MANUAL",
    "gemini": "AI-GEMINI",
    "ai-gemini": "AI-GEMINI",
    "google": "AI-GEMINI",
    "us": "AI-US",
    "ai-us": "AI-US",
    "sg": "AI-SG",
    "ai-sg": "AI-SG",
    "auto": "AI-AUTO",
    "ai-auto": "AI-AUTO",
    "global": "GLOBAL",
    "全局": "GLOBAL",
}

DEFAULT_GROUP_ALIASES = frozenset({"default", "proxy", "match", "默认", "默认流量"})

# 紧凑输入（如 kr01 / 韩国1 / uk02）拆分为 (字母或汉字前缀, 数字后缀)
_COMPACT_TOKEN_RE = re.compile(r"^([A-Za-z\u4e00-\u9fff]+?)(\d{1,3})$")


def normalize_name(value: object) -> str:
    if value in ("-", None):
        return "-"

    text = str(value).strip()
    parts = text.split(maxsplit=1)
    if len(parts) == 2 and parts[0] and all(not ch.isalnum() for ch in parts[0]):
        text = parts[1].strip()

    text = text.replace("丨", " ")
    text = text.replace("|", " ")
    return " ".join(text.split())


def resolve_group_alias(
    query: str,
    available_groups: Iterable[str],
    *,
    match_group: str | None = None,
) -> str | None:
    """将用户输入的组名或简写别名解析为真实存在的 Mihomo 组名。"""
    needle = str(query or "").strip()
    if not needle:
        return None
    group_list = list(available_groups)
    group_set = set(group_list)
    if needle in group_set:
        return needle

    lower = needle.lower()
    for name in group_list:
        if name.lower() == lower:
            return name

    if lower in DEFAULT_GROUP_ALIASES and match_group and match_group in group_set:
        return match_group

    alias_target = GROUP_ALIASES.get(lower)
    if alias_target and alias_target in group_set:
        return alias_target

    folded = normalize_name(needle).lower()
    folded_hits = [name for name in group_list if normalize_name(name).lower() == folded]
    if len(folded_hits) == 1:
        return folded_hits[0]
    return None


def _split_query_tokens(target: str) -> list[str]:
    """将模糊查询词拆分为原子 token（例如 `kr01 iepl` -> `['kr', '01', 'iepl']`）。"""
    cleaned = target.replace("|", " ").replace("丨", " ").replace("-", " ").replace("_", " ")
    tokens: list[str] = []
    for raw_part in cleaned.split():
        part = raw_part.strip().lower()
        if not part:
            continue
        compact = _COMPACT_TOKEN_RE.match(part)
        if compact and compact.group(1).lower() not in {"x", "s", "v"}:
            tokens.append(compact.group(1).lower())
            tokens.append(compact.group(2))
        else:
            tokens.append(part)
    return tokens


def _candidate_matches_token(candidate: str, folded_lower: str, token: str) -> bool:
    from .services.node_tags import TOKEN_TO_REGION, parse_node_tag

    if token in folded_lower or token in candidate.lower():
        return True
    tag = parse_node_tag(candidate)
    if token.isdigit():
        wanted_idx = f"{int(token):02d}" if int(token) < 100 else str(int(token))
        if tag.index == wanted_idx:
            return True
        return False
    region_code = TOKEN_TO_REGION.get(token)
    if region_code and tag.region == region_code:
        return True
    return False


def match_candidates_fuzzy(target: str, candidates: list[str]) -> list[str]:
    """按地区简写、序号与关键词对候选节点列表做模糊过滤，返回全部匹配项。"""
    needle = str(target or "").strip()
    if not needle:
        return []
    tokens = _split_query_tokens(needle)
    if not tokens:
        return []

    matched: list[str] = []
    for candidate in candidates:
        folded_lower = normalize_name(candidate).lower()
        if all(_candidate_matches_token(candidate, folded_lower, tok) for tok in tokens):
            matched.append(candidate)
    return matched


def resolve_candidate(target: str, candidates: list[str]) -> str | None:
    """把用户输入解析成候选里的**原始名称**；无法唯一确定时返回 None。

    优先级：
    1. 原始名称精确匹配
    2. `normalize_name` 规范化后唯一精确匹配（如 `🇯🇵 Japan` ↔ `Japan`）
    3. 模糊简写唯一匹配（如 `kr01` → `🇰🇷韩国 01 | IEPL | 1.7X | CM`）

    多义时返回 None 而非盲猜，由调用方给出匹配的候选提示。
    """
    if target in candidates:
        return target

    folded = normalize_name(target)
    hits = [candidate for candidate in candidates if normalize_name(candidate) == folded]
    if len(hits) == 1:
        return hits[0]
    if len(hits) > 1:
        return None

    fuzzy_hits = match_candidates_fuzzy(target, candidates)
    return fuzzy_hits[0] if len(fuzzy_hits) == 1 else None
