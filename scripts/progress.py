from __future__ import annotations

import sys
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
    width: int = 24
    stream: TextIO | None = None

    def __post_init__(self) -> None:
        self.total = max(1, int(self.total))
        self.width = max(8, int(self.width))
        if self.stream is None:
            self.stream = sys.stderr
        self._current = 0
        self._bar = None
        if self.enabled:
            if tqdm is None:
                raise RuntimeError("缺少 tqdm 依赖，请先安装项目依赖或运行: python3 -m pip install tqdm")
            self._bar = tqdm(
                total=self.total,
                desc=self.label,
                ncols=None,
                bar_format="{desc}: {percentage:3.0f}%|{bar}| {n_fmt}/{total_fmt}",
                file=self.stream,
                leave=True,
                dynamic_ncols=True,
                mininterval=0,
                miniters=1,
            )

    def update(self, completed: int, detail: str = "") -> None:
        if self._bar is None:
            return

        completed = max(0, min(int(completed), self.total))
        if detail:
            self._bar.set_description_str(f"{self.label} | {detail}", refresh=False)
        else:
            self._bar.set_description_str(self.label, refresh=False)
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
