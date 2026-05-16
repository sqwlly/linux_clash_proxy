from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from typing import TextIO

try:
    from tqdm import tqdm
except ImportError:  # pragma: no cover - exercised only on incomplete runtime installs
    tqdm = None


RESET = "\033[0m"
BOLD_CYAN = "\033[1;36m"
GREEN = "\033[0;32m"
YELLOW = "\033[1;33m"
COLORS = {
    "cyan": BOLD_CYAN,
    "green": GREEN,
    "yellow": YELLOW,
}


def color_enabled(stream: TextIO) -> bool:
    mode = os.environ.get("CPROXY_COLOR")
    if mode == "always":
        return True
    if mode == "never":
        return False
    if os.environ.get("FORCE_COLOR") == "1":
        return True
    if os.environ.get("NO_COLOR"):
        return False
    if mode == "auto":
        return bool(getattr(stream, "isatty", lambda: False)())
    return True


def _paint(value: str, color: str, enabled: bool) -> str:
    if not enabled:
        return value
    return f"{color}{value}{RESET}"


def style_text(value: str, color: str, stream: TextIO | None = None) -> str:
    target_stream = stream or sys.stderr
    return _paint(value, COLORS[color], color_enabled(target_stream))


if tqdm is not None:

    class _ProgressTqdm(tqdm):
        def __init__(self, *args, status_width: int, status_color: str = "", reset: str = "", **kwargs):
            self._status_width = status_width
            self._status_color = status_color
            self._reset = reset
            super().__init__(*args, **kwargs)

        @property
        def format_dict(self):
            data = super().format_dict
            total = int(data.get("total") or self.total or 1)
            current = int(data.get("n") or self.n)
            percent = min(100, int(current * 100 / total))
            status = f"{percent}% {current}/{total}".ljust(self._status_width)
            if self._status_color:
                status = f"{self._status_color}{status}{self._reset}"
            data["status"] = status
            return data

else:
    _ProgressTqdm = None


@dataclass
class ProgressBar:
    total: int
    label: str
    enabled: bool = True
    width: int = 12
    count_width: int | None = None
    stream: TextIO | None = None

    def __post_init__(self) -> None:
        self.total = max(1, int(self.total))
        self.width = max(8, int(self.width))
        self.count_width = max(1, int(self.count_width or len(str(self.total))))
        if self.stream is None:
            self.stream = sys.stderr
        self._current = 0
        self._bar = None
        if self.enabled:
            if _ProgressTqdm is None:
                raise RuntimeError("缺少 tqdm 依赖，请先安装项目依赖或运行: python3 -m pip install tqdm")
            use_color = color_enabled(self.stream)
            status_width = len("100% ") + self.count_width + 1 + self.count_width
            self._bar = _ProgressTqdm(
                total=self.total,
                desc=_paint(self.label, BOLD_CYAN, use_color),
                ncols=None,
                bar_format=(
                    f"{{desc}} |{_paint(f'{{bar:{self.width}}}', GREEN, use_color)}| "
                    "{status}"
                ),
                file=self.stream,
                leave=True,
                dynamic_ncols=False,
                ascii=False,
                mininterval=0,
                miniters=1,
                status_width=status_width,
                status_color=YELLOW if use_color else "",
                reset=RESET,
            )

    def update(self, completed: int, detail: str = "") -> None:
        if self._bar is None:
            return

        completed = max(0, min(int(completed), self.total))
        delta = completed - self._current
        if delta > 0:
            self._bar.update(delta)
            self._current = completed
        else:
            self._bar.refresh()

    def finish(self, detail: str = "") -> None:
        if self._bar is None:
            return

        self.update(self.total, detail)
        self._bar.close()
