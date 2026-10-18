"""`cproxy prompt` —— 面向 Shell PS1 / Starship / tmux 状态栏的极速状态微组件。

设计约束：
- 绝不阻塞终端回车：API 请求设 150ms 超时上限，超时则回退静态运行配置；
- 自动感知当前 shell 是否已注入 `HTTP_PROXY` / `HTTPS_PROXY` 环境变量（标 `⚡`）。
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from ..backend.api import APIBackend, APIUnavailableError
from ..backend.process import ProcessBackend
from ..backend.runtime import AI_GEMINI_GROUP, AI_MANUAL_GROUP, RuntimeBackend, resolve_ai_leaf
from ..config import AppPaths
from .node_tags import parse_node_tag

PROMPT_FORMATS = ("compact", "full", "tmux")
_PROMPT_API_TIMEOUT = 0.15


@dataclass(frozen=True)
class PromptSnapshot:
    running: bool
    proxy_env_active: bool
    ai_node: str = "-"
    gemini_node: str = "-"

    @property
    def ai_compact(self) -> str:
        if not self.ai_node or self.ai_node == "-":
            return "-"
        return parse_node_tag(self.ai_node).compact_label

    @property
    def gemini_compact(self) -> str:
        if not self.gemini_node or self.gemini_node == "-":
            return "-"
        return parse_node_tag(self.gemini_node).compact_label


def _is_proxy_env_active() -> bool:
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        if os.environ.get(key, "").strip():
            return True
    return False


def collect_prompt_snapshot(paths: AppPaths) -> PromptSnapshot:
    env_active = _is_proxy_env_active()
    running = ProcessBackend(paths).is_running()
    if not running:
        return PromptSnapshot(running=False, proxy_env_active=env_active)

    groups = {}
    try:
        groups = APIBackend(paths).get_groups(request_timeout=_PROMPT_API_TIMEOUT)
    except APIUnavailableError:
        try:
            groups = RuntimeBackend(paths).get_groups()
        except Exception:
            groups = {}

    ai_node = resolve_ai_leaf(groups, AI_MANUAL_GROUP) if AI_MANUAL_GROUP in groups else "-"
    gemini_node = resolve_ai_leaf(groups, AI_GEMINI_GROUP) if AI_GEMINI_GROUP in groups else "-"
    return PromptSnapshot(
        running=True,
        proxy_env_active=env_active,
        ai_node=ai_node or "-",
        gemini_node=gemini_node or "-",
    )


def format_prompt(snapshot: PromptSnapshot, fmt: str = "compact", *, raw: bool = False) -> str:
    if raw:
        state = "RUNNING" if snapshot.running else "STOPPED"
        env_flag = "ENV" if snapshot.proxy_env_active else "NOENV"
        return f"{state}\t{env_flag}\t{snapshot.ai_compact}\t{snapshot.gemini_compact}"

    env_mark = "⚡" if snapshot.proxy_env_active else ""
    if not snapshot.running:
        if fmt == "tmux":
            return f"#[fg=colour240]🌐{env_mark} OFF#[default]"
        return f"[🌐{env_mark} OFF]"

    ai_label = snapshot.ai_compact
    gem_label = snapshot.gemini_compact
    if fmt == "full":
        if gem_label != "-" and gem_label != ai_label:
            return f"[🌐{env_mark} AI:{ai_label} · Gem:{gem_label}]"
        return f"[🌐{env_mark} AI:{ai_label}]"
    if fmt == "tmux":
        return f"#[fg=green]🌐{env_mark} {ai_label}#[default]"
    return f"[🌐{env_mark} {ai_label}]"
