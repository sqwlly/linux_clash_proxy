import sys
from pathlib import Path

# 优先使用仓库内 src/ 的 cproxy，避免命中 site-packages 中的过期安装快照
SRC_DIR = Path(__file__).resolve().parents[1] / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

import pytest

from cproxy.backend.models import ProxyGroup

# AI 出口测试现场用的节点名（真实订阅里的写法：国旗 + 竖线分隔）。
AI_KR_NODE = "🇰🇷韩国 01 | IEPL 1.7X | CM"
AI_US_NODE = "🇺🇸美国 01 | IEPL 1.7X | CM"
AI_SG_NODE = "🇸🇬新加坡 01 | 1X"


def _proxy_group(name, type_, now, candidates=(), delay=None, alive=True):
    return ProxyGroup(
        name=name,
        type=type_,
        current=now,
        candidates=list(candidates),
        alive=alive,
        delay=delay,
    )


def _proxy_node(name, delay):
    """真实节点条目：`/proxies` 里恒无 `now`/`all`，只有 history 与 alive。"""
    return _proxy_group(name, "Vless", "-", (), delay=delay)


def build_ai_route_groups() -> dict:
    """兜底模式（AI-MANUAL 钉住韩国**节点**）的 AI 出口现场。

    AI-MANUAL/AI-GEMINI 是 fixed fallback，mihomo 不做健康检查、`history` 恒空，
    所以它们自身 `delay=None`；真实延迟只能从出口节点条目上取——这正是解析层要处理的
    不变式，CLI（`ai-status`/`status`）与 TUI（仪表盘/AI 路由面板）的测试共用这份现场。
    """
    return {
        "AI-MANUAL": _proxy_group("AI-MANUAL", "Fallback", AI_KR_NODE, ["AI-AUTO", AI_KR_NODE, AI_US_NODE]),
        "AI-AUTO": _proxy_group("AI-AUTO", "Fallback", "AI-US", ["AI-US", "AI-SG"], delay=820),
        "AI-US": _proxy_group("AI-US", "Fallback", AI_US_NODE, [AI_US_NODE], delay=210),
        "AI-SG": _proxy_group("AI-SG", "Fallback", AI_SG_NODE, [AI_SG_NODE], delay=330),
        "AI-GEMINI": _proxy_group("AI-GEMINI", "Fallback", AI_KR_NODE, [AI_KR_NODE]),
        AI_KR_NODE: _proxy_node(AI_KR_NODE, 140),
        AI_US_NODE: _proxy_node(AI_US_NODE, 210),
        AI_SG_NODE: _proxy_node(AI_SG_NODE, 330),
    }


@pytest.fixture(autouse=True)
def isolate_xdg_environment(monkeypatch):
    for name in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME"):
        monkeypatch.delenv(name, raising=False)
    for name in ("NO_PROXY", "no_proxy"):
        monkeypatch.setenv(name, "127.0.0.1,localhost")
    monkeypatch.setenv("CPROXY_NO_SYSTEMD", "1")


@pytest.fixture
def ai_route_groups() -> dict:
    """每次返回新实例——用例会就地改写条目（如把 AI-MANUAL 切到自动池）。"""
    return build_ai_route_groups()
