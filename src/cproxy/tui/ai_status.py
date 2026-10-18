"""AI 出口在 TUI 里的展示拼装。

仪表盘（`dash-ai-*`）与 AI 路由面板（`ai-route-*`）展示的是同一组数据，此前各自
写了一套「取组 → 读 delay/alive → 拼标记」的逻辑，两处都**直接读组自身的 delay**，
而 AI-MANUAL/AI-GEMINI 是 fixed fallback 组（mihomo 不做健康检查、`history` 恒空），
于是延迟永远显示成 `─`、健康永远显示成 `?`。

现在解析统一由 :func:`cproxy.backend.runtime.resolve_ai_route` 提供，这里只负责
把结果拼成 Textual 标记串，色板与两个面板既有的配色保持一致。
"""

from __future__ import annotations

from ..backend.runtime import AI_GEMINI_GROUP, AIExit, AIRoute, delay_state
from ..names import normalize_name

_ACCENT = "#f6c177"
_NODE = "#5eead4"
_DIM = "#8b98aa"
_OK = "#a3e635"
_BAD = "#fb7185"
_CHAIN_ROOT = "#7dd3fc"

#: 无数据占位（与两个面板初始值一致）
IDLE = f"[{_DIM}]─[/]"


def alive_markup(alive: bool | None) -> str:
    """三态健康：● 正常 / ○ 异常 / ? mihomo 未上报（不是「异常」）。"""
    if alive is True:
        return f"[{_OK}]●[/]"
    if alive is False:
        return f"[{_BAD}]○[/]"
    return f"[{_DIM}]?[/]"


def delay_text(delay: int | None) -> str:
    """延迟三态：`NNNms` / `超时` / `未测得`。

    `history` 里的 5000 是探测超时哨兵、0 是未测得，都不是真实延迟——照毫秒展示会把
    「超时」渲染成「很快」（CLI 面板用同一套判定，见 `backend.runtime.delay_state`）。
    """
    if not isinstance(delay, int):
        return "未测得"
    return {"ok": f"{delay}ms", "timeout": "超时", "unknown": "未测得"}[delay_state(delay)]


def exit_line(exit_: AIExit) -> str:
    """`<组> → <节点> (<延迟>) <健康>`；节点名与 CLI 面板同样走 `normalize_name`。

    延迟不是 ok 时不附健康标记：`超时 · 正常` 这种组合自相矛盾。
    """
    parts = [f"{exit_.group} → {normalize_name(exit_.node)} ({delay_text(exit_.delay)})"]
    if isinstance(exit_.delay, int) and delay_state(exit_.delay) == "ok":
        parts.append(alive_markup(exit_.alive))
    return " ".join(parts)


def exit_line_or_idle(exit_: AIExit | None, groups: dict) -> str:
    """出口组不在快照里就别拼行——否则会退化成 `AI-US → AI-US (─) ?`。"""
    if exit_ is None or exit_.group not in groups:
        return IDLE
    return exit_line(exit_)


def mode_markup(route: AIRoute) -> str:
    """`自动` 或 `兜底 · 优选池`。与 CLI `mode_label` 语义对齐，避免两处再次漂移。"""
    if route.auto_mode:
        return f"[{_ACCENT}]自动[/]"
    return f"[{_ACCENT}]兜底 · 优选池[/]"


def chain_markup(route: AIRoute) -> str:
    """链路树。手动模式下 `pool` 就是 AI-MANUAL 本身，不再重复画一层。"""
    lines = [f"[{_CHAIN_ROOT}]通用 AI-MANUAL[/]"]
    if route.auto_mode:
        lines.append(f"└─ [{_ACCENT}]AI-AUTO[/]")
        lines.append(f"   └─ [{_NODE}]{route.pool}[/]")
        lines.append(f"      └─ [{_OK}]{normalize_name(route.active.node)}[/]")
    else:
        lines.append(f"└─ [{_OK}]{normalize_name(route.active.node)}[/]")
    if route.gemini is not None:
        lines.append("")
        lines.append(f"[{_CHAIN_ROOT}]Gemini {AI_GEMINI_GROUP}[/]")
        lines.append(f"└─ [{_OK}]{normalize_name(route.gemini.node)}[/]")
    return "\n".join(lines)
