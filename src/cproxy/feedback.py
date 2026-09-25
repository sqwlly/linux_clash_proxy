"""CLI 进行中的即时反馈：stderr 旋转指示（Spinner）与单行进度（ProgressLine）。

输出分工沿用 cli_render 的约定：数据/版面走 stdout，进行中的反馈一律走 stderr。
- `--raw` / `--json` 模式由调用方传 ``enabled=False`` 整体关闭；
- 非 TTY（管道、journal、pytest 捕获）自动降级：Spinner 静默，ProgressLine 只保留
  阶段事件行，不产生逐帧输出；
- TTY 下所有写入都以 ``\\r\\x1b[K`` 开头（回行首并清到行尾），退出时擦除活行，
  不污染后续版面。

本模块刻意不 import cli_render（cli_render → output → services.probe → feedback 会成环），
颜色门控只取环境变量（CPROXY_COLOR / FORCE_COLOR / NO_COLOR），不读配置文件。
"""

from __future__ import annotations

import itertools
import os
import sys
import threading
import time

from .textwidth import display_width

_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
_FRAME_INTERVAL = 0.08
_CLEAR_LINE = "\r\x1b[K"
_ANSI_CYAN = "\x1b[36m"
_ANSI_RESET = "\x1b[0m"


def _stderr_color_enabled() -> bool:
    mode = os.environ.get("CPROXY_COLOR", "").strip().lower()
    if mode == "always" or os.environ.get("FORCE_COLOR") == "1":
        return True
    if mode == "never" or os.environ.get("NO_COLOR"):
        return False
    return sys.stderr.isatty()


def _terminal_columns(stream: object) -> int:
    try:
        return os.get_terminal_size(getattr(stream, "fileno")()).columns
    except (OSError, ValueError, AttributeError):
        return 80


def _truncate(text: str, max_width: int) -> str:
    if max_width < 1 or display_width(text) <= max_width:
        return text
    result: list[str] = []
    width = 0
    for char in text:
        char_width = 2 if display_width(char) == 2 else 1
        if width + char_width > max_width - 1:
            break
        result.append(char)
        width += char_width
    return "".join(result) + "…"


def _live_capable(stream: object) -> bool:
    isatty = getattr(stream, "isatty", None)
    if not (callable(isatty) and isatty()):
        return False
    # dumb 终端不支持回车擦行的光标控制，降级为静态输出
    return os.environ.get("TERM", "") != "dumb"


class Spinner:
    """未知时长阻塞操作的即时反馈。

    TTY 下在 stderr 以单行动画展示「帧 + 消息 + 已耗时」，退出 with 块时自动擦除；
    非 TTY 或 ``enabled=False`` 时完全静默。``delay`` 用于快慢不定的操作：操作在
    delay 秒内完成则动画从未出现，避免一闪而过的视觉噪声。

    约束：with 块内不能有其他终台输出（print/ProgressLine 活行），否则动画行会与
    正文互相覆盖——把渲染放在 with 块之外。
    """

    def __init__(
        self,
        message: str,
        *,
        enabled: bool = True,
        delay: float = 0.0,
        stream: object | None = None,
    ) -> None:
        self._message = message
        self._stream = stream if stream is not None else sys.stderr
        self._live = bool(enabled and _live_capable(self._stream))
        self._delay = max(0.0, delay)
        self._color = _stderr_color_enabled()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._started = time.monotonic()
        self._drew = False

    def __enter__(self) -> "Spinner":
        if self._live:
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join()
        if self._live and self._drew:
            self._stream.write(_CLEAR_LINE)
            self._stream.flush()

    def update(self, message: str) -> None:
        """切换阶段文案（如「正在下载订阅…」→「正在重启代理…」）。"""
        with self._lock:
            self._message = message

    def _render(self, frame: str) -> str:
        elapsed = time.monotonic() - self._started
        text = f"{frame} {self._message} ({elapsed:.0f}s)"
        text = _truncate(text, _terminal_columns(self._stream) - 1)
        if self._color:
            return f"{_ANSI_CYAN}{text}{_ANSI_RESET}"
        return text

    def _run(self) -> None:
        if self._delay and self._stop.wait(self._delay):
            return
        for frame in itertools.cycle(_FRAMES):
            with self._lock:
                line = self._render(frame)
            self._stream.write(_CLEAR_LINE + line)
            self._stream.flush()
            self._drew = True
            if self._stop.wait(_FRAME_INTERVAL):
                return


class ProgressLine:
    """有明确进展粒度的单行进度（如探测逐节点）。

    TTY（``live``）：``update`` 反复重写同一行，``commit`` 把当前行定格并换行；
    非 TTY：``update`` 丢弃（避免刷屏），``commit`` 打印普通一行——与既有的
    「探测 | …」「筛选 | …」静态行行为完全一致，journal/测试不受影响。
    """

    def __init__(self, *, enabled: bool = True, stream: object | None = None) -> None:
        self._stream = stream if stream is not None else sys.stderr
        self.live = bool(enabled and _live_capable(self._stream))
        self._color = _stderr_color_enabled()

    def update(self, text: str) -> None:
        if not self.live:
            return
        line = _truncate(str(text), _terminal_columns(self._stream) - 1)
        if self._color:
            line = f"{_ANSI_CYAN}{line}{_ANSI_RESET}"
        self._stream.write(_CLEAR_LINE + line)
        self._stream.flush()

    def commit(self, text: object) -> None:
        if self.live:
            self._stream.write(_CLEAR_LINE)
        self._stream.write(f"{text}\n")
        self._stream.flush()
