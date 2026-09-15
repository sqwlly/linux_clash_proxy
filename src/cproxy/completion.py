"""`cproxy __complete` —— shell 补全的候选来源。

协议：

    cproxy __complete <当前词下标> <词0> <词1> ... <词N>

`词0` 是程序名（`cproxy`），下标指向光标所在的那个词。**下标由 shell 显式给出**
而不是"取最后一项"，是因为 bash 在光标位于空白处时不一定把空词放进 `COMP_WORDS`，
届时 `cproxy switch ` 与 `cproxy switch` 无法区分，补全会答非所问。

唯一的设计约束是**绝不能卡住 <Tab>**：动态取值有超时兜底，任何异常都静默降级为
空候选，退出码恒为 0 —— 补全失败不该干扰用户正常敲命令。
"""

from __future__ import annotations

import os
from functools import lru_cache
from importlib.resources import files
from pathlib import Path

from .config import AppPaths
from .output import (
    _COMMAND_HELP,
    PROFILE_CHOICES,
    STRATEGY_CHOICES,
    TRAFFIC_ACTIONS,
    TRAFFIC_DIMENSIONS,
)
from .services.query import QueryService

# 补全取分组时的 API 超时（秒）。本机 controller 正常时响应在毫秒级，这个值只在
# 它不可达时生效，用来把 <Tab> 的等待钉住。
_API_TIMEOUT_SECONDS = 0.5

# 位置参数补什么：命令 → 逐位置的候选来源（见 `_values_for`）
_POSITIONAL_SOURCES: dict[str, tuple[str, ...]] = {
    "current": ("groups",),
    "list-nodes": ("groups",),
    "test-group": ("groups",),
    "probe-stable-node": ("groups",),
    "switch": ("selector-groups", "nodes"),
    "guard": ("profiles",),
    "incident": ("profiles",),
    "ai-use": ("profiles",),
    "shadow-probe": ("profiles",),
    "traffic": ("traffic-actions",),
}

# `--flag` 取值补什么
_FLAG_SOURCES: dict[str, str] = {
    "--profile": "profiles",
    "--strategy": "strategies",
    "--by": "traffic-dimensions",
    "--group": "groups",
    "--node": "nodes",
}


def run_complete(paths: AppPaths, argv: list[str]) -> int:
    """`cproxy __complete ...` 的入口。永远返回 0。"""
    try:
        point = int(argv[0])
        for candidate in _candidates(paths, list(argv[1:]), point):
            print(candidate)
    except BaseException:
        # 补全出任何问题都只是「这次没有候选」——不能把栈或非 0 退出码甩给 shell
        pass
    return 0


def _candidates(paths: AppPaths, words: list[str], point: int) -> list[str]:
    prefix = words[point] if 0 <= point < len(words) else ""
    # 跳过 words[0]（程序名）
    context = words[1:point] if point > 1 else []

    if not context:
        return _matching(prefix, list(_root_options()) if prefix.startswith("-") else sorted(_COMMAND_HELP))

    command, args = context[0], context[1:]

    if prefix.startswith("-"):
        return _matching(prefix, list(_command_options(command)))

    # 当前词是某个 flag 的取值
    if args and args[-1] in _FLAG_SOURCES:
        return _matching(prefix, _values_for(paths, _FLAG_SOURCES[args[-1]], args[:-1]))

    sources = _POSITIONAL_SOURCES.get(command, ())
    if len(args) < len(sources):
        return _matching(prefix, _values_for(paths, sources[len(args)], args))
    return []


@lru_cache(maxsize=1)
def _options_index() -> tuple[tuple[str, ...], dict[str, tuple[str, ...]]]:
    """(顶层选项, 命令 → 选项)，从 parser 现取，不另维护一份清单。

    只收 `--xxx`：补出 `-h` 这类单字母选项只是噪音。
    """
    from argparse import _SubParsersAction

    from .output import build_root_parser

    root: tuple[str, ...] = ()
    commands: dict[str, tuple[str, ...]] = {}
    for action in build_root_parser()._actions:
        if isinstance(action, _SubParsersAction):
            for name, sub in action.choices.items():
                commands[str(name)] = tuple(
                    string
                    for sub_action in sub._actions
                    for string in sub_action.option_strings
                    if string.startswith("--")
                )
        else:
            root += tuple(string for string in action.option_strings if string.startswith("--"))
    return root, commands


def _root_options() -> tuple[str, ...]:
    return _options_index()[0]


def _command_options(command: str) -> tuple[str, ...]:
    return _options_index()[1].get(command, ())


def _values_for(paths: AppPaths, source: str, prior_args: list[str]) -> list[str]:
    if source == "profiles":
        return list(PROFILE_CHOICES)
    if source == "strategies":
        return list(STRATEGY_CHOICES)
    if source == "traffic-actions":
        return list(TRAFFIC_ACTIONS)
    if source == "traffic-dimensions":
        return list(TRAFFIC_DIMENSIONS)
    if source == "groups":
        return _group_names(paths)
    if source == "selector-groups":
        return _group_names(paths, selectable_only=True)
    if source == "nodes":
        return _node_names(paths, _group_from(prior_args))
    return []


def _group_names(paths: AppPaths, *, selectable_only: bool = False) -> list[str]:
    """代理组名。`selectable_only` 时只给能手动切换的（与 switch 的校验口径一致）。"""
    groups = QueryService(paths).list_groups(request_timeout=_API_TIMEOUT_SECONDS)
    if selectable_only:
        groups = [group for group in groups if str(group.type).lower() in {"selector", "select"}]
    return [group.name for group in groups]


def _node_names(paths: AppPaths, group_name: str | None) -> list[str]:
    groups = QueryService(paths).list_groups(request_timeout=_API_TIMEOUT_SECONDS)
    if group_name:
        for group in groups:
            if group.name == group_name:
                return list(group.candidates)
        return []
    # 没有分组上下文时汇总全部候选（去重且保持稳定顺序）
    seen: dict[str, None] = {}
    for group in groups:
        for candidate in group.candidates:
            seen.setdefault(candidate, None)
    return list(seen)


def _group_from(args: list[str]) -> str | None:
    """从已输入的参数里推断分组名：`--group X` 优先，否则第一个非选项词。"""
    for index, word in enumerate(args):
        if word == "--group" and index + 1 < len(args):
            return args[index + 1]
    for word in args:
        if not word.startswith("-"):
            return word
    return None


def _matching(prefix: str, items: list[str]) -> list[str]:
    if not prefix:
        return items
    folded = prefix.casefold()
    return [item for item in items if item.casefold().startswith(folded)]


# --- `cproxy completion <shell>`：把补全脚本交给用户 ---

_SCRIPT_FILES = {"bash": "cproxy.bash", "zsh": "_cproxy"}


def _install_target(shell: str) -> Path:
    """该 shell 的标准补全文件位置。

    bash 会主动扫描 `$XDG_DATA_HOME/bash-completion/completions`，装进去即生效；
    zsh 没有免配置的等价目录，只能装到 `~/.zfunc` 并提示用户加 fpath。
    """
    if shell == "bash":
        base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
        return Path(base) / "bash-completion" / "completions" / "cproxy"
    return Path.home() / ".zfunc" / "_cproxy"


def run_completion(shell: str, *, install: bool) -> int:
    """打印补全脚本；`--install` 时写入标准位置。"""
    script = (files(__package__) / "data" / "completion" / _SCRIPT_FILES[shell]).read_text(encoding="utf-8")

    if not install:
        print(script, end="")
        return 0

    target = _install_target(shell)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(script, encoding="utf-8")
    print(f"已安装补全脚本: {target}")

    if shell == "bash":
        print("新开的 bash 会话自动生效；当前会话可先执行: source " + str(target))
    else:
        print("还需让 zsh 找到它，在 ~/.zshrc 加入下列两行后重开终端：")
        print("  fpath=(~/.zfunc $fpath)")
        print("  autoload -Uz compinit && compinit")
    return 0
