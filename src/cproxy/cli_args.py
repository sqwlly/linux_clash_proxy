"""argparse 的行为补丁：中文帮助版式、中文报错、拼错建议。

`output.py` 负责"有哪些命令、怎么描述"，本模块负责"argparse 自己吐出来的那些
英文样板怎么变成中文"。分开是因为前者是 CLI 契约、后者是框架适配。
"""

from __future__ import annotations

import ast
import difflib
import re
import sys
from argparse import ArgumentParser, RawDescriptionHelpFormatter, _SubParsersAction
from typing import NoReturn


class CliHelpFormatter(RawDescriptionHelpFormatter):
    """把 argparse 写死的 `usage:` 前缀换成中文的 `用法:`。

    英文前缀硬编码在 `HelpFormatter._format_usage` 里，只能在这一层替换。
    注意 **`formatter_class` 不会被子解析器继承**，所以 `_add_command()` 注册
    每个子命令时也要显式传入，否则只有顶层 help 是中文。
    """

    def _format_usage(self, usage, actions, groups, prefix):
        return super()._format_usage(usage, actions, groups, "用法: " if prefix is None else prefix)


# argparse 的英文报错模板 → 中文。未登记的新文案会原样透出（不会崩，只是仍为英文）。
_TRANSLATIONS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"^the following arguments are required: (.+)$"), r"缺少必需参数: \1"),
    (re.compile(r"^unrecognized arguments: (.+)$"), r"无法识别的参数: \1"),
    (re.compile(r"^argument (.+?): expected one argument$"), r"参数 \1 需要一个取值"),
    (re.compile(r"^argument (.+?): invalid int value: (.+)$"), r"参数 \1 需要整数，收到: \2"),
    (re.compile(r"^argument (.+?): not allowed with argument (.+)$"), r"参数 \1 不能与 \2 同时使用"),
    (re.compile(r"^argument (.+?): ignored explicit argument (.+)$"), r"参数 \1 不接受取值: \2"),
    (re.compile(r"^expected one argument$"), r"需要提供一个取值"),
    (re.compile(r"^ambiguous option: (.+)$"), r"参数缩写有歧义: \1"),
)

# 匹配 argparse 的两种 invalid choice 文案：
#   invalid choice: 'statu' (choose from 'init', ...)          ← 顶层子命令名
#   argument --profile: invalid choice: 'codexx' (choose ...)  ← 普通 choices 参数
_INVALID_CHOICE_RE = re.compile(r"^(?:argument (?P<label>.+?): )?invalid choice: (?P<value>.+?)(?: \(choose from .*\))?$")

# 建议的相似度阈值：0.6 下 `statu`→`status`、`codexx`→`codex` 命中，而毫不相干的
# 输入不会硬凑出一个建议。
_SUGGEST_CUTOFF = 0.6


def _translate_message(message: str) -> str:
    for pattern, replacement in _TRANSLATIONS:
        if pattern.match(message):
            return pattern.sub(replacement, message)
    return message


def _parse_invalid_choice(message: str) -> tuple[str | None, str] | None:
    """从 invalid choice 文案里取回 (参数名, 用户输入)。取不到返回 None。"""
    match = _INVALID_CHOICE_RE.match(message)
    if match is None:
        return None
    raw = match.group("value")
    try:
        # argparse 用 repr 拼文案，字符串选项会带引号
        value = ast.literal_eval(raw)
    except (ValueError, SyntaxError):
        value = raw
    return match.group("label"), value if isinstance(value, str) else str(value)


def _subcommand_names(parser: ArgumentParser) -> tuple[str, ...]:
    """取该 parser 的子命令名（不是顶层 parser 时返回空）。

    顶层子命令名拼错时，`_check_value` 其实也会被调用（实测确认），但拿到的
    metavar 是 `<命令>`，既不适合展示、也不该被当成普通参数处理；候选在这里现取。
    """
    try:
        for action in parser._actions:
            if isinstance(action, _SubParsersAction):
                return tuple(str(name) for name in action.choices)
    except Exception:
        pass
    return ()


def _invalid_choice_message(label: str | None, value: str, choices: tuple[str, ...]) -> str:
    """生成中文的「取值无效」文案，附最接近的候选。"""
    head = f"参数 {label} 取值无效: {value}" if label else f"无效的命令: {value}"
    try:
        matches = difflib.get_close_matches(value, list(choices), n=1, cutoff=_SUGGEST_CUTOFF)
    except Exception:
        matches = []  # 建议是锦上添花，算不出来也要正常报错
    if matches:
        return f"{head}\n提示: 您是不是想输入 {matches[0]}?"
    if label:
        return f"{head}（可用值: {', '.join(choices)}）"
    # 命令名不凑建议时不再罗列全部命令——`cproxy --help` 里的分组清单更好读
    return f"{head}（运行 'cproxy --help' 查看全部命令）"


class CliArgumentParser(ArgumentParser):
    """在 argparse 默认行为上加两件事：中文报错、拼错时给建议。

    - `_check_value`：choices 校验失败时记下 (用户输入, 候选集) 交给 `error()`。
      这是 CPython 的内部钩子；万一日后失效，只是「没有建议」，报错与 exit 2 照旧。
    - `error()`：中文文案 + 中文的 `错误:` 前缀，并沿用 argparse 的 exit 2 契约。
    """

    # 类属性给默认值：`error()` 也可能在从未经过 `_check_value` 的路径上被调用
    # （例如"无法识别的参数"），此时实例属性并不存在。
    _pending_choice: tuple[str, tuple[str, ...]] | None = None

    def _check_value(self, action, value) -> None:
        # 顶层子命令名（`_SubParsersAction`）不记候选：argparse 给它的 metavar 是
        # `<命令>`，若当成普通 choices 参数会把 34 个命令名又罗列一遍——那正是要
        # 消灭的。它的候选改由 `_subcommand_names()` 现取，并可指向 `cproxy --help`。
        if action.choices is not None and not isinstance(action, _SubParsersAction):
            try:
                if value not in action.choices:
                    self._pending_choice = (str(value), tuple(str(item) for item in action.choices))
            except Exception:
                pass
        super()._check_value(action, value)

    def error(self, message: str) -> NoReturn:
        pending, self._pending_choice = self._pending_choice, None
        if "invalid choice" in message:
            parsed = _parse_invalid_choice(message)
            if parsed is not None:
                label, value = parsed
                # 记过候选说明错在普通 choices 参数；没记过则说明错在顶层子命令名
                # ——它也被 `_check_value` 看到过，但被刻意跳过记录（见上），
                # 这里置 label 为 None 走「命令」分支。
                if pending is not None:
                    message = _invalid_choice_message(label, value, pending[1])
                else:
                    choices = _subcommand_names(self)
                    if choices:
                        message = _invalid_choice_message(None, value, choices)
        self.print_usage(sys.stderr)
        self.exit(2, f"{self.prog}: 错误: {_translate_message(message)}\n")
