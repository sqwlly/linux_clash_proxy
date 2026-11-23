"""机场订阅负载解析：YAML 全量配置 / Base64 分享链接列表两种形态。

多订阅场景下，非主订阅（subscriptions 列表中的机场）往往只提供 base64
编码的分享链接列表（vless://、hysteria2:// 等），本模块把它们统一转换为
mihomo proxies 列表，并按节点名归纳地区，供 refresh 生成每家机场自己的
地区分组。
"""
from __future__ import annotations

import base64
import binascii
import json
import re
from urllib.parse import parse_qsl, unquote

import yaml

SUPPORTED_SCHEMES = ("vless", "hysteria2", "hy2", "tuic", "trojan", "ss", "vmess", "anytls")

# 订阅分组地区归纳顺序；节点名命中任一模式即归入该地区
SUBSCRIPTION_REGION_ORDER = (
    "HK", "JP", "KR", "SG", "TW", "US", "AU", "BR", "AE", "DE", "FR", "GB", "IN", "OTHER",
)
REGION_LABELS = {"OTHER": "其他"}
# switch 第二步用中文地区名；不写入 REGION_LABELS，以免附加订阅分组从 Mitce-HK 变成 Mitce-香港
SWITCH_REGION_LABELS = {
    "HK": "香港",
    "JP": "日本",
    "KR": "韩国",
    "SG": "新加坡",
    "TW": "台湾",
    "US": "美国",
    "AU": "澳大利亚",
    "BR": "巴西",
    "AE": "迪拜",
    "DE": "德国",
    "FR": "法国",
    "GB": "英国",
    "IN": "印度",
    "OTHER": "其他",
}
REGION_PATTERNS: dict[str, tuple[str, ...]] = {
    "HK": (
        r"🇭🇰", r"香港", r"Hong\s*Kong",
        r"(?<![A-Za-z0-9])HKG?(?![A-Za-z0-9])", r"(?<![A-Za-z0-9])HK\d",
    ),
    "JP": (
        r"🇯🇵", r"日本", r"东京", r"東京", r"大阪", r"Japan", r"Tokyo", r"Osaka",
        r"(?<![A-Za-z0-9])JPN?(?![A-Za-z0-9])", r"(?<![A-Za-z0-9])JP\d",
    ),
    "KR": (
        r"🇰🇷", r"韩国", r"韓國", r"首尔", r"首爾", r"釜山", r"Korea", r"Seoul",
        r"(?<![A-Za-z0-9])KOR?(?![A-Za-z0-9])", r"(?<![A-Za-z0-9])KR(?![A-Za-z0-9])", r"(?<![A-Za-z0-9])KR\d",
    ),
    "SG": (
        r"🇸🇬", r"新加坡", r"狮城", r"獅城", r"Singapore",
        r"(?<![A-Za-z0-9])SGP?(?![A-Za-z0-9])", r"(?<![A-Za-z0-9])SG\d",
    ),
    "TW": (
        r"🇹🇼", r"台湾", r"台灣", r"台北", r"新北", r"彰化", r"高雄",
        r"Taiwan", r"Taipei", r"Hinet",
        r"(?<![A-Za-z0-9])TWN?(?![A-Za-z0-9])", r"(?<![A-Za-z0-9])TW\d",
    ),
    "US": (
        r"🇺🇸", r"美国", r"美國", r"洛杉矶", r"圣何塞", r"硅谷", r"西雅图", r"纽约", r"芝加哥", r"达拉斯",
        r"United States", r"America", r"Los Angeles", r"San Jose", r"Silicon Valley", r"Seattle",
        r"(?<![A-Za-z0-9])USA?(?![A-Za-z0-9])", r"(?<![A-Za-z0-9])US\d",
        r"(?<![A-Za-z0-9])LAX(?![A-Za-z0-9])", r"(?<![A-Za-z0-9])SJC(?![A-Za-z0-9])", r"(?<![A-Za-z0-9])SEA(?![A-Za-z0-9])",
    ),
    "AU": (
        r"🇦🇺", r"澳大利亚", r"澳洲", r"悉尼", r"雪梨", r"墨尔本",
        r"Australia", r"Sydney",
        r"(?<![A-Za-z0-9])AUS?(?![A-Za-z0-9])", r"(?<![A-Za-z0-9])AU(?![A-Za-z0-9])", r"(?<![A-Za-z0-9])AU\d",
    ),
    "BR": (
        r"🇧🇷", r"巴西", r"圣保罗", r"聖保羅", r"Brazil", r"Sao Paulo",
        r"(?<![A-Za-z0-9])BRA?(?![A-Za-z0-9])", r"(?<![A-Za-z0-9])BR(?![A-Za-z0-9])", r"(?<![A-Za-z0-9])BR\d",
    ),
    "AE": (
        r"🇦🇪", r"迪拜", r"杜拜", r"阿联酋", r"阿聯酋", r"Dubai", r"UAE",
        r"(?<![A-Za-z0-9])AE(?![A-Za-z0-9])",
    ),
    "DE": (
        r"🇩🇪", r"德国", r"德國", r"法兰克福", r"法蘭克福", r"慕尼黑",
        r"Germany", r"Frankfurt",
        r"(?<![A-Za-z0-9])DEU(?![A-Za-z0-9])",
        r"(?-i:(?<![A-Za-z0-9])DE(?![A-Za-z0-9]))",
        r"(?-i:(?<![A-Za-z0-9])DE\d)",
    ),
    "FR": (
        r"🇫🇷", r"法国", r"法國", r"巴黎", r"France", r"Paris",
        r"(?<![A-Za-z0-9])FRA?(?![A-Za-z0-9])", r"(?<![A-Za-z0-9])FR(?![A-Za-z0-9])", r"(?<![A-Za-z0-9])FR\d",
    ),
    "GB": (
        r"🇬🇧", r"英国", r"英國", r"伦敦", r"倫敦",
        r"Britain", r"United Kingdom", r"London", r"England",
        r"(?<![A-Za-z0-9])UK(?![A-Za-z0-9])", r"(?<![A-Za-z0-9])UK\d",
        r"(?<!\d\s)(?<!\d)(?<![A-Za-z0-9])GBR?(?![A-Za-z0-9])", r"(?<![A-Za-z0-9])GB\d",
    ),
    "IN": (
        r"🇮🇳", r"印度", r"孟买", r"India", r"Mumbai",
        r"(?<![A-Za-z0-9])IND(?![A-Za-z0-9])",
        r"(?-i:(?<![A-Za-z0-9])IN(?![A-Za-z0-9]))",
        r"(?-i:(?<![A-Za-z0-9])IN\d)",
    ),
}


PANEL_INFO_NODE_MARKERS: tuple[str, ...] = (
    "剩余流量",
    "套餐到期",
    "过期时间",
    "到期时间",
    "流量重置",
    "下次重置",
    "重置剩余",
    "有效期",
    "官网",
    "官網",
    "网址",
    "網址",
    "公告",
    "expire",
    "traffic",
)


_PANEL_QUOTA_RE = re.compile(
    r"\b\d+(?:\.\d+)?\s*[KMGTPE]i?B\s*(?:[|/]|of)\s*\d+(?:\.\d+)?\s*[KMGTPE]i?B\b",
    re.IGNORECASE,
)


def is_panel_info_node(name: str) -> bool:
    """识别机场面板注入的信息节点（剩余流量/套餐到期/已用流量配额等）。

    这类节点名携带动态数值（如“剩余流量：160.9 GB”或“99.35 GB | 200 GB”），不能当可切换出口。
    """
    if not name:
        return True
    lowered = name.lower()
    if any(marker in lowered for marker in PANEL_INFO_NODE_MARKERS):
        return True
    return bool(_PANEL_QUOTA_RE.search(name))


def match_region(proxy_name: str) -> str:
    """按节点名归纳地区，未命中或面板信息节点返回 OTHER。"""
    if is_panel_info_node(proxy_name):
        return "OTHER"
    for region in SUBSCRIPTION_REGION_ORDER[:-1]:
        patterns = REGION_PATTERNS.get(region) or ()
        if any(re.search(pat, proxy_name, re.IGNORECASE) for pat in patterns):
            return region
    return "OTHER"


_PREFER_REGION_ALIASES: dict[str, str] = {"UK": "GB"}
_KNOWN_PREFER_REGIONS: frozenset[str] = frozenset(REGION_PATTERNS.keys()) | frozenset(_PREFER_REGION_ALIASES.keys())


def normalize_prefer_region_code(token: str) -> str | None:
    """若偏好项为受支持的 2 字母地区码（如 JP / SG / US / UK），返回标准地区码，否则返回 None。"""
    cleaned = str(token or "").strip().upper()
    if cleaned in _KNOWN_PREFER_REGIONS:
        return _PREFER_REGION_ALIASES.get(cleaned, cleaned)
    return None


def prefer_rank_for_node(name: str, prefer: list | tuple | None) -> int:
    """返回节点在用户偏好列表中的最高优先级下标（越小越靠前）；未命中返回 len(prefer)。

    偏好项支持：
    - 精确节点名（如 `"🇯🇵 Japan 01"`）
    - 2 字母地区码（如 `"JP"` / `"SG"` / `"US"` / `"GB"` / `"UK"`）
    """
    if not prefer:
        return 0
    items = [str(item).strip() for item in prefer if item is not None and str(item).strip()]
    if not items:
        return 0
    node_name = str(name or "").strip()
    node_region: str | None = None
    for idx, token in enumerate(items):
        reg_code = normalize_prefer_region_code(token)
        if reg_code is not None:
            if node_region is None:
                node_region = match_region(node_name)
            if node_region == reg_code:
                return idx
        elif node_name == token:
            return idx
    return len(items)


def apply_prefer_order(nodes: list[str], prefer: list | tuple | None) -> list[str]:
    """按用户偏好（精确节点名或地区码）稳定重排节点列表；同级内部保留原有顺序。"""
    if not nodes or not prefer:
        return list(nodes)
    items = [str(item).strip() for item in prefer if item is not None and str(item).strip()]
    if not items:
        return list(nodes)
    return sorted(nodes, key=lambda n: prefer_rank_for_node(str(n), items))


def _parse_host_port(hostport: str, uri: str) -> tuple[str, int]:
    text = hostport.strip()
    if text.startswith("["):
        end_bracket = text.find("]")
        if end_bracket == -1 or end_bracket + 1 >= len(text) or text[end_bracket + 1] != ":":
            raise ValueError(f"分享链接 IPv6 主机或端口非法: {uri[:48]}")
        host = text[1:end_bracket].strip()
        port_str = text[end_bracket + 2 :].strip()
    else:
        if ":" not in text:
            raise ValueError(f"分享链接缺少端口: {uri[:48]}")
        host, port_str = text.rsplit(":", 1)
        host = host.strip()
        port_str = port_str.strip()
    if not host:
        raise ValueError(f"分享链接缺少主机: {uri[:48]}")
    try:
        port = int(port_str)
    except ValueError as exc:
        raise ValueError(f"分享链接端口非法: {uri[:48]}") from exc
    if not (1 <= port <= 65535):
        raise ValueError(f"分享链接端口超出范围 (1-65535): {port}")
    return host, port


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
    host, port = _parse_host_port(hostport, uri)
    return scheme, userinfo, host, str(port), name, dict(parse_qsl(query))


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


def _b64_decode_text(encoded: str) -> str:
    padded = encoded.strip().replace("-", "+").replace("_", "/")
    padded += "=" * (-len(padded) % 4)
    return base64.b64decode(padded).decode("utf-8", errors="replace")


def _parse_ss(uri: str) -> dict:
    _, rest = uri.split("://", 1)
    if "#" in rest:
        rest, _ = rest.split("#", 1)
    if "?" in rest:
        rest, _ = rest.split("?", 1)
    rest = rest.split("/", 1)[0]
    if "@" not in rest:
        # Legacy base64: ss://BASE64(method:password@host:port)#name
        try:
            decoded_rest = _b64_decode_text(unquote(rest))
        except Exception as exc:
            raise ValueError(f"ss 分享链接解码失败: {uri[:48]}") from exc
        if "@" not in decoded_rest:
            raise ValueError(f"ss 分享链接缺少 @: {uri[:48]}")
        userinfo, hostport = decoded_rest.rsplit("@", 1)
    else:
        userinfo, hostport = rest.rsplit("@", 1)
        userinfo = unquote(userinfo)
        if ":" not in userinfo:
            # SIP002 base64 userinfo: ss://BASE64(method:password)@host:port#name
            try:
                userinfo = _b64_decode_text(userinfo)
            except Exception as exc:
                raise ValueError(f"ss 用户信息解码失败: {uri[:48]}") from exc
    if ":" not in userinfo:
        raise ValueError(f"ss 分享链接格式非法: {uri[:48]}")
    method, password = userinfo.split(":", 1)
    host, port = _parse_host_port(hostport, uri)
    return {
        "type": "ss",
        "server": host,
        "port": port,
        "cipher": method,
        "password": password,
        "udp": True,
    }


def _parse_vmess(uri: str) -> tuple[str, dict]:
    _, rest = uri.split("://", 1)
    if "#" in rest:
        rest, fallback_name = rest.split("#", 1)
        fallback_name = unquote(fallback_name)
    else:
        fallback_name = ""
    try:
        payload = json.loads(_b64_decode_text(rest))
    except Exception as exc:
        raise ValueError(f"vmess 链接解码失败: {uri[:48]}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"vmess 负载不是 JSON 对象: {uri[:48]}")
    name = str(payload.get("ps") or fallback_name or "").strip()
    host = str(payload.get("add") or "").strip().strip("[]")
    try:
        port = int(payload.get("port") or 0)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"vmess 端口非法: {uri[:48]}") from exc
    uuid_ = str(payload.get("id") or "").strip()
    if not name or not host or not (1 <= port <= 65535) or not uuid_:
        raise ValueError(f"vmess 缺少必填字段或端口非法: {uri[:48]}")
    try:
        alter_id = int(payload.get("aid") or 0)
    except (TypeError, ValueError):
        alter_id = 0
    proxy: dict = {
        "type": "vmess",
        "server": host,
        "port": port,
        "uuid": uuid_,
        "alterId": alter_id,
        "cipher": str(payload.get("scy") or "auto"),
        "udp": True,
    }
    net = str(payload.get("net") or "tcp").lower()
    if net != "tcp":
        proxy["network"] = net
    if net == "ws":
        ws_opts: dict = {"path": str(payload.get("path") or "/")}
        if payload.get("host"):
            ws_opts["headers"] = {"Host": str(payload["host"])}
        proxy["ws-opts"] = ws_opts
    elif net == "grpc":
        proxy["grpc-opts"] = {"grpc-service-name": str(payload.get("path") or "")}
    tls_val = str(payload.get("tls") or "").lower()
    if tls_val in ("tls", "true", "1"):
        proxy["tls"] = True
        sni = str(payload.get("sni") or payload.get("host") or "").strip()
        if sni:
            proxy["servername"] = sni
    return name, proxy


def _parse_anytls(uri: str) -> dict:
    _s, userinfo, host, port, _n, q = _split_share_uri(uri)
    proxy: dict = {
        "type": "anytls",
        "server": host,
        "port": int(port),
        "password": unquote(userinfo),
        "udp": True,
    }
    if q.get("sni"):
        proxy["sni"] = q["sni"]
    if q.get("insecure") in ("1", "true"):
        proxy["skip-cert-verify"] = True
    return proxy


_LINK_PARSERS = {
    "vless": _parse_vless,
    "hysteria2": _parse_hysteria2,
    "hy2": _parse_hysteria2,
    "tuic": _parse_tuic,
    "trojan": _parse_trojan,
    "ss": _parse_ss,
    "anytls": _parse_anytls,
}


def parse_share_link(uri: str) -> tuple[str, dict] | None:
    """解析单条分享链接为 (节点名, mihomo proxy dict)；未知协议返回 None，
    已知协议但格式非法时抛 ValueError。"""
    cleaned = uri.strip()
    if "://" not in cleaned:
        return None
    scheme = cleaned.split("://", 1)[0].lower()
    if scheme == "vmess":
        return _parse_vmess(cleaned)
    parser = _LINK_PARSERS.get(scheme)
    if parser is None:
        return None
    proxy = parser(cleaned)
    if "#" in cleaned:
        name = unquote(cleaned.split("#", 1)[1]).strip()
    else:
        name = ""
    if not name:
        raise ValueError(f"分享链接缺少节点名: {cleaned[:48]}")
    return name, proxy


def _parse_share_link_lines(content: str) -> list[dict]:
    proxies: list[dict] = []
    for line in content.splitlines():
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
    return proxies


def parse_subscription_payload(raw: bytes) -> dict:
    """解析订阅响应：优先按 Clash/Mihomo YAML 全量配置，失败后按
    base64 / 纯文本分享链接列表解码。返回至少含 proxies 列表的 dict。"""
    text = raw.decode("utf-8", errors="replace").strip()
    data = None
    try:
        loaded = yaml.safe_load(text)
        if isinstance(loaded, dict):
            data = loaded
    except yaml.YAMLError:
        data = None
    if isinstance(data, dict) and "proxies" in data:
        proxies_val = data.get("proxies")
        if not isinstance(proxies_val, list):
            raise ValueError("错误: 订阅配置的 proxies 不是有效的列表")
        if proxies_val:
            return data
        raise ValueError("错误: 订阅配置的 proxies 为空")

    compact = "".join(text.split())
    decoded = ""
    b64_ok = False
    try:
        decoded = _b64_decode_text(compact)
        b64_ok = True
    except (binascii.Error, ValueError):
        b64_ok = False

    proxies = _parse_share_link_lines(decoded) if b64_ok else []
    if not proxies:
        proxies = _parse_share_link_lines(text)
    if not proxies:
        if not b64_ok:
            raise ValueError("错误: 订阅内容不是有效的 Clash YAML 或 base64 节点列表")
        raise ValueError("错误: 订阅内容不是有效的 Clash YAML，且 base64 解码后未找到可用节点")
    return {"proxies": proxies}
