"""机场订阅负载解析：YAML 全量配置 / Base64 分享链接列表两种形态。

多订阅场景下，非主订阅（subscriptions 列表中的机场）往往只提供 base64
编码的分享链接列表（vless://、hysteria2:// 等），本模块把它们统一转换为
mihomo proxies 列表，并按节点名归纳地区，供 refresh 生成每家机场自己的
地区分组。
"""
from __future__ import annotations

import base64
import binascii
import re
from urllib.parse import parse_qsl, unquote

import yaml

SUPPORTED_SCHEMES = ("vless", "hysteria2", "tuic", "trojan", "ss")

# 订阅分组地区归纳顺序；节点名命中任一模式即归入该地区
SUBSCRIPTION_REGION_ORDER = ("HK", "JP", "KR", "SG", "TW", "US", "OTHER")
REGION_LABELS = {"OTHER": "其他"}
REGION_PATTERNS: dict[str, tuple[str, ...]] = {
    "HK": (r"🇭🇰", r"香港", r"\bHKG?\b", r"\bHK\d"),
    "JP": (r"🇯🇵", r"日本", r"Japan", r"\bJPN?\b", r"\bJP\d"),
    "KR": (r"🇰🇷", r"韩国", r"Korea", r"\bKOR?\b", r"\bKR\b", r"\bKR\d"),
    "SG": (r"🇸🇬", r"新加坡", r"Singapore", r"\bSGP?\b", r"\bSG\d"),
    "TW": (r"🇹🇼", r"台湾", r"Taiwan", r"\bTWN?\b", r"\bTW\d"),
    "US": (r"🇺🇸", r"美国", r"United States", r"\bUSA?\b", r"\bUS\d"),
}


def match_region(proxy_name: str) -> str:
    """按节点名归纳地区，未命中返回 OTHER。"""
    for region in SUBSCRIPTION_REGION_ORDER[:-1]:
        patterns = REGION_PATTERNS.get(region) or ()
        if any(re.search(pat, proxy_name, re.IGNORECASE) for pat in patterns):
            return region
    return "OTHER"


def _split_share_uri(uri: str) -> tuple[str, str, str, str, str, dict[str, str]]:
    """拆分分享链接为 (scheme, userinfo, host, port, 节点名, params)。"""
    scheme, rest = uri.split("://", 1)
    name = ""
    if "#" in rest:
        rest, name = rest.split("#", 1)
        name = unquote(name)
    query = ""
    if "?" in rest:
        rest, query = rest.split("?", 1)
    rest = rest.split("/", 1)[0]
    if "@" not in rest:
        raise ValueError(f"分享链接缺少用户信息: {uri[:48]}")
    userinfo, hostport = rest.rsplit("@", 1)
    host, port = hostport.rsplit(":", 1)
    return scheme, userinfo, host, port, name, dict(parse_qsl(query))


def _parse_vless(uri: str) -> dict:
    _s, userinfo, host, port, _n, q = _split_share_uri(uri)
    security = q.get("security") or "none"
    proxy: dict = {
        "type": "vless",
        "server": host,
        "port": int(port),
        "uuid": unquote(userinfo),
        "udp": True,
    }
    if security in ("tls", "reality"):
        proxy["tls"] = True
        if q.get("sni"):
            proxy["servername"] = q["sni"]
        if q.get("fp"):
            proxy["client-fingerprint"] = q["fp"]
    if security == "reality":
        proxy["reality-opts"] = {
            "public-key": q.get("pbk") or "",
            "short-id": str(q.get("sid") or ""),
        }
    if q.get("flow"):
        proxy["flow"] = q["flow"]
    network = q.get("type") or "tcp"
    if network != "tcp":
        proxy["network"] = network
    if network == "ws":
        proxy["ws-opts"] = {"path": q.get("path") or "/", **({"headers": {"Host": q["host"]}} if q.get("host") else {})}
    elif network == "grpc":
        proxy["grpc-opts"] = {"grpc-service-name": q.get("serviceName") or ""}
    return proxy


def _parse_hysteria2(uri: str) -> dict:
    _s, userinfo, host, port, _n, q = _split_share_uri(uri)
    proxy: dict = {
        "type": "hysteria2",
        "server": host,
        "port": int(port),
        "password": unquote(userinfo),
        "udp": True,
    }
    if q.get("sni"):
        proxy["sni"] = q["sni"]
    if q.get("insecure") in ("1", "true"):
        proxy["skip-cert-verify"] = True
    if q.get("obfs"):
        proxy["obfs"] = q["obfs"]
        if q.get("obfs-password"):
            proxy["obfs-password"] = q["obfs-password"]
    return proxy


def _parse_tuic(uri: str) -> dict:
    _s, userinfo, host, port, _n, q = _split_share_uri(uri)
    uuid_, password = unquote(userinfo).split(":", 1)
    alpn = [item for item in (q.get("alpn") or "").split(",") if item]
    proxy: dict = {
        "type": "tuic",
        "server": host,
        "port": int(port),
        "uuid": uuid_,
        "password": password,
        "congestion-controller": q.get("congestion_control") or "bbr",
        "udp-relay-mode": q.get("udp_relay_mode") or "native",
        "udp": True,
    }
    if alpn:
        proxy["alpn"] = alpn
    if q.get("sni"):
        proxy["sni"] = q["sni"]
    if q.get("insecure") in ("1", "true"):
        proxy["skip-cert-verify"] = True
    return proxy


def _parse_trojan(uri: str) -> dict:
    _s, userinfo, host, port, _n, q = _split_share_uri(uri)
    proxy: dict = {
        "type": "trojan",
        "server": host,
        "port": int(port),
        "password": unquote(userinfo),
        "udp": True,
    }
    if q.get("sni"):
        proxy["sni"] = q["sni"]
    if q.get("allowInsecure") in ("1", "true"):
        proxy["skip-cert-verify"] = True
    if q.get("type") == "ws":
        proxy["network"] = "ws"
        proxy["ws-opts"] = {"path": q.get("path") or "/"}
    return proxy


def _parse_ss(uri: str) -> dict:
    _s, userinfo, host, port, _n, _q = _split_share_uri(uri)
    method, password = unquote(userinfo).split(":", 1)
    return {
        "type": "ss",
        "server": host,
        "port": int(port),
        "cipher": method,
        "password": password,
        "udp": True,
    }


_LINK_PARSERS = {
    "vless": _parse_vless,
    "hysteria2": _parse_hysteria2,
    "tuic": _parse_tuic,
    "trojan": _parse_trojan,
    "ss": _parse_ss,
}


def parse_share_link(uri: str) -> tuple[str, dict] | None:
    """解析单条分享链接为 (节点名, mihomo proxy dict)；未知协议返回 None，
    已知协议但格式非法时抛 ValueError。"""
    scheme = uri.split("://", 1)[0].lower()
    parser = _LINK_PARSERS.get(scheme)
    if parser is None:
        return None
    proxy = parser(uri.strip())
    _s, _u, _h, _p, name, _q = _split_share_uri(uri.strip())
    if not name:
        raise ValueError(f"分享链接缺少节点名: {uri[:48]}")
    return name, proxy


def parse_subscription_payload(raw: bytes) -> dict:
    """解析订阅响应：优先按 Clash/Mihomo YAML 全量配置，失败后按
    base64 分享链接列表解码。返回至少含 proxies 列表的 dict。"""
    text = raw.decode("utf-8", errors="replace").strip()
    data = None
    try:
        loaded = yaml.safe_load(text)
        if isinstance(loaded, dict):
            data = loaded
    except yaml.YAMLError:
        data = None
    if isinstance(data, dict) and data.get("proxies"):
        return data

    compact = "".join(text.split())
    try:
        decoded = base64.b64decode(compact + "=" * (-len(compact) % 4)).decode("utf-8", errors="replace")
    except (binascii.Error, ValueError) as exc:
        raise ValueError("错误: 订阅内容既不是 Clash YAML 也不是 base64 节点列表") from exc
    proxies: list = []
    for line in decoded.splitlines():
        line = line.strip()
        if "://" not in line:
            continue
        try:
            parsed = parse_share_link(line)
        except ValueError:
            # 单条畸形链接只跳过该行，不否定整个订阅
            continue
        if parsed is None:
            continue
        name, proxy = parsed
        proxy["name"] = name
        proxies.append(proxy)
    if not proxies:
        raise ValueError("错误: 订阅 base64 解码后未找到可用节点")
    return {"proxies": proxies}
