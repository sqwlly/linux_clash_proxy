from __future__ import annotations

import sys
import unicodedata
from dataclasses import dataclass
from typing import TextIO

try:
    from tqdm import tqdm
except ImportError:  # pragma: no cover - exercised only on incomplete runtime installs
    tqdm = None


@dataclass
class ProgressBar:
    total: int
    label: str
    enabled: bool = True
    width: int = 18
    label_width: int = 10
    detail_width: int = 28
    stream: TextIO | None = None

    def __post_init__(self) -> None:
        self.total = max(1, int(self.total))
        self.width = max(8, int(self.width))
        self.label_width = max(4, int(self.label_width))
        self.detail_width = max(8, int(self.detail_width))
        if self.stream is None:
            self.stream = sys.stderr
        self._current = 0
        self._bar = None
        if self.enabled:
            if tqdm is None:
                raise RuntimeError("缺少 tqdm 依赖，请先安装项目依赖或运行: python3 -m pip install tqdm")
            self._bar = tqdm(
                total=self.total,
                desc=self._description(""),
                ncols=None,
                bar_format=f"{{desc}}: {{percentage:3.0f}}%|{{bar:{self.width}}}|",
                file=self.stream,
                leave=True,
                dynamic_ncols=False,
                mininterval=0,
                miniters=1,
            )

    def _description(self, detail: str) -> str:
        return f"{fit_display(self.label, self.label_width)} | {fit_display(detail, self.detail_width)}"

    def update(self, completed: int, detail: str = "") -> None:
        if self._bar is None:
            return

        completed = max(0, min(int(completed), self.total))
        self._bar.set_description_str(self._description(detail), refresh=False)
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


def display_width(value: str) -> int:
    width = 0
    for char in value:
        if unicodedata.combining(char):
            continue
        width += 2 if unicodedata.east_asian_width(char) in {"F", "W"} else 1
    return width


def fit_display(value: object, width: int) -> str:
    target_width = max(1, int(width))
    text = str(value)
    if display_width(text) <= target_width:
        return text + " " * (target_width - display_width(text))

    result = ""
    result_width = 0
    suffix = "…"
    suffix_width = display_width(suffix)
    limit = max(0, target_width - suffix_width)
    for char in text:
        char_width = display_width(char)
        if result_width + char_width > limit:
            break
        result += char
        result_width += char_width
    return result + suffix + " " * max(0, target_width - result_width - suffix_width)
