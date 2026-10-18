"""通用机场节点标签解析引擎（Node Tagging Engine）。

将不同机场风格各异的节点命名（如 ``🇰🇷韩国 01 | IEPL | 1.7X | CM``、
``HK-BGP-01 0.5x``、``[IPLC] SG_02 (2倍率)``、``US-West-03 专线``）
归一化为结构化的 :class:`NodeTag`，供路由编排、模糊搜索与产品化展示复用。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .nodelist import match_region

# 专线/中转/直连等线路类型识别模式
_LINE_TYPE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("iepl", re.compile(r"\bIEPL\b|内网专线", re.IGNORECASE)),
    ("iplc", re.compile(r"\bIPLC\b|深港|沪日|沪美|广港|京德", re.IGNORECASE)),
    ("bgp", re.compile(r"\bBGP\b|三网|Anycast", re.IGNORECASE)),
    ("relay", re.compile(r"\bRelay\b|中转|隧道", re.IGNORECASE)),
    ("direct", re.compile(r"直连|原生|\bDirect\b", re.IGNORECASE)),
)

# 倍率提取：支持 1.7X / 1.7x / x1.7 / X2 / 0.5倍率 / 2.0倍 等主流写法
_MULTIPLIER_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?<![A-Za-z0-9])(\d+(?:\.\d+)?)\s*[xX×](?![A-Za-z0-9])"),
    re.compile(r"(?<![A-Za-z0-9])[xX×]\s*(\d+(?:\.\d+)?)(?![A-Za-z0-9])"),
    re.compile(r"(\d+(?:\.\d+)?)\s*倍(?:率)?"),
)

# 节点序号提取：优先匹配紧跟地区或分隔符后的 1~3 位编号（排除 1X 等倍率数字）
_INDEX_RE = re.compile(
    r"(?:^|[\s|\-丨_#]|[^\x00-\x7F])0*(\d{1,3})(?=$|[\s|\-丨_]|[^\x00-\x7F])"
)

# 运营商入口标识
_CARRIER_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("CM", re.compile(r"\bCM(?:CC)?\b|移动", re.IGNORECASE)),
    ("CT", re.compile(r"\bCT(?:CC)?\b|\bCN2\b|电信", re.IGNORECASE)),
    ("CU", re.compile(r"\bCU(?:CC)?\b|联通", re.IGNORECASE)),
    ("BGP", re.compile(r"\bBGP\b|三网", re.IGNORECASE)),
)

# 用户在 CLI 进行模糊匹配（如 `cproxy switch ai kr01` / `uk` / `hk`）时的地区同义词表
REGION_QUERY_TOKENS: dict[str, tuple[str, ...]] = {
    "HK": ("hk", "hkg", "hongkong", "香港", "港"),
    "JP": ("jp", "jpn", "japan", "日本", "日", "东京", "大阪"),
    "KR": ("kr", "kor", "korea", "韩国", "韩", "首尔"),
    "SG": ("sg", "sgp", "singapore", "新加坡", "狮城", "新"),
    "TW": ("tw", "twn", "taiwan", "台湾", "台", "台北"),
    "US": ("us", "usa", "美国", "美", "硅谷", "洛杉矶", "西雅图"),
    "GB": ("gb", "uk", "britain", "英国", "英", "伦敦"),
    "DE": ("de", "deu", "germany", "德国", "德", "法兰克福"),
    "FR": ("fr", "fra", "france", "法国", "法", "巴黎"),
    "AU": ("au", "aus", "australia", "澳大利亚", "澳洲", "悉尼"),
    "AE": ("ae", "uae", "dubai", "迪拜", "阿联酋"),
    "BR": ("br", "bra", "brazil", "巴西", "圣保罗"),
    "IN": ("in", "ind", "india", "印度", "孟买"),
}

# 反查表：任意简写词 → 标准地区码
TOKEN_TO_REGION: dict[str, str] = {
    alias: code
    for code, aliases in REGION_QUERY_TOKENS.items()
    for alias in aliases
}


@dataclass(frozen=True)
class NodeTag:
    """归一化后的节点结构化标签。"""

    raw_name: str
    region: str
    line_type: str = "standard"
    multiplier: float = 1.0
    index: str | None = None
    carrier: str | None = None

    @property
    def is_dedicated_line(self) -> bool:
        return self.line_type in {"iepl", "iplc"}

    @property
    def is_high_multiplier(self) -> bool:
        return self.multiplier >= 3.0

    @property
    def compact_label(self) -> str:
        """用于 prompt / 状态栏的紧凑标签（如 ``🇰🇷韩国01`` 或 ``🇺🇸US02``）。"""
        flag_map = {
            "HK": "🇭🇰香港",
            "JP": "🇯🇵日本",
            "KR": "🇰🇷韩国",
            "SG": "🇸🇬狮城",
            "TW": "🇹🇼台湾",
            "US": "🇺🇸美国",
            "GB": "🇬🇧英国",
            "DE": "🇩🇪德国",
            "FR": "🇫🇷法国",
            "AU": "🇦🇺澳洲",
            "AE": "🇦🇪迪拜",
            "BR": "🇧🇷巴西",
            "IN": "🇮🇳印度",
        }
        base = flag_map.get(self.region)
        if base and self.index:
            return f"{base}{self.index}"
        if base:
            return base
        cleaned = self.raw_name.replace("|", " ").replace("丨", " ").split()
        return "".join(cleaned[:2]) if cleaned else self.raw_name


def parse_node_tag(name: str) -> NodeTag:
    """从任意机场节点名提取结构化标签。"""
    text = str(name or "").strip()
    region = match_region(text)

    line_type = "standard"
    for kind, pattern in _LINE_TYPE_PATTERNS:
        if pattern.search(text):
            line_type = kind
            break

    multiplier = 1.0
    text_without_mult = text
    for pattern in _MULTIPLIER_PATTERNS:
        match = pattern.search(text)
        if match:
            try:
                val = float(match.group(1))
                if 0.0 < val <= 100.0:
                    multiplier = val
                    text_without_mult = text[: match.start()] + " " + text[match.end() :]
                    break
            except ValueError:
                pass

    index: str | None = None
    idx_match = _INDEX_RE.search(text_without_mult)
    if idx_match:
        num = int(idx_match.group(1))
        index = f"{num:02d}" if num < 100 else str(num)

    carrier: str | None = None
    for code, pattern in _CARRIER_PATTERNS:
        if pattern.search(text):
            carrier = code
            break

    return NodeTag(
        raw_name=text,
        region=region,
        line_type=line_type,
        multiplier=multiplier,
        index=index,
        carrier=carrier,
    )
