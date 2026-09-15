"""订阅用量（subscription-userinfo 头）的解析、落盘与 status 展示。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

import yaml

ROOT_DIR = Path(__file__).resolve().parents[1]
SRC_DIR = ROOT_DIR / "src"

FAR_FUTURE_EXPIRE = int((datetime(2099, 1, 1) - datetime(1970, 1, 1)).total_seconds())


def make_paths(tmp_path: Path):
    from cproxy.config import AppPaths

    return AppPaths(config_dir=tmp_path / "config", data_dir=tmp_path / "data", state_dir=tmp_path / "state")


def write_config(paths, config: dict) -> None:
    paths.config_dir.mkdir(parents=True, exist_ok=True)
    with (paths.config_dir / "config.yaml").open("w", encoding="utf-8") as fh:
        yaml.safe_dump(config, fh, allow_unicode=True, sort_keys=False)


# ---------------------------------------------------------------------- 解析


def test_parse_full_header():
    from cproxy.services.subscription_info import parse_userinfo_header

    usage = parse_userinfo_header("upload=100; download=300; total=1024; expire=4102444800")
    assert usage is not None
    assert (usage.upload, usage.download, usage.total, usage.expire) == (100, 300, 1024, 4102444800)
    assert usage.used == 400
    assert usage.remaining == 624
    assert usage.updated_at  # 记录时间总在


def test_parse_tolerates_subset_disorder_and_garbage():
    from cproxy.services.subscription_info import parse_userinfo_header

    # 字段乱序 + 未知键 + 空白
    usage = parse_userinfo_header(" total=2048 ; unknown=x ; download=48 ")
    assert usage is not None
    assert usage.total == 2048 and usage.download == 48
    assert usage.upload is None and usage.expire is None
    assert usage.remaining == 2000
    # 非数字/空串/无有效字段 → None
    assert parse_userinfo_header("upload=abc") is None
    assert parse_userinfo_header("") is None
    assert parse_userinfo_header(None) is None
    # expire=0 视为未设置
    usage = parse_userinfo_header("total=10; expire=0")
    assert usage is not None and usage.expire is None


def test_remaining_clamps_at_zero():
    from cproxy.services.subscription_info import parse_userinfo_header

    usage = parse_userinfo_header("upload=600; download=600; total=1000")
    assert usage is not None
    assert usage.remaining == 0, "超用后剩余按 0 截断，不显示负数"


# ------------------------------------------------------------------ 落盘/读取


def test_record_and_load_roundtrip(tmp_path):
    from cproxy.config import subscription_info_file
    from cproxy.services.subscription_info import load_subscription_info, record_subscription_info

    paths = make_paths(tmp_path)
    assert record_subscription_info(paths, "main", "upload=1; download=2; total=10; expire=4102444800")
    assert record_subscription_info(paths, "Mitce", "upload=3; download=4; total=20")

    entries = load_subscription_info(paths)
    assert entries["main"].total == 10 and entries["main"].expire == 4102444800
    assert entries["Mitce"].remaining == 13

    # 无头/坏头不覆盖既有记录
    assert record_subscription_info(paths, "Mitce", None) is False
    assert record_subscription_info(paths, "Mitce", "garbage") is False
    assert load_subscription_info(paths)["Mitce"].total == 20

    # 原子落盘产物是合法 JSON，且无残留 tmp 文件
    data = json.loads(subscription_info_file(paths).read_text(encoding="utf-8"))
    assert set(data) == {"main", "Mitce"}
    assert list(paths.state_dir.glob("*.tmp")) == []


def test_load_tolerates_missing_or_broken_state(tmp_path):
    from cproxy.config import subscription_info_file
    from cproxy.services.subscription_info import load_subscription_info

    paths = make_paths(tmp_path)
    assert load_subscription_info(paths) == {}  # 文件不存在
    subscription_info_file(paths).parent.mkdir(parents=True, exist_ok=True)
    subscription_info_file(paths).write_text("{half json", encoding="utf-8")
    assert load_subscription_info(paths) == {}  # 损坏不抛错


def test_display_entries_orders_and_filters(tmp_path):
    from cproxy.services.subscription_info import record_subscription_info, display_entries

    paths = make_paths(tmp_path)
    write_config(
        paths,
        {
            "subscription-url": "https://example.com/primary",
            "subscriptions": [{"name": "Mitce", "url": "https://example.com/mitce"}],
        },
    )
    # 陈旧记录（配置里已不存在的机场）不应展示
    record_subscription_info(paths, "main", "upload=0; download=1; total=100")
    record_subscription_info(paths, "Mitce", "upload=0; download=2; total=200")
    record_subscription_info(paths, "Ghost", "upload=0; download=3; total=300")

    entries = display_entries(paths)
    assert [(label, usage.total) for label, usage in entries] == [("主订阅", 100), ("Mitce", 200)]

    # 主订阅未配置时不展示 main
    write_config(paths, {"subscriptions": [{"name": "Mitce", "url": "https://example.com/mitce"}]})
    entries = display_entries(paths)
    assert [label for label, _ in entries] == ["Mitce"]


# ---------------------------------------------------------------------- 展示


def test_format_subscription_usage_colors_and_expiry(monkeypatch):
    from cproxy.cli_render import ANSI_GREEN, ANSI_RED, ANSI_YELLOW, _format_subscription_usage
    from cproxy.services.subscription_info import SubscriptionUsage

    monkeypatch.setenv("CPROXY_COLOR", "always")
    # 到期日由「现在 + 90 天」推导，断言也从同一个值算——写死日期会让这个测试
    # 从写下之日起第 91 天必然失败（原实现写死 2026-12-12，已过期）
    far_expire_moment = datetime.now() + timedelta(days=90)
    far_expire = int(far_expire_moment.timestamp())
    soon_expire = int((datetime.now() + timedelta(days=3)).timestamp())
    past_expire = int((datetime.now() - timedelta(days=2)).timestamp())

    healthy = _format_subscription_usage(
        SubscriptionUsage(upload=10, download=20, total=1000, expire=far_expire, updated_at=datetime.now().isoformat())
    )
    assert ANSI_GREEN in healthy and "剩余 970 B" in healthy and "已用 3.0%" in healthy
    # 90 天超出 30 天提示窗：只给日期，不带「（剩 N 天）」后缀
    assert f"到期 {far_expire_moment.strftime('%Y-%m-%d')}" in healthy and "（剩" not in healthy

    low = _format_subscription_usage(
        SubscriptionUsage(upload=0, download=850, total=1000, expire=soon_expire, updated_at=datetime.now().isoformat())
    )
    assert ANSI_YELLOW in low and ANSI_GREEN not in low and "剩 3 天" in low

    exhausted = _format_subscription_usage(
        SubscriptionUsage(upload=0, download=1000, total=1000, expire=past_expire, updated_at=datetime.now().isoformat())
    )
    assert ANSI_RED in exhausted and "剩余 0 B" in exhausted and "已过期 2 天" in exhausted

    # total 缺失：退化为已用上下行，不带剩余着色
    plain = _format_subscription_usage(SubscriptionUsage(download=5, upload=2, updated_at=datetime.now().isoformat()))
    assert "已用 ↓5 B ↑2 B" in plain

    # 记录超 48h 标注数据截至时间
    stale = _format_subscription_usage(
        SubscriptionUsage(
            upload=0, download=1, total=100, updated_at=(datetime.now() - timedelta(hours=50)).isoformat()
        )
    )
    assert "数据截至" in stale


def test_format_subscription_usage_survives_malformed_values():
    """畸形 expire/updated_at 不得崩掉格式化（status 面板「绝不阻塞」契约）。"""
    from cproxy.cli_render import _format_subscription_usage
    from cproxy.services.subscription_info import SubscriptionUsage

    absurd = _format_subscription_usage(
        SubscriptionUsage(download=1, total=10, expire=99999999999999, updated_at="not-a-time")
    )
    assert "剩余 9 B" in absurd
    assert "到期" not in absurd and "数据截至" not in absurd

    aware = _format_subscription_usage(
        SubscriptionUsage(download=1, total=10, updated_at="2026-09-13T10:00:00+00:00")
    )
    assert "剩余 9 B" in aware  # aware 时间串不抛 TypeError


def _run_status(tmp_path: Path, *args: str) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(SRC_DIR)
    env["HOME"] = str(tmp_path)
    env["CPROXY_COLOR"] = "never"
    return subprocess.run(
        [sys.executable, "-m", "cproxy.cli", "status", *args],
        capture_output=True,
        text=True,
        cwd=ROOT_DIR,
        env=env,
    )


def _write_state_home(tmp_path: Path, data: dict) -> None:
    """写入 CLI 子进程视角（$HOME/.local/state/cproxy）的订阅状态文件。"""
    state_dir = tmp_path / ".local" / "state" / "cproxy"
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "subscription-info.json").write_text(json.dumps(data), encoding="utf-8")


def test_status_shows_subscription_block(tmp_path):
    config_dir = tmp_path / ".config" / "cproxy"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "config.yaml").write_text(
        "mixed-port: 7890\n"
        "subscription-url: https://example.com/primary\n"
        "subscriptions:\n  - {name: Mitce, url: 'https://example.com/mitce'}\n",
        encoding="utf-8",
    )
    expire = int((datetime.now() + timedelta(days=45)).timestamp())
    _write_state_home(
        tmp_path,
        {
            "main": {"upload": 0, "download": 100_000_000_000, "total": 1_000_000_000_000, "expire": expire, "updated_at": datetime.now().isoformat()},
            "Mitce": {"upload": 0, "download": 3_000_000_000, "total": 0, "expire": None, "updated_at": datetime.now().isoformat()},
        },
    )

    result = _run_status(tmp_path)
    assert result.returncode == 0, result.stderr
    out = result.stdout
    assert "订阅" in out
    assert "主订阅" in out and "剩余 838.19 GB" in out and "已用 10.0%" in out
    assert "Mitce" in out and "已用 ↓2.79 GB ↑0 B" in out
    assert "到期" in out

    raw = _run_status(tmp_path, "--raw").stdout
    assert f"订阅 主订阅: upload=0 download=100000000000 total=1000000000000 expire={expire}" in raw
    assert "订阅 Mitce: upload=0 download=3000000000 total=0 expire=None" in raw


# ------------------------------------------------------- refresh 时记录用量


def test_update_source_records_main_usage(tmp_path, monkeypatch):
    from cproxy.services import refresh as refresh_module
    from cproxy.services.refresh import update_source_from_subscription
    from cproxy.services.subscription_info import load_subscription_info

    paths = make_paths(tmp_path)
    write_config(paths, {"mixed-port": 7890, "rules": ["MATCH,PROXY"]})
    payload = yaml.safe_dump({"proxies": [{"name": "N1", "type": "ss", "server": "1.1.1.1", "port": 1, "cipher": "x", "password": "y"}]})
    monkeypatch.setattr(
        refresh_module,
        "_download_subscription",
        lambda paths, url, timeout=20: (payload.encode(), "upload=1; download=2; total=10"),
    )

    update_source_from_subscription(paths, "https://example.com/sub")

    main = load_subscription_info(paths)["main"]
    assert (main.upload, main.download, main.total) == (1, 2, 10)


def test_apply_extra_records_per_subscription(tmp_path, monkeypatch):
    import base64

    from cproxy.services.refresh import apply_extra_subscriptions
    from cproxy.services.subscription_info import load_subscription_info

    paths = make_paths(tmp_path)
    write_config(
        paths,
        {
            "mixed-port": 7890,
            "proxies": [],
            "proxy-groups": [],
            "subscriptions": [{"name": "Mitce", "url": "https://example.com/mitce"}],
        },
    )
    links = "vless://11111111-2222-3333-4444-555555555555@hk1.example.com:443?security=reality&pbk=PBK&sni=s.example.com#HK-1"
    monkeypatch.setattr(
        "cproxy.services.refresh._download_subscription",
        lambda paths, url, timeout=20: (base64.b64encode(links.encode()), "download=4; total=8"),
    )

    results = apply_extra_subscriptions(paths)

    assert [(r.name, r.status) for r in results] == [("Mitce", "已更新")]
    mitce = load_subscription_info(paths)["Mitce"]
    assert (mitce.download, mitce.total, mitce.remaining) == (4, 8, 4)
