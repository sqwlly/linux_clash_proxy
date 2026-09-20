"""交互式列表选择器（纯标准库实现）。

`cproxy switch` 不带参数时用它挑分组与节点。刻意**不依赖 textual**：tui 是可选
依赖，没装它不该让 switch 失效。

终端状态的恢复放在 `finally`：无论正常选中、用户取消、按 Ctrl-C 还是中途抛异常，
都不能把用户的终端留在 cbreak 模式或藏起光标。
"""

from __future__ import annotations

import os
import select
import sys
import termios
import tty
from collections.abc import Callable, Iterable, Sequence

from .cli_render import _display_width, _pad_left, _pad_right, _section_heading

# 列表窗口最多显示的行数，超出的以当前项为中心滚动
_MAX_VISIBLE = 12

# 孤立 Esc 与方向键前缀同为 `\x1b`，靠一个短超时区分（见 `_terminal_read_key`）
_ESCAPE_SEQUENCE_TIMEOUT = 0.05

_UP_KEYS = frozenset({"\x1b[A", "k"})
_DOWN_KEYS = frozenset({"\x1b[B", "j"})
_CONFIRM_KEYS = frozenset({"\r", "\n"})
# `\x03` 是 Ctrl-C 的字节形式：cbreak 保留了 ISIG，正常会走 KeyboardInterrupt，
# 但若终端关掉了 ISIG 就会以字节到达，两路都要当取消处理
_CANCEL_KEYS = frozenset({"\x1b", "q", "\x03"})
# 嵌套选择的「返回上一级」：左方向键与 vim 的 h（与 j/k 配套）
_BACK_KEYS = frozenset({"\x1b[D", "h"})
_BACKSPACE_KEYS = frozenset({"\x7f", "\x08"})

_HIGHLIGHT_ON = "\033[7m"
_HIGHLIGHT_OFF = "\033[0m"
_HIDE_CURSOR = "\033[?25l"
_SHOW_CURSOR = "\033[?25h"
_CLEAR_BELOW = "\033[J"
_ANSI_DIM = "\033[2m"
_ANSI_RESET = "\033[0m"


class NotATerminalError(RuntimeError):
    """当前环境无法交互（非 TTY 或 TERM=dumb）——调用方据此降级。"""


def interactive_supported() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty() and os.environ.get("TERM") != "dumb"


def select_one(
    title: str,
    items: Sequence[str],
    *,
    current: str | None = None,
    keys: Iterable[str] | None = None,
    annotations: dict[str, str] | None = None,
    annotation_styles: dict[str, str] | None = None,
    back_hint: bool = False,
) -> str | None:
    """让用户从 `items` 里选一项，返回选中值；取消（q / Esc / Ctrl-C）返回 None。

    `annotations` 给每项挂一段右对齐的附注（如节点延迟），缺项留空。
    `annotation_styles` 给每项的标注单独上 ANSI 色（如延迟快慢着色），缺项不上色。
    `back_hint` 为 True 时底部提示「返回」而非「取消」，`←` / `h` 与 `q` / `Esc`
    一样结束本次选择（返回 None），供嵌套选择的内层回到上一级。

    `keys` 仅供测试注入按键流；给定时完全不碰终端，因此可在非 TTY 下测试交互逻辑。
    """
    choices = list(items)
    if not choices:
        return None
    index = choices.index(current) if current in choices else 0

    if keys is not None:
        stream = iter(keys)
        return _loop(
            choices, index,
            read_key=lambda: next(stream, None),
            draw=lambda _f, _i, _q: None,
            current=current,
            back_hint=back_hint,
        )

    if not interactive_supported():
        raise NotATerminalError("当前不是交互终端")

    fd = sys.stdin.fileno()
    saved = termios.tcgetattr(fd)
    drawn = 0

    def draw(filtered: list[str], position: int, query: str) -> None:
        nonlocal drawn
        drawn = _redraw(
            filtered, position, title, drawn,
            annotations=annotations,
            annotation_styles=annotation_styles,
            current=current,
            total=len(choices),
            query=query,
            back_hint=back_hint,
        )

    try:
        tty.setcbreak(fd)
        _write(_HIDE_CURSOR)
        return _loop(
            choices, index,
            read_key=_terminal_read_key(fd),
            draw=draw,
            current=current,
            back_hint=back_hint,
        )
    except KeyboardInterrupt:
        return None
    finally:
        # 第一件事就是还原终端：即便上面任何一步炸了，也不能留下烂终端
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)
        # 把这一帧菜单擦掉，光标回到起点——否则下一级 select_one 会画在下面，
        # 上一屏的标题/页脚残留叠在一起（这正是「信息残留、回不去」的观感来源）
        _clear_frame(drawn)
        _write(_SHOW_CURSOR)


def _fuzzy_match(name: str, query: str) -> bool:
    """模糊匹配：query 的每个字符按顺序出现在 name 的小写形式中即匹配。"""
    lower = name.lower()
    pos = 0
    for ch in query.lower():
        found = lower.find(ch, pos)
        if found == -1:
            return False
        pos = found + 1
    return True


def _filter_choices(choices: list[str], query: str) -> list[str]:
    """按搜索词过滤候选列表。"""
    if not query:
        return choices
    return [c for c in choices if _fuzzy_match(c, query)]


def _loop(
    choices: list[str],
    index: int,
    *,
    read_key: Callable[[], str | None],
    draw: Callable[[list[str], int, str], None],
    current: str | None = None,
    back_hint: bool = False,
) -> str | None:
    """核心循环：读键、移动、搜索、确认。与终端和绘制方式解耦，便于测试。"""
    query = ""
    searching = False
    filtered = list(choices)

    while True:
        if filtered:
            index = max(0, min(index, len(filtered) - 1))
        draw(filtered, index, query if searching else "")

        try:
            key = read_key()
        except KeyboardInterrupt:
            return None

        if key is None:
            return None

        if not filtered and key in _UP_KEYS | _DOWN_KEYS | _CONFIRM_KEYS:
            # 空列表：移动/确认无意义，但搜索输入、退格、取消、返回仍要往下走
            continue

        # --- 搜索模式 ---
        if searching:
            if key in _CONFIRM_KEYS:
                if filtered:
                    return filtered[index]
                continue
            if key == "\x1b" or key == "\x03":
                # 退出搜索，恢复完整列表
                searching = False
                query = ""
                filtered = list(choices)
                if current and current in filtered:
                    index = filtered.index(current)
                else:
                    index = 0
                continue
            if key in _BACKSPACE_KEYS:
                if query:
                    query = query[:-1]
                    filtered = _filter_choices(choices, query)
                    index = 0
                else:
                    searching = False
                    filtered = list(choices)
                    if current and current in filtered:
                        index = filtered.index(current)
                    else:
                        index = 0
                continue
            if key == "\x1b[A":
                if filtered:
                    index = (index - 1) % len(filtered)
                continue
            if key == "\x1b[B":
                if filtered:
                    index = (index + 1) % len(filtered)
                continue
            # 可打印字符追加到搜索词
            if len(key) == 1 and key.isprintable():
                query += key
                filtered = _filter_choices(choices, query)
                index = 0
            continue

        # --- 普通模式 ---
        if key in _CANCEL_KEYS:
            return None
        if back_hint and key in _BACK_KEYS:
            return None
        if key in _UP_KEYS:
            index = (index - 1) % len(filtered)
        elif key in _DOWN_KEYS:
            index = (index + 1) % len(filtered)
        elif key in _CONFIRM_KEYS:
            if filtered:
                return filtered[index]
        elif key == "/":
            searching = True
            query = ""


def _terminal_read_key(fd: int) -> Callable[[], str | None]:
    def read() -> str | None:
        data = os.read(fd, 1)
        if not data:
            return None  # EOF（如 Ctrl-D）
        char = data.decode("utf-8", "replace")
        if char != "\x1b":
            return char
        # 必须用带超时的 select：否则单按 Esc 会一直等后续字节而阻塞
        ready, _, _ = select.select([fd], [], [], _ESCAPE_SEQUENCE_TIMEOUT)
        if not ready:
            return char
        return char + os.read(fd, 2).decode("utf-8", "replace")

    return read


def _window(count: int, index: int, limit: int = _MAX_VISIBLE) -> tuple[int, int]:
    """当前项可见的窗口 [start, end)，让高亮项尽量居中。"""
    if limit < 1:
        limit = 1
    if count <= limit:
        return 0, count
    half = limit // 2
    start = max(0, min(index - half, count - limit))
    return start, start + limit


def _visible_capacity(*, extra: int = 0) -> int:
    """列表窗口行数：不超过 `_MAX_VISIBLE`，也不把标题/页脚顶出终端。

    菜单一旦超出终端高度，终端会自己滚动，之后用 `CUU` 回不到真正的起点，
    旧行就会留在屏幕上。所以窗口必须按当前终端行数收一收。
    """
    try:
        rows = os.get_terminal_size().lines
    except OSError:
        return _MAX_VISIBLE
    # 标题 + 底栏 + 上下滚动提示，再加上 extra（搜索栏）
    reserved = 4 + extra
    return max(3, min(_MAX_VISIBLE, rows - reserved))


def _clear_frame(drawn: int) -> None:
    """把光标移回上一帧起点，并清掉它及以下的内容。`drawn=0` 时无操作。"""
    if drawn:
        _write(f"\033[{drawn}A\r{_CLEAR_BELOW}")


def _paint(lines: list[str], drawn: int) -> int:
    """擦掉上一帧再写出新内容，避免短行盖不住长行留下残字。

    清屏与新内容放进同一次 write，减少「先空白再画出」的闪烁。
    """
    prefix = f"\033[{drawn}A\r{_CLEAR_BELOW}" if drawn else ""
    _write(prefix + "\n".join(lines) + "\n")
    return len(lines)


def _redraw(
    choices: list[str],
    index: int,
    title: str,
    drawn: int,
    *,
    annotations: dict[str, str] | None = None,
    annotation_styles: dict[str, str] | None = None,
    current: str | None = None,
    total: int | None = None,
    query: str = "",
    back_hint: bool = False,
) -> int:
    """重绘列表，返回本次画出的行数（下次据此上移光标）。"""
    if not choices:
        # 搜索无结果
        pos_label = f" [0/{total}]" if total else ""
        lines = [_section_heading(f"{title}{pos_label}")]
        lines.append(f"{_ANSI_DIM}  (无匹配项){_ANSI_RESET}")
        if query:
            lines.append(f"  搜索: {query}▏")
        lines.append(_footer_line(searching=bool(query), back_hint=back_hint))
        return _paint(lines, drawn)

    display_total = total if total is not None else len(choices)
    start, end = _window(len(choices), index, limit=_visible_capacity(extra=1 if query else 0))
    visible = choices[start:end]
    notes = annotations or {}
    styles = annotation_styles or {}

    # 当前项标记宽度（✓ 占 1 格 + 1 空格）
    current_marker = "✓ "
    marker_width = _display_width(current_marker)
    label_width = max(_display_width(name) for name in visible) + marker_width
    note_width = max((_display_width(notes.get(name, "")) for name in visible), default=0)

    row_width = label_width + 2 + (note_width + 2 if note_width else 0)

    # 标题行含位置指示
    pos_label = f" [{index + 1}/{len(choices)}]"
    if len(choices) < display_total:
        pos_label = f" [{index + 1}/{len(choices)}｜共{display_total}]"
    lines = [_section_heading(f"{title}{pos_label}")]

    # 滚动提示（上）
    if start > 0:
        lines.append(f"{_ANSI_DIM}  ▲ 还有 {start} 项{_ANSI_RESET}")

    for offset in range(start, end):
        name = choices[offset]
        is_current = (current is not None and name == current)
        cursor = "❯ " if offset == index else "  "
        suffix = current_marker if is_current else " " * marker_width
        text = f"{cursor}{_pad_right(name, label_width - marker_width)}{suffix}"
        if note_width:
            note_text = notes.get(name, "")
            note_style = styles.get(name, "")
            if note_style and note_text:
                colored_note = f"{note_style}{_pad_left(note_text, note_width)}{_ANSI_RESET}"
            else:
                colored_note = _pad_left(note_text, note_width)
            # 先算好无色宽度再拼色：反显条需要等宽
            text = f"{_pad_right(text, label_width + 2)}  {colored_note}"
            padded_plain = (
                _pad_right(
                    f"{cursor}{_pad_right(name, label_width - marker_width)}{suffix}",
                    label_width + 2,
                )
                + "  "
                + _pad_left(note_text, note_width)
            )
            full_width = _display_width(padded_plain)
        else:
            text = _pad_right(text, row_width)
            full_width = row_width

        if offset == index:
            # 反显行：需要在无色文本上算宽度补齐后再上色
            # 但因为注解已经上了色，直接包一层反显即可——终端会叠加
            padded = _pad_right(text, full_width) if not note_width else text
            lines.append(f"{_HIGHLIGHT_ON}{padded}{_HIGHLIGHT_OFF}")
        else:
            lines.append(text)

    # 滚动提示（下）
    remaining_below = len(choices) - end
    if remaining_below > 0:
        lines.append(f"{_ANSI_DIM}  ▼ 还有 {remaining_below} 项{_ANSI_RESET}")

    # 搜索栏
    if query:
        lines.append(f"  搜索: {query}▏")

    # 底部提示
    lines.append(_footer_line(searching=bool(query), back_hint=back_hint))
    return _paint(lines, drawn)


def _footer_line(*, searching: bool = False, back_hint: bool = False) -> str:
    """底部按键提示行。"""
    if searching:
        hints = "↑↓ 移动  Enter 确认  Esc 取消搜索  Backspace 删除"
    elif back_hint:
        hints = "↑↓/jk 移动  Enter 确认  / 搜索  ←/q 返回"
    else:
        hints = "↑↓/jk 移动  Enter 确认  / 搜索  q 取消"
    return f"{_ANSI_DIM}{hints}{_ANSI_RESET}"


def _write(text: str) -> None:
    sys.stdout.write(text)
    sys.stdout.flush()
