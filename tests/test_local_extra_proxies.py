from __future__ import annotations

import io
from pathlib import Path

import yaml

from cproxy.config import AppPaths
from cproxy.services.refresh import (
    _preserve_local_extra_proxies,
    update_source_from_subscription,
)

VULTR_PROXY = {
    "name": "🇯🇵日本 Vultr-Reality",
    "type": "vless",
    "server": "45.32.47.247",
    "port": 443,
    "uuid": "4db99bbd-7eab-4aa9-91fc-3a356864a9d6",
    "udp": True,
    "tls": True,
    "flow": "xtls-rprx-vision",
    "servername": "www.bing.com",
    "client-fingerprint": "chrome",
    "reality-opts": {"public-key": "PBK", "short-id": "9ed936e9d21e5c01"},
}


def make_paths(tmp_path: Path) -> AppPaths:
    base = tmp_path
    return AppPaths(config_dir=base / "config", data_dir=base / "data", state_dir=base / "state")


def write_config(paths: AppPaths, config: dict) -> None:
    paths.config_dir.mkdir(parents=True, exist_ok=True)
    with (paths.config_dir / "config.yaml").open("w", encoding="utf-8") as fh:
        yaml.safe_dump(config, fh, allow_unicode=True, sort_keys=False)


def subscription_payload() -> dict:
    return {
        "proxies": [
            {"name": "🇭🇰香港 01", "type": "ss", "server": "1.2.3.4", "port": 8388, "cipher": "aes-128-gcm", "password": "x"},
        ],
        "proxy-groups": [
            {"name": "CyberGuard", "type": "select", "proxies": ["🇭🇰香港 01", "自动选择"]},
            {"name": "自动选择", "type": "url-test", "proxies": ["🇭🇰香港 01"]},
        ],
        "rules": ["MATCH,CyberGuard"],
    }


def fake_urlopen(payload: dict):
    raw = yaml.safe_dump(payload).encode("utf-8")

    def _open(request, timeout=10):
        return io.BytesIO(raw)

    return _open


def fake_download(payload: dict):
    """桩掉订阅下载层（_download_subscription），聚焦合并逻辑测试。"""
    raw = yaml.safe_dump(payload).encode("utf-8")

    def _download(paths, url, timeout=10):
        return raw

    return _download


def test_preserve_local_extra_proxies_appends_nodes_and_group_members():
    merged = subscription_payload()
    existing = {
        "proxies": [
            {"name": "🇭🇰香港 01", "type": "ss", "server": "1.2.3.4", "port": 8388},
            VULTR_PROXY,
        ],
        "proxy-groups": [
            {"name": "CyberGuard", "type": "select", "proxies": ["🇭🇰香港 01", "自动选择", "🇯🇵日本 Vultr-Reality"]},
            {"name": "自动选择", "type": "url-test", "proxies": ["🇭🇰香港 01"]},
        ],
    }

    _preserve_local_extra_proxies(merged, existing)

    names = [p["name"] for p in merged["proxies"]]
    assert names == ["🇭🇰香港 01", "🇯🇵日本 Vultr-Reality"]
    cyber = merged["proxy-groups"][0]
    assert cyber["proxies"] == ["🇭🇰香港 01", "自动选择", "🇯🇵日本 Vultr-Reality"]
    auto = merged["proxy-groups"][1]
    assert "🇯🇵日本 Vultr-Reality" not in auto["proxies"], "本地组没有的成员不应被注入"


def test_preserve_keeps_original_group_position():
    merged = subscription_payload()
    existing = {
        "proxies": [
            {"name": "🇭🇰香港 01", "type": "ss", "server": "1.2.3.4", "port": 8388},
            VULTR_PROXY,
        ],
        "proxy-groups": [
            {"name": "CyberGuard", "type": "select", "proxies": ["🇯🇵日本 Vultr-Reality", "🇭🇰香港 01", "自动选择"]},
            {"name": "故障转移", "type": "fallback", "proxies": ["🇯🇵日本 Vultr-Reality", "🇭🇰香港 01"]},
        ],
    }
    merged["proxy-groups"].append({"name": "故障转移", "type": "fallback", "proxies": ["🇭🇰香港 01"]})

    _preserve_local_extra_proxies(merged, existing)

    cyber = merged["proxy-groups"][0]
    assert cyber["proxies"][0] == "🇯🇵日本 Vultr-Reality", "首位成员应保持在首位"
    fallback = merged["proxy-groups"][2]
    assert fallback["proxies"][0] == "🇯🇵日本 Vultr-Reality", "fallback 优先级不应沉底"


def test_preserve_skips_when_no_extra_nodes():
    merged = subscription_payload()
    existing = {
        "proxies": [{"name": "🇭🇰香港 01", "type": "ss", "server": "1.2.3.4", "port": 8388}],
        "proxy-groups": [],
    }
    before = yaml.safe_dump(merged, allow_unicode=True)
    _preserve_local_extra_proxies(merged, existing)
    assert yaml.safe_dump(merged, allow_unicode=True) == before


def test_preserve_drops_stale_panel_info_nodes():
    """面板信息节点名字随流量/到期日变化，不应被当成自建节点累积保留。"""
    merged = subscription_payload()
    existing = {
        "proxies": [
            {"name": "🇭🇰香港 01", "type": "ss", "server": "1.2.3.4", "port": 8388},
            # 上一轮订阅留下的旧信息节点（新订阅里名字已变成“剩余流量：155.3 GB”）
            {"name": "剩余流量：160.9 GB", "type": "trojan", "server": "panel.example.com"},
            {"name": "套餐到期：2026-09-10", "type": "trojan", "server": "panel.example.com"},
            {"name": "Expire: 2026-09-10", "type": "trojan", "server": "panel.example.com"},
        ],
        "proxy-groups": [
            {
                "name": "CyberGuard",
                "type": "select",
                "proxies": ["剩余流量：160.9 GB", "🇭🇰香港 01", "自动选择"],
            },
        ],
    }

    _preserve_local_extra_proxies(merged, existing)

    names = [str(p["name"]) for p in merged["proxies"]]
    assert names == ["🇭🇰香港 01"], "旧信息节点不应以自建节点形式残留"
    cyber = merged["proxy-groups"][0]
    assert "剩余流量：160.9 GB" not in cyber["proxies"], "组内也不应回插旧信息节点"


def test_preserve_local_only_groups_and_references():
    """本地附加分组（多订阅机场组）应整体保留，且主订阅同名组里挂的
    分组引用（如 CyberGuard 里的 "Mitce" 入口）按原位置插回。"""
    merged = subscription_payload()
    existing = {
        "proxies": [
            {"name": "🇭🇰香港 01", "type": "ss", "server": "1.2.3.4", "port": 8388},
            {"name": "Mitce HK-1", "type": "hysteria2", "server": "hk1.example.com", "port": 20272, "password": "x"},
        ],
        "proxy-groups": [
            {
                "name": "CyberGuard",
                "type": "select",
                "proxies": ["🇭🇰香港 01", "自动选择", "Mitce"],
            },
            {"name": "Mitce-HK", "type": "url-test", "proxies": ["Mitce HK-1"],
             "url": "https://cp.cloudflare.com/generate_204", "interval": 300},
            {"name": "Mitce", "type": "select", "proxies": ["Mitce-HK"]},
        ],
    }

    _preserve_local_extra_proxies(merged, existing)

    group_names = [g["name"] for g in merged["proxy-groups"]]
    assert "Mitce" in group_names and "Mitce-HK" in group_names, "本地附加分组应整体保留"
    cyber = merged["proxy-groups"][0]
    assert cyber["proxies"] == ["🇭🇰香港 01", "自动选择", "Mitce"], "主订阅组里的附加入口应按原位置保留"
    names = [p["name"] for p in merged["proxies"]]
    assert "Mitce HK-1" in names, "附加分组引用的节点也应保留"


def test_update_source_keeps_local_extra_proxy(tmp_path):
    paths = make_paths(tmp_path)
    write_config(
        paths,
        {
            "mixed-port": 7890,
            "mode": "rule",
            "proxies": [
                {
                    "name": "🇭🇰香港 01",
                    "type": "ss",
                    "server": "1.2.3.4",
                    "port": 8388,
                    "cipher": "aes-128-gcm",
                    "password": "old",
                },
                VULTR_PROXY,
            ],
            "proxy-groups": [
                {"name": "CyberGuard", "type": "select", "proxies": ["🇭🇰香港 01", "🇯🇵日本 Vultr-Reality"]},
            ],
            "rules": ["MATCH,CyberGuard"],
            "subscription-url": "https://example.com/sub",
        },
    )

    import cproxy.services.refresh as refresh_mod

    original = refresh_mod._download_subscription
    refresh_mod._download_subscription = fake_download(subscription_payload())
    try:
        out = update_source_from_subscription(paths, "https://example.com/sub")
    finally:
        refresh_mod._download_subscription = original

    merged = yaml.safe_load(out.read_text(encoding="utf-8"))
    names = [p["name"] for p in merged["proxies"]]
    assert "🇯🇵日本 Vultr-Reality" in names
    cyber = merged["proxy-groups"][0]
    assert "🇯🇵日本 Vultr-Reality" in cyber["proxies"]
    assert merged["subscription-url"] == "https://example.com/sub"
