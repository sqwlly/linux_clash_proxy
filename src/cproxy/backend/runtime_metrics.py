"""进程运行时指标采集。

纯 ``/proc`` 只读实现,不依赖 ``ps`` / ``ss`` / ``du`` 等外部命令——与
:mod:`cproxy.backend.process` 读取 ``/proc/{pid}/cmdline`` 的做法一致。

全部字段都是 best-effort:任一读取失败即返回 ``None``,由渲染层静默跳过该行,
绝不因为指标不可得而让 ``cproxy status`` 失败(与流量摘要的容错约定一致)。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from ..config import AppPaths, log_file

# /proc/{pid}/stat 的 starttime 以时钟节拍计;Linux 用户态通常为 100。
_DEFAULT_CLK_TCK = 100


@dataclass(frozen=True)
class RuntimeMetrics:
    """某一时刻的进程资源快照;字段为 ``None`` 表示该项不可得。"""

    uptime_seconds: int | None = None
    memory_bytes: int | None = None
    log_bytes: int | None = None


def _clk_tck() -> int:
    try:
        return int(os.sysconf("SC_CLK_TCK"))
    except (ValueError, OSError, AttributeError):
        return _DEFAULT_CLK_TCK


def _read_uptime_seconds(pid: int) -> int | None:
    """进程已运行秒数 = 系统 uptime - 进程启动时刻。"""
    try:
        # errors="replace"：/proc 文本理论上可能含非 UTF-8 字节（comm 字段），
        # 严格解码会抛 UnicodeDecodeError —— 它是 ValueError 子类，不被下面的
        # OSError 捕获，会穿透本模块“绝不阻塞 status”的契约。
        stat_text = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8", errors="replace")
        uptime_text = Path("/proc/uptime").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None

    # comm 字段(第 2 个)可能含空格与括号,因此从最后一个 ')' 之后开始切分。
    # 切分后 fields[0] 是 state(第 3 字段),故 starttime(第 22 字段)位于 fields[19]。
    _, _, tail = stat_text.rpartition(")")
    fields = tail.split()
    if len(fields) <= 19:
        return None
    try:
        start_seconds = int(fields[19]) / _clk_tck()
        boot_uptime = float(uptime_text.split()[0])
    except (ValueError, IndexError):
        return None

    elapsed = int(boot_uptime - start_seconds)
    return max(0, elapsed)


def _read_memory_bytes(pid: int) -> int | None:
    try:
        status_text = Path(f"/proc/{pid}/status").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None

    for line in status_text.splitlines():
        if line.startswith("VmRSS:"):
            parts = line.split()
            if len(parts) >= 2:
                try:
                    return int(parts[1]) * 1024
                except ValueError:
                    return None
            return None
    return None


def _read_log_bytes(paths: AppPaths) -> int | None:
    path = log_file(paths)
    try:
        return path.stat().st_size
    except OSError:
        return None


def collect_runtime_metrics(paths: AppPaths, pid: int | None) -> RuntimeMetrics:
    """采集运行时间 / 内存 / 日志大小。缺失项为 ``None``,不抛异常。"""
    return RuntimeMetrics(
        uptime_seconds=_read_uptime_seconds(pid) if pid else None,
        memory_bytes=_read_memory_bytes(pid) if pid else None,
        log_bytes=_read_log_bytes(paths),
    )
