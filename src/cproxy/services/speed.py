"""实时网速：对 mihomo 全局累计流量计数器做差分，得到瞬时上下行速率。

mihomo ``GET /connections`` 返回自进程启动以来的 ``downloadTotal`` / ``uploadTotal``
累计字节；两次采样差分除以时间间隔即得速率。计数器在进程重启后会归零，负差分用
``max(0, Δ)`` 钳住，避免把重启误报成负速率。
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class TrafficRate:
    upload_bps: float
    download_bps: float
    interval: float


def format_rate(bps: float) -> str:
    """把字节/秒格式化为易读速率（B/KB/MB/GB per s，1024 进制）。"""
    value = float(bps)
    for unit in ("B/s", "KB/s", "MB/s", "GB/s"):
        if value < 1024 or unit == "GB/s":
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB/s"


def fetch_total_traffic(api: Any) -> tuple[int, int]:
    """读 mihomo /connections 的全局累计计数，返回 (上传字节, 下载字节)。"""
    data = api.get_connections()
    upload = int(data.get("uploadTotal") or 0)
    download = int(data.get("downloadTotal") or 0)
    return upload, download


def measure_once(api: Any, window: float = 1.0) -> TrafficRate | None:
    """采样两次（间隔 window 秒），返回瞬时速率；API 异常由调用方处理。"""
    meter = RateMeter()
    up, down = fetch_total_traffic(api)
    meter.sample(up, down)
    time.sleep(window)
    up, down = fetch_total_traffic(api)
    return meter.sample(up, down)


class RateMeter:
    """持有上一帧 (时间戳, 上行累计, 下行累计)，``sample`` 返回瞬时速率。

    首次采样无前帧返回 ``None``；两次采样时间戳相同（或计数器回退）时安全返回
    ``None`` / 钳到 0。
    """

    def __init__(self) -> None:
        self._prev: tuple[float, int, int] | None = None

    def sample(self, upload: int, download: int, now: float | None = None) -> TrafficRate | None:
        ts = time.monotonic() if now is None else now
        prev = self._prev
        self._prev = (ts, upload, download)
        if prev is None:
            return None
        prev_ts, prev_up, prev_down = prev
        dt = ts - prev_ts
        if dt <= 0:
            return None
        return TrafficRate(
            upload_bps=max(0, upload - prev_up) / dt,
            download_bps=max(0, download - prev_down) / dt,
            interval=dt,
        )
