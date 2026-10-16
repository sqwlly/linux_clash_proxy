"""实时网速：速率格式化与差分采样。"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
SRC_DIR = ROOT_DIR / "src"
sys.path.insert(0, str(SRC_DIR))

from cproxy.services.speed import RateMeter, format_rate


def test_format_rate_units():
    assert format_rate(0) == "0.0 B/s"
    assert format_rate(512) == "512.0 B/s"
    assert format_rate(1024) == "1.0 KB/s"
    assert format_rate(1024 * 1024) == "1.0 MB/s"
    assert format_rate(1024**3) == "1.0 GB/s"


def test_rate_meter_first_sample_returns_none():
    meter = RateMeter()
    assert meter.sample(100, 200, now=0.0) is None


def test_rate_meter_computes_rate():
    meter = RateMeter()
    meter.sample(100, 200, now=0.0)
    rate = meter.sample(300, 600, now=2.0)
    assert rate is not None
    assert rate.upload_bps == 100.0
    assert rate.download_bps == 200.0


def test_rate_meter_clamps_counter_reset():
    meter = RateMeter()
    meter.sample(1000, 2000, now=0.0)
    rate = meter.sample(50, 80, now=1.0)  # 计数器重启归零
    assert rate is not None
    assert rate.upload_bps == 0.0
    assert rate.download_bps == 0.0


def test_rate_meter_same_timestamp_returns_none():
    meter = RateMeter()
    meter.sample(100, 200, now=1.0)
    assert meter.sample(300, 600, now=1.0) is None
