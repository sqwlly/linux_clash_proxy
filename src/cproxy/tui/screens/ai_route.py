from __future__ import annotations

import threading

from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widget import Widget
from textual.widgets import Button, Label

from ...api import APIUnavailableError
from ...config import AppPaths
from ...diagnostics import run_ai_probe
from ...services.query import QueryService
from ..widgets import NavigationDataTable as DataTable


class AIRouteScreen(Widget):
    BINDINGS = [
        Binding("p", "probe_ai", "探测"),
        Binding("s", "switch_us_sg", "切换"),
        Binding("r", "refresh_data", "刷新"),
    ]

    def __init__(self, paths: AppPaths, **kwargs):
        super().__init__(**kwargs)
        self.paths = paths
        self._probe_running = False

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label("AI 路由", classes="page-title")

            with Horizontal():
                with Vertical(classes="ai-route-panel ai-selector-panel"):
                    yield Label("选择器", classes="ai-route-title")
                    with Horizontal(classes="field-row"):
                        yield Label("模式", classes="label-key")
                        yield Label("─", id="ai-route-mode", classes="metric-value")
                    with Horizontal(classes="field-row"):
                        yield Label("当前", classes="label-key")
                        yield Label("─", id="ai-route-active", classes="metric-value")
                    with Horizontal(classes="field-row"):
                        yield Label("备用", classes="label-key")
                        yield Label("─", id="ai-route-standby", classes="metric-value")

                with Vertical(classes="ai-route-panel ai-chain-panel"):
                    yield Label("路由链", classes="ai-route-title")
                    yield Label("─", id="ai-route-chain", classes="current-info")

            with Vertical(classes="panel output-panel"):
                yield Label("连通性探测", classes="panel-title")
                yield Label("─", id="ai-probe-status", classes="status-strip")
                yield DataTable(id="ai-probe-table")

            with Horizontal(classes="toolbar"):
                yield Button("刷新", id="btn-ai-refresh", classes="action-button muted-button")
                yield Button("探测", id="btn-ai-probe", classes="action-button primary-button")
                yield Button("切换美国/新加坡", id="btn-ai-switch", classes="action-button success-button")

    def on_mount(self) -> None:
        probe_table = self.query_one("#ai-probe-table", DataTable)
        probe_table.add_columns("目标", "状态", "详情")
        probe_table.show_header = True
        if not list(self.app.query("#main-tabs")):
            self.call_later(self.refresh_data)

    def refresh_data(self) -> None:
        self.query_one("#ai-route-mode", Label).update("[#f6c177]刷新中…[/]")
        self._refresh_route_worker()

    @work(thread=True, exclusive=True, group="ai-route-refresh")
    def _refresh_route_worker(self) -> None:
        try:
            groups = QueryService(self.paths).get_ai_status_groups()
            self.app.call_from_thread(self._apply_route, groups, None)
        except Exception as exc:
            self.app.call_from_thread(self._apply_route, None, exc)

    def _apply_route(self, groups: dict | None, error: Exception | None) -> None:
        if error is not None or groups is None:
            label = "API 不可访问" if isinstance(error, APIUnavailableError) else f"错误: {error}"
            self.query_one("#ai-route-mode", Label).update(f"[#fb7185]{label}[/]")
            self.query_one("#ai-route-active", Label).update("[#8b98aa]─[/]")
            self.query_one("#ai-route-standby", Label).update("[#8b98aa]─[/]")
            self.query_one("#ai-route-chain", Label).update("[#8b98aa]─[/]")
            return
        try:
            manual = groups.get("AI-MANUAL")
            auto = groups.get("AI-AUTO")

            if manual is None or auto is None:
                self.query_one("#ai-route-mode", Label).update("[#8b98aa]尚未配置[/]")
                self.query_one("#ai-route-active", Label).update("[#8b98aa]─[/]")
                self.query_one("#ai-route-standby", Label).update("[#8b98aa]─[/]")
                self.query_one("#ai-route-chain", Label).update("[#8b98aa]请先生成运行配置[/]")
                return

            auto_mode = manual.current == "AI-AUTO"
            active_group_name = auto.current if auto_mode else manual.current
            active_group = groups.get(active_group_name)

            standby_name = "AI-SG" if active_group_name == "AI-US" else "AI-US"
            standby_group = groups.get(standby_name)

            mode_text = "[#f6c177]自动[/]" if auto_mode else f"[#f6c177]固定[/]（{manual.current}）"
            self.query_one("#ai-route-mode", Label).update(mode_text)

            if active_group:
                delay_str = f"{active_group.delay}ms" if active_group.delay else "─"
                alive_str = (
                    "[#a3e635]●[/]" if active_group.alive
                    else "[#fb7185]○[/]" if active_group.alive is False
                    else "[#8b98aa]?[/]"
                )
                self.query_one("#ai-route-active", Label).update(
                    f"{active_group_name} → {active_group.current} ({delay_str}) {alive_str}"
                )

            if standby_group:
                delay_str = f"{standby_group.delay}ms" if standby_group.delay else "─"
                alive_str = (
                    "[#a3e635]●[/]" if standby_group.alive
                    else "[#fb7185]○[/]" if standby_group.alive is False
                    else "[#8b98aa]?[/]"
                )
                self.query_one("#ai-route-standby", Label).update(
                    f"{standby_name} → {standby_group.current} ({delay_str}) {alive_str}"
                )

            chain_lines = ["[#7dd3fc]AI-MANUAL[/]"]
            if auto_mode:
                chain_lines.append("└─ [#f6c177]AI-AUTO[/]")
                chain_lines.append(f"   └─ [#5eead4]{active_group_name}[/]")
                if active_group:
                    chain_lines.append(f"      └─ [#a3e635]{active_group.current}[/]")
            else:
                chain_lines.append(f"└─ [#5eead4]{active_group_name}[/]")
                if active_group:
                    chain_lines.append(f"   └─ [#a3e635]{active_group.current}[/]")
            self.query_one("#ai-route-chain", Label).update("\n".join(chain_lines))

        except Exception as e:
            self.query_one("#ai-route-mode", Label).update(f"[#fb7185]错误: {e}[/]")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-ai-refresh":
            self.refresh_data()
        elif event.button.id == "btn-ai-probe":
            self.action_probe_ai()
        elif event.button.id == "btn-ai-switch":
            self.action_switch_us_sg()

    def action_probe_ai(self) -> None:
        if self._probe_running:
            return
        self._probe_running = True
        self.query_one("#ai-probe-status", Label).update("[#f6c177]◐ 探测中…[/]")
        self.query_one("#btn-ai-probe", Button).disabled = True
        threading.Thread(target=self._probe_ai_worker, daemon=True).start()

    def _probe_ai_worker(self) -> None:
        try:
            report = run_ai_probe(self.paths)
            self._call_from_probe_thread(self._finish_probe_ai, report)
        except Exception as e:
            self._call_from_probe_thread(self._fail_probe_ai, e)

    def _call_from_probe_thread(self, callback, *args) -> None:
        try:
            self.app.call_from_thread(callback, *args)
        except Exception:
            self._probe_running = False

    def _finish_probe_ai(self, report) -> None:
        if not self.is_mounted:
            self._probe_running = False
            return
        probe_table = self.query_one("#ai-probe-table", DataTable)
        probe_table.clear()

        for item in report.results:
            status = "[#a3e635]● 正常[/]" if item.ok else "[#fb7185]○ 失败[/]"
            probe_table.add_row(item.name, status, item.detail or item.url)

        ok_count = sum(1 for item in report.results if item.ok)
        total = len(report.results)

        if ok_count == total:
            probe_status = f"[#a3e635]● {ok_count}/{total} 正常[/]"
        elif ok_count == 0:
            probe_status = f"[#fb7185]○ {ok_count}/{total} 失败[/]"
        else:
            probe_status = f"[#f6c177]◐ {ok_count}/{total} 部分正常[/]"
        self.query_one("#ai-probe-status", Label).update(probe_status)
        self.query_one("#btn-ai-probe", Button).disabled = False
        self._probe_running = False
        self.notify("探测完成", severity="information")

    def _fail_probe_ai(self, error: Exception) -> None:
        if not self.is_mounted:
            self._probe_running = False
            return
        self.query_one("#ai-probe-status", Label).update(f"[#fb7185]探测失败: {error}[/]")
        self.query_one("#btn-ai-probe", Button).disabled = False
        self._probe_running = False
        self.notify(f"探测失败: {error}", severity="error")

    def action_switch_us_sg(self) -> None:
        self.query_one("#ai-route-mode", Label).update("[#f6c177]切换中…[/]")
        self._switch_us_sg_worker()

    @work(thread=True, exclusive=True, group="ai-route-action")
    def _switch_us_sg_worker(self) -> None:
        try:
            service = QueryService(self.paths)
            groups = service.get_ai_status_groups()
            manual = groups.get("AI-MANUAL")
            auto = groups.get("AI-AUTO")

            if manual is None or auto is None:
                self.app.call_from_thread(
                    self._finish_switch,
                    "",
                    "",
                    RuntimeError("AI-MANUAL 或 AI-AUTO 尚未配置"),
                )
                return

            auto_mode = manual.current == "AI-AUTO"
            active_group_name = auto.current if auto_mode else manual.current

            target = "AI-SG" if active_group_name == "AI-US" else "AI-US"

            if auto_mode:
                service.switch_group("AI-AUTO", target)
            else:
                service.switch_group("AI-MANUAL", target)

            self.app.call_from_thread(self._finish_switch, active_group_name, target, None)

        except Exception as exc:
            self.app.call_from_thread(self._finish_switch, "", "", exc)

    def _finish_switch(self, active: str, target: str, error: Exception | None) -> None:
        if error is None:
            self.notify(f"已切换: {active} → {target}", severity="information")
            self.refresh_data()
        elif isinstance(error, APIUnavailableError):
            self.notify("API 不可访问", severity="error")
        else:
            self.notify(f"切换失败: {error}", severity="error")
