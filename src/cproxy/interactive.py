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

_HIGHLIGHT_ON = "\033[7m"
_HIGHLIGHT_OFF = "\033[0m"
_HIDE_CURSOR = "\033[?25l"
_SHOW_CURSOR = "\033[?25h"
_CLEAR_BELOW = "\033[J"


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
) -> str | None:
    """让用户从 `items` 里选一项，返回选中值；取消（q / Esc / Ctrl-C）返回 None。

    `annotations` 给每项挂一段右对齐的附注（如节点延迟），缺项留空。

    `keys` 仅供测试注入按键流；给定时完全不碰终端，因此可在非 TTY 下测试交互逻辑。
    """
    choices = list(items)
    if not choices:
        return None
    index = choices.index(current) if current in choices else 0

    if keys is not None:
        stream = iter(keys)
        return _loop(choices, index, read_key=lambda: next(stream, None), draw=lambda _: None)

    if not interactive_supported():
        raise NotATerminalError("当前不是交互终端")

    fd = sys.stdin.fileno()
    saved = termios.tcgetattr(fd)
    drawn = 0

    def draw(position: int) -> None:
        nonlocal drawn
        drawn = _redraw(choices, position, title, drawn, annotations)

    try:
        tty.setcbreak(fd)
        _write(_HIDE_CURSOR)
        return _loop(choices, index, read_key=_terminal_read_key(fd), draw=draw)
    except KeyboardInterrupt:
        return None
    finally:
        # 第一件事就是还原终端：即便上面任何一步炸了，也不能留下烂终端
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)
        _write(f"{_SHOW_CURSOR}{_CLEAR_BELOW}")


def _loop(choices: list[str], index: int, *, read_key: Callable[[], str | None], draw: Callable[[int], None]) -> str | None:
    """核心循环：读键、移动、确认。与终端和绘制方式解耦，便于测试。"""
    while True:
        draw(index)
        try:
            key = read_key()
        except KeyboardInterrupt:
            return None
        if key is None or key in _CANCEL_KEYS:
            return None
        if key in _UP_KEYS:
            index = (index - 1) % len(choices)
        elif key in _DOWN_KEYS:
            index = (index + 1) % len(choices)
        elif key in _CONFIRM_KEYS:
            return choices[index]


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


def _window(count: int, index: int) -> tuple[int, int]:
    """当前项可见的窗口 [start, end)，让高亮项尽量居中。"""
    if count <= _MAX_VISIBLE:
        return 0, count
    half = _MAX_VISIBLE // 2
    start = max(0, min(index - half, count - _MAX_VISIBLE))
    return start, start + _MAX_VISIBLE


def _redraw(choices: list[str], index: int, title: str, drawn: int, annotations: dict[str, str] | None = None) -> int:
    """重绘列表，返回本次画出的行数（下次据此上移光标）。"""
    start, end = _window(len(choices), index)
    visible = choices[start:end]
    label_width = max(_display_width(name) for name in visible)
    notes = annotations or {}
    note_width = max((_display_width(note) for note in notes.values()), default=0)

    row_width = label_width + 2 + (note_width + 2 if note_width else 0)
    lines = [_section_heading(title)]
    for offset in range(start, end):
        name = choices[offset]
        marker = "❯ " if offset == index else "  "
        text = f"{marker}{name}"
        if note_width:
            text = f"{_pad_right(text, label_width + 2)}  {_pad_left(notes.get(name, ''), note_width)}"
        # 先补齐宽度再上色：反显条要等宽，而 ANSI 转义不能参与宽度计算
        padded = _pad_right(text, row_width)
        lines.append(f"{_HIGHLIGHT_ON}{padded}{_HIGHLIGHT_OFF}" if offset == index else padded)

    text = "\n".join(lines)
    if drawn:
        text = f"\033[{drawn}A{text}"
    _write(f"{text}{_CLEAR_BELOW}\n")
    return len(lines)


def _write(text: str) -> None:
    sys.stdout.write(text)
    sys.stdout.flush()
