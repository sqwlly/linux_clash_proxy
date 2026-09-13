from __future__ import annotations

import base64

import pytest

from cproxy.services.nodelist import match_region, parse_share_link, parse_subscription_payload

# 测试样例均为虚构凭据，非真实订阅
VLESS_URI = (
    "vless://11111111-2222-3333-4444-555555555555@hk1.example.com:10126"
    "?type=grpc&encryption=none&security=reality&sni=s0.example.com&fp=chrome"
    "&pbk=PBKFAKE&sid=686c0ef0&serviceName=update&mode=gun&flow=#HK-1"
)
HY2_URI = "hysteria2://passfake@hk2.example.com:20272/?sni=hk2.example.com#HK2-HY2"
TUIC_URI = (
    "tuic://11111111-2222-3333-4444-555555555555:passfake@tw1.example.com:8080"
    "?congestion_control=bbr&udp_relay_mode=native&alpn=h3&sni=tw1.example.com&insecure=0#TW-1"
)
TROJAN_URI = "trojan://passfake@jp1.example.com:443?sni=jp1.example.com#JP-1"
SS_URI = "ss://aes-128-gcm:passfake@sg1.example.com:8388#SG-1"


def test_parse_vless_reality_grpc():
    name, proxy = parse_share_link(VLESS_URI)
    assert name == "HK-1"
    assert proxy["type"] == "vless"
    assert proxy["server"] == "hk1.example.com"
    assert proxy["port"] == 10126
    assert proxy["uuid"] == "11111111-2222-3333-4444-555555555555"
    assert proxy["tls"] is True
    assert proxy["servername"] == "s0.example.com"
    assert proxy["client-fingerprint"] == "chrome"
    assert proxy["reality-opts"] == {"public-key": "PBKFAKE", "short-id": "686c0ef0"}
    assert proxy["network"] == "grpc"
    assert proxy["grpc-opts"] == {"grpc-service-name": "update"}
    assert "flow" not in proxy, "空 flow 不应写入"


def test_parse_hysteria2_with_trailing_slash_port():
    name, proxy = parse_share_link(HY2_URI)
    assert name == "HK2-HY2"
    assert proxy["type"] == "hysteria2"
    assert proxy["port"] == 20272
    assert proxy["password"] == "passfake"
    assert proxy["sni"] == "hk2.example.com"
    assert "skip-cert-verify" not in proxy


def test_parse_tuic():
    name, proxy = parse_share_link(TUIC_URI)
    assert name == "TW-1"
    assert proxy["type"] == "tuic"
    assert proxy["uuid"] == "11111111-2222-3333-4444-555555555555"
    assert proxy["password"] == "passfake"
    assert proxy["congestion-controller"] == "bbr"
    assert proxy["udp-relay-mode"] == "native"
    assert proxy["alpn"] == ["h3"]


def test_parse_trojan_and_ss():
    name, trojan = parse_share_link(TROJAN_URI)
    assert name == "JP-1"
    assert trojan["type"] == "trojan"
    assert trojan["password"] == "passfake"
    name, ss = parse_share_link(SS_URI)
    assert name == "SG-1"
    assert ss["cipher"] == "aes-128-gcm"


def test_unknown_scheme_returns_none():
    assert parse_share_link("wireguard://xxx#node") is None


def test_share_link_without_name_raises():
    with pytest.raises(ValueError):
        parse_share_link("trojan://passfake@jp1.example.com:443?sni=x")


def test_payload_base64_nodelist():
    links = [VLESS_URI, HY2_URI, TUIC_URI, "ss://IGNORED-unknown://#x"]
    raw = base64.b64encode("\n".join(links).encode("utf-8")).decode("ascii").encode("utf-8")
    data = parse_subscription_payload(raw)
    names = [proxy["name"] for proxy in data["proxies"]]
    assert names == ["HK-1", "HK2-HY2", "TW-1"]


def test_payload_yaml_passthrough():
    import yaml

    payload = {"proxies": [{"name": "n1", "type": "ss", "server": "1.2.3.4", "port": 8388}]}
    raw = yaml.safe_dump(payload).encode("utf-8")
    assert parse_subscription_payload(raw) == payload


def test_payload_invalid_raises():
    with pytest.raises(ValueError):
        parse_subscription_payload(b"this is not a subscription at all")


def test_match_region():
    assert match_region("Mitce HK-1") == "HK"
    assert match_region("Mitce HK2-HY2") == "HK"
    assert match_region("Mitce JP-1") == "JP"
    assert match_region("Mitce KR-1") == "KR"
    assert match_region("Mitce SG3-HY2") == "SG"
    assert match_region("Mitce TW-1") == "TW"
    assert match_region("Mitce US1-HY2") == "US"
    assert match_region("🇯🇵日本 01 | 1X") == "JP"
    assert match_region("Mars-1") == "OTHER"
