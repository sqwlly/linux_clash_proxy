import asyncio

from textual.app import App, ComposeResult
from textual.widgets import Button, DataTable

from cproxy.backend.models import AIProbeReport, AIProbeResult
from cproxy.config import AppPaths
from cproxy.tui.screens import ai_route as ai_route_module
from cproxy.tui.screens.ai_route import AIRouteScreen


class _AIRouteApp(App):
    def __init__(self, paths: AppPaths):
        super().__init__()
        self.paths = paths

    def compose(self) -> ComposeResult:
        yield AIRouteScreen(self.paths)


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
