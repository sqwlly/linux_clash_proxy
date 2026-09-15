"""代理名／节点名的规范化与候选解析。

面板展示与用户输入共用同一套规则：`🇯🇵 Japan` → `Japan`、`A丨B` → `A B`。

放在这一层（而不是 `output.py`）是为了让服务层能用它做候选匹配，又不必反向依赖
CLI 契约层——`output.py` 会顺带拖入 probe 依赖，服务层不该背这个。
"""

from __future__ import annotations


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


def resolve_candidate(target: str, candidates: list[str]) -> str | None:
    """把用户输入解析成候选里的**原始名称**；无法唯一确定时返回 None。

    面板把节点名经 `normalize_name` 去掉了 emoji 与分隔符再展示（`🇯🇵 Japan`
    显示为 `Japan`），用户照着显示输入是自然行为，不该因此失败。所以精确匹配
    之外还要接受规范化后的匹配。

    多义时返回 None 而非猜一个——候选里可能同时存在 `🇯🇵 Japan` 与 `Japan`，
    这时宁可让调用方报错要求精确输入。
    """
    if target in candidates:
        return target

    folded = normalize_name(target)
    hits = [candidate for candidate in candidates if normalize_name(candidate) == folded]
    return hits[0] if len(hits) == 1 else None
