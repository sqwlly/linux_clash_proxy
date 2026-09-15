from __future__ import annotations

import base64
from pathlib import Path

import yaml

from cproxy.config import AppPaths
from cproxy.services.refresh import apply_extra_subscriptions, update_source_from_subscription

# 测试样例均为虚构凭据，非真实订阅
NODELIST_LINKS = "\n".join(
    (
        "vless://11111111-2222-3333-4444-555555555555@hk1.example.com:443?security=reality&pbk=PBK&sni=s.example.com#HK-1",
        "vless://11111111-2222-3333-4444-555555555555@jp1.example.com:443?security=reality&pbk=PBK&sni=s.example.com#JP-1",
        "hysteria2://passfake@us1.example.com:20272/?sni=us1.example.com#US1-HY2",
        "trojan://passfake@panel.example.com:443#剩余流量：100 GB",
    )
)


def make_paths(tmp_path: Path) -> AppPaths:
    return AppPaths(config_dir=tmp_path / "config", data_dir=tmp_path / "data", state_dir=tmp_path / "state")


def write_config(paths: AppPaths, config: dict) -> None:
    paths.config_dir.mkdir(parents=True, exist_ok=True)
    with (paths.config_dir / "config.yaml").open("w", encoding="utf-8") as fh:
        yaml.safe_dump(config, fh, allow_unicode=True, sort_keys=False)


def read_config(paths: AppPaths) -> dict:
    return yaml.safe_load((paths.config_dir / "config.yaml").read_text(encoding="utf-8"))


def nodelist_raw() -> bytes:
    return base64.b64encode(NODELIST_LINKS.encode("utf-8"))


def patch_download(monkeypatch, payloads: dict[str, bytes]):
    def _download(paths, url, timeout=20):
        if url not in payloads:
            raise OSError(f"unexpected url: {url}")
        return payloads[url], None

    monkeypatch.setattr("cproxy.services.refresh._download_subscription", _download)


def base_config() -> dict:
    return {
        "mixed-port": 7890,
        "proxies": [
            {"name": "🇭🇰香港 01", "type": "ss", "server": "1.2.3.4", "port": 8388, "cipher": "aes-128-gcm", "password": "x"},
        ],
        "proxy-groups": [
            {"name": "CyberGuard", "type": "select", "proxies": ["🇭🇰香港 01"]},
        ],
        "rules": ["MATCH,CyberGuard"],
        "subscription-url": "https://example.com/primary",
        "subscriptions": [{"name": "Mitce", "url": "https://example.com/mitce"}],
    }


def test_apply_adds_prefixed_nodes_and_region_groups(tmp_path, monkeypatch):
    paths = make_paths(tmp_path)
    write_config(paths, base_config())
    patch_download(monkeypatch, {"https://example.com/mitce": nodelist_raw()})

    results = apply_extra_subscriptions(paths)

    assert [(r.name, r.status) for r in results] == [("Mitce", "已更新")]
    config = read_config(paths)
    names = [p["name"] for p in config["proxies"]]
    assert names[:1] == ["🇭🇰香港 01"], "主订阅节点不受影响"
    assert "Mitce HK-1" in names and "Mitce US1-HY2" in names
    assert not any("剩余流量" in n for n in names), "面板信息节点应被过滤"

    groups = {g["name"]: g for g in config["proxy-groups"]}
    assert groups["CyberGuard"]["proxies"] == ["🇭🇰香港 01"], "主订阅分组不受影响"
    assert groups["Mitce"]["type"] == "select"
    assert groups["Mitce"]["proxies"] == ["Mitce-HK", "Mitce-JP", "Mitce-US"]
    assert groups["Mitce-HK"]["type"] == "url-test"
    assert groups["Mitce-HK"]["proxies"] == ["Mitce HK-1"]
    assert groups["Mitce-US"]["proxies"] == ["Mitce US1-HY2"]
    assert groups["Mitce-HK"]["url"] == "https://cp.cloudflare.com/generate_204"


def test_apply_is_idempotent(tmp_path, monkeypatch):
    paths = make_paths(tmp_path)
    write_config(paths, base_config())
    patch_download(monkeypatch, {"https://example.com/mitce": nodelist_raw()})

    apply_extra_subscriptions(paths)
    apply_extra_subscriptions(paths)

    config = read_config(paths)
    names = [p["name"] for p in config["proxies"]]
    assert names.count("Mitce HK-1") == 1
    mitce_groups = [g["name"] for g in config["proxy-groups"] if g["name"] == "Mitce" or g["name"].startswith("Mitce-")]
    assert len(mitce_groups) == 4, "Mitce + HK/JP/US 三个地区组，不应重复"


def test_apply_failure_keeps_previous_nodes(tmp_path, monkeypatch):
    paths = make_paths(tmp_path)
    write_config(paths, base_config())
    patch_download(monkeypatch, {"https://example.com/mitce": nodelist_raw()})
    apply_extra_subscriptions(paths)

    def _broken(paths, url, timeout=20):
        raise OSError("connection refused")

    monkeypatch.setattr("cproxy.services.refresh._download_subscription", _broken)
    results = apply_extra_subscriptions(paths)

    assert [(r.name, r.status) for r in results] == [("Mitce", "失败")]
    config = read_config(paths)
    names = [p["name"] for p in config["proxies"]]
    assert "Mitce HK-1" in names, "下载失败应保留上一轮节点"
    group_names = [g["name"] for g in config["proxy-groups"]]
    assert "Mitce" in group_names and "Mitce-HK" in group_names


def test_first_refresh_keeps_master_entry_in_primary_group(tmp_path, monkeypatch):
    """回归：首次 refresh 时本地还没有机场分组，若先做主订阅合并，
    主组里手工挂的 "Mitce" 入口会因分组尚不存在而被丢弃。生产顺序是
    先 apply_extra_subscriptions 落地分组，再 update_source_from_subscription
    合并主订阅，入口与分组都应保留。"""
    paths = make_paths(tmp_path)
    write_config(paths, base_config())
    patch_download(
        monkeypatch,
        {
            "https://example.com/mitce": nodelist_raw(),
            "https://example.com/primary": yaml.safe_dump(
                {
                    "proxies": [
                        {"name": "🇭🇰香港 01", "type": "ss", "server": "1.2.3.4", "port": 8388,
                         "cipher": "aes-128-gcm", "password": "new"},
                    ],
                    "proxy-groups": [
                        {"name": "CyberGuard", "type": "select", "proxies": ["自动选择", "🇭🇰香港 01"]},
                        {"name": "自动选择", "type": "url-test", "proxies": ["🇭🇰香港 01"]},
                    ],
                    "rules": ["MATCH,CyberGuard"],
                }
            ).encode("utf-8"),
        },
    )
    # 模拟用户把机场入口挂进主组（分组尚不存在）
    config = base_config()
    config["proxy-groups"][0]["proxies"].append("Mitce")
    write_config(paths, config)

    apply_extra_subscriptions(paths)
    update_source_from_subscription(paths, "https://example.com/primary")

    merged = read_config(paths)
    cyber = [g for g in merged["proxy-groups"] if g["name"] == "CyberGuard"][0]
    assert "Mitce" in cyber["proxies"], "主组里的机场入口应保留"
    group_names = [g["name"] for g in merged["proxy-groups"]]
    assert "Mitce" in group_names and "Mitce-HK" in group_names, "机场分组应保留"
    assert "Mitce HK-1" in [p["name"] for p in merged["proxies"]], "机场节点应保留"
    assert merged["subscription-url"] == "https://example.com/primary"


def test_apply_without_subscriptions_is_noop(tmp_path):
    paths = make_paths(tmp_path)
    config = base_config()
    config.pop("subscriptions")
    write_config(paths, config)

    assert apply_extra_subscriptions(paths) == []
    assert read_config(paths) == config


def test_subscription_url_yaml_also_supported(tmp_path, monkeypatch):
    """附加订阅返回 Clash YAML（只取其 proxies）同样可并入。"""
    paths = make_paths(tmp_path)
    write_config(paths, base_config())
    payload = yaml.safe_dump(
        {
            "proxies": [
                {"name": "SG-1", "type": "ss", "server": "5.6.7.8", "port": 8388, "cipher": "aes-128-gcm", "password": "y"},
            ],
            "rules": ["MATCH,PROXY"],
        }
    ).encode("utf-8")
    monkeypatch.setattr(
        "cproxy.services.refresh._download_subscription",
        lambda paths, url, timeout=20: (payload, None),
    )

    results = apply_extra_subscriptions(paths)

    assert [(r.name, r.status) for r in results] == [("Mitce", "已更新")]
    config = read_config(paths)
    assert "Mitce SG-1" in [p["name"] for p in config["proxies"]]
    assert config["rules"] == ["MATCH,CyberGuard"], "附加订阅不应覆盖本地规则"
