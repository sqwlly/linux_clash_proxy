from __future__ import annotations

import os
import time
from pathlib import Path


def read_recent_lines(path: Path, lines: int) -> list[str]:
    if lines <= 0:
        return []
    content = path.read_text(encoding="utf-8", errors="replace").splitlines()
    return content[-lines:]


def follow_lines(path: Path):
    handle = path.open("r", encoding="utf-8", errors="replace")
    try:
        inode = os.fstat(handle.fileno()).st_ino
        handle.seek(0, os.SEEK_END)
        while True:
            line = handle.readline()
            if line:
                yield line.rstrip("\r\n")
                continue
            try:
                stat = path.stat()
            except OSError:
                time.sleep(0.2)
                continue
            if inode and stat.st_ino and stat.st_ino != inode:
                handle.close()
                handle = path.open("r", encoding="utf-8", errors="replace")
                inode = os.fstat(handle.fileno()).st_ino
                continue
            if stat.st_size < handle.tell():
                handle.seek(0)
                continue
            time.sleep(0.2)
    finally:
        handle.close()
