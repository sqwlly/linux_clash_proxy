import asyncio

from textual.app import App, ComposeResult
from textual.widgets import Button, DataTable, Label

from cproxy.backend.models import AIProbeReport, AIProbeResult, ProxyGroup
from cproxy.config import AppPaths
from cproxy.tui.screens import ai_route as ai_route_module
from cproxy.tui.screens.ai_route import AIRouteScreen


class _AIRouteApp(App):
    def __init__(self, paths: AppPaths):
        super().__init__()
        self.paths = paths

    def compose(self) -> ComposeResult:
        yield AIRouteScreen(self.paths)


def _labels(screen: AIRouteScreen) -> dict[str, str]:
    return {
        key: str(screen.query_one(f"#ai-route-{key}", Label).render())
        for key in ("mode", "active", "gemini", "standby", "chain")
    }


def test_ai_route_panel_renders_resolved_exit_for_pinned_node(ai_route_groups, tmp_path):
    """兜底模式钉住真实节点时，出口要显示该节点与**真实延迟**，而不是旧实现的空 `-`。"""
    paths = AppPaths(tmp_path / "config", tmp_path / "data", tmp_path / "state")

    async def run_case():
        app = _AIRouteApp(paths)
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.pause(0.1)
            screen = app.query_one(AIRouteScreen)
            # 直接驱动渲染（不经过 worker）：本屏无定时器，赋值后立即可断言
            screen._apply_route(ai_route_groups, None)
            labels = _labels(screen)

            # 出口是钉住的韩国节点：旧实现在这里显示 `→ -`
            assert "→ -" not in labels["active"]
            assert "AI-MANUAL → 🇰🇷韩国 01 IEPL 1.7X CM (140ms)" in labels["active"]
            # 备用按出口节点地区映射到美国
            assert "AI-US → 🇺🇸美国 01 IEPL 1.7X CM (210ms)" in labels["standby"]
            # Gemini 也拿到真实延迟（其组自身 history 为空）
            assert "AI-GEMINI → 🇰🇷韩国 01 IEPL 1.7X CM (140ms)" in labels["gemini"]
            assert "兜底" in labels["mode"]
            # 链路树不再画空叶子
            assert "通用 AI-MANUAL" in labels["chain"]
            assert "→ -" not in labels["chain"]
            assert "└─ 🇰🇷韩国 01 IEPL 1.7X CM" in labels["chain"]

    asyncio.run(run_case())


def test_ai_route_panel_renders_auto_chain(ai_route_groups, tmp_path):
    """自动模式要多画 AI-AUTO 与池组两层，叶子仍是真实节点。"""
    ai_route_groups["AI-MANUAL"] = ProxyGroup(
        name="AI-MANUAL",
        type="Fallback",
        current="AI-AUTO",
        candidates=["AI-AUTO", "🇰🇷韩国 01 | IEPL 1.7X | CM"],
        alive=True,
        delay=None,
    )
    paths = AppPaths(tmp_path / "config", tmp_path / "data", tmp_path / "state")

    async def run_case():
        app = _AIRouteApp(paths)
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.pause(0.1)
            screen = app.query_one(AIRouteScreen)
            screen._apply_route(ai_route_groups, None)
            labels = _labels(screen)

            assert "自动" in labels["mode"]
            assert "AI-US → 🇺🇸美国 01 IEPL 1.7X CM (210ms)" in labels["active"]
            assert "└─ AI-AUTO" in labels["chain"]
            assert "└─ AI-US" in labels["chain"]
            assert "└─ 🇺🇸美国 01 IEPL 1.7X CM" in labels["chain"]

    asyncio.run(run_case())


def test_ai_route_panel_shows_placeholder_when_groups_missing(tmp_path):
    paths = AppPaths(tmp_path / "config", tmp_path / "data", tmp_path / "state")

    async def run_case():
        app = _AIRouteApp(paths)
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.pause(0.1)
            screen = app.query_one(AIRouteScreen)
            screen._apply_route({}, None)
            labels = _labels(screen)

            assert "尚未配置" in labels["mode"]
            assert "请先生成运行配置" in labels["chain"]

    asyncio.run(run_case())


def test_ai_route_probe_streams_results_per_target(monkeypatch, tmp_path):
    item1 = AIProbeResult(name="ChatGPT Web", url="https://chatgpt.com", ok=True, detail="HTTP 200")
    item2 = AIProbeResult(name="OpenAI API", url="https://api.openai.com/v1/models", ok=False, detail="超时")

    class FakeDiagnostics:
        def __init__(self, paths):
            self.paths = paths

        def run_ai_probe(self, *, on_result=None):
            on_result(item1)
            on_result(item2)
            return AIProbeReport(results=[item1, item2])

    monkeypatch.setattr(ai_route_module, "DiagnosticsService", FakeDiagnostics)
    paths = AppPaths(tmp_path / "config", tmp_path / "data", tmp_path / "state")

    async def run_case():
        app = _AIRouteApp(paths)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(0.1)
            screen = app.query_one(AIRouteScreen)
            probe_table = screen.query_one("#ai-probe-table", DataTable)
            assert probe_table.row_count == 0

            streamed = []
            original = screen._apply_probe_result

            def spy(item):
                original(item)
                streamed.append((item.name, probe_table.row_count))

            screen._apply_probe_result = spy
            screen.action_probe_ai()
            await pilot.pause(0.3)

            # 前两次回调对应流式上表：第一个目标出结果时表里有 1 行，第二个出结果时有 2 行
            assert streamed[:2] == [("ChatGPT Web", 1), ("OpenAI API", 2)]
            assert probe_table.row_count == 2
            assert "1/2 部分正常" in str(screen.query_one("#ai-probe-status").render())
            assert screen.query_one("#btn-ai-probe", Button).disabled is False
            assert screen._probe_running is False

    asyncio.run(run_case())


def test_ai_route_probe_failure_recovers_state(monkeypatch, tmp_path):
    class FakeDiagnostics:
        def __init__(self, paths):
            self.paths = paths

        def run_ai_probe(self, *, on_result=None):
            raise RuntimeError("proxy down")

    monkeypatch.setattr(ai_route_module, "DiagnosticsService", FakeDiagnostics)
    paths = AppPaths(tmp_path / "config", tmp_path / "data", tmp_path / "state")

    async def run_case():
        app = _AIRouteApp(paths)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(0.1)
            screen = app.query_one(AIRouteScreen)
            screen.action_probe_ai()
            await pilot.pause(0.3)

            assert "探测失败" in str(screen.query_one("#ai-probe-status").render())
            assert screen.query_one("#btn-ai-probe", Button).disabled is False
            assert screen._probe_running is False

            screen.action_probe_ai()
            await pilot.pause(0.3)
            assert "探测失败" in str(screen.query_one("#ai-probe-status").render())

    asyncio.run(run_case())
