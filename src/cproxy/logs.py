from __future__ import annotations

import time
from pathlib import Path


def read_recent_lines(path: Path, lines: int) -> list[str]:
    content = path.read_text(encoding="utf-8").splitlines()
    return content[-lines:] if lines > 0 else content


def follow_lines(path: Path):
    with path.open("r", encoding="utf-8") as handle:
        handle.seek(0, 2)
        while True:
            line = handle.readline()
            if line:
                yield line.rstrip("\n")
                continue
            time.sleep(0.2)
