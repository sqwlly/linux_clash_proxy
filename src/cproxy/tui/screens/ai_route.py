from __future__ import annotations

import threading

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
        Binding("p", "probe_ai", "Probe"),
        Binding("s", "switch_us_sg", "Switch"),
        Binding("r", "refresh_data", "Refresh"),
    ]

    def __init__(self, paths: AppPaths, **kwargs):
        super().__init__(**kwargs)
        self.paths = paths
        self._probe_running = False

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label("AI Route", classes="page-title")

            with Horizontal():
                with Vertical(classes="ai-route-panel ai-selector-panel"):
                    yield Label("Selector", classes="ai-route-title")
                    with Horizontal(classes="field-row"):
                        yield Label("Mode", classes="label-key")
                        yield Label("─", id="ai-route-mode", classes="metric-value")
                    with Horizontal(classes="field-row"):
                        yield Label("Active", classes="label-key")
                        yield Label("─", id="ai-route-active", classes="metric-value")
                    with Horizontal(classes="field-row"):
                        yield Label("Standby", classes="label-key")
                        yield Label("─", id="ai-route-standby", classes="metric-value")

                with Vertical(classes="ai-route-panel ai-chain-panel"):
                    yield Label("Route Chain", classes="ai-route-title")
                    yield Label("─", id="ai-route-chain", classes="current-info")

            with Vertical(classes="panel output-panel"):
                yield Label("Connectivity Probe", classes="panel-title")
                yield Label("─", id="ai-probe-status", classes="status-strip")
                yield DataTable(id="ai-probe-table")

            with Horizontal(classes="toolbar"):
                yield Button("Refresh", id="btn-ai-refresh", classes="action-button muted-button")
                yield Button("Probe", id="btn-ai-probe", classes="action-button primary-button")
                yield Button("Switch US/SG", id="btn-ai-switch", classes="action-button success-button")

    def on_mount(self) -> None:
        probe_table = self.query_one("#ai-probe-table", DataTable)
        probe_table.add_columns("Target", "Status", "Detail")
        probe_table.show_header = True
        if not list(self.app.query("#main-tabs")):
            self.call_later(self.refresh_data)

    def refresh_data(self) -> None:
        try:
            service = QueryService(self.paths)
            groups = service.get_ai_status_groups()

            manual = groups.get("AI-MANUAL")
            auto = groups.get("AI-AUTO")

            if not all([manual, auto]):
                self.query_one("#ai-route-mode", Label).update("[#8b98aa]Not configured[/]")
                self.query_one("#ai-route-active", Label).update("[#8b98aa]─[/]")
                self.query_one("#ai-route-standby", Label).update("[#8b98aa]─[/]")
                self.query_one("#ai-route-chain", Label).update("[#8b98aa]Render runtime config first[/]")
                return

            auto_mode = manual.current == "AI-AUTO"
            active_group_name = auto.current if auto_mode else manual.current
            active_group = groups.get(active_group_name)

            standby_name = "AI-SG" if active_group_name == "AI-US" else "AI-US"
            standby_group = groups.get(standby_name)

            mode_text = "[#f6c177]Auto[/]" if auto_mode else f"[#f6c177]Manual[/] ({manual.current})"
            self.query_one("#ai-route-mode", Label).update(mode_text)

            if active_group:
                delay_str = f"{active_group.delay}ms" if active_group.delay else "─"
                alive_str = "[#a3e635]●[/]" if active_group.alive else "[#fb7185]○[/]" if active_group.alive is False else "[#8b98aa]?[/]"
                self.query_one("#ai-route-active", Label).update(
                    f"{active_group_name} → {active_group.current} ({delay_str}) {alive_str}"
                )

            if standby_group:
                delay_str = f"{standby_group.delay}ms" if standby_group.delay else "─"
                alive_str = "[#a3e635]●[/]" if standby_group.alive else "[#fb7185]○[/]" if standby_group.alive is False else "[#8b98aa]?[/]"
                self.query_one("#ai-route-standby", Label).update(
                    f"{standby_name} → {standby_group.current} ({delay_str}) {alive_str}"
                )

            chain_lines = ["[#7dd3fc]AI-MANUAL[/]"]
            if auto_mode:
                chain_lines.append(f"└─ [#f6c177]AI-AUTO[/]")
                chain_lines.append(f"   └─ [#5eead4]{active_group_name}[/]")
                if active_group:
                    chain_lines.append(f"      └─ [#a3e635]{active_group.current}[/]")
            else:
                chain_lines.append(f"└─ [#5eead4]{active_group_name}[/]")
                if active_group:
                    chain_lines.append(f"   └─ [#a3e635]{active_group.current}[/]")
            self.query_one("#ai-route-chain", Label).update("\n".join(chain_lines))

        except APIUnavailableError:
            self.query_one("#ai-route-mode", Label).update("[#fb7185]API Unavailable[/]")
            self.query_one("#ai-route-active", Label).update("[#8b98aa]─[/]")
            self.query_one("#ai-route-standby", Label).update("[#8b98aa]─[/]")
            self.query_one("#ai-route-chain", Label).update("[#8b98aa]─[/]")
        except Exception as e:
            self.query_one("#ai-route-mode", Label).update(f"[#fb7185]Error: {e}[/]")

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
        self.query_one("#ai-probe-status", Label).update("[#f6c177]◐ Probing...[/]")
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
            status = "[#a3e635]● OK[/]" if item.ok else "[#fb7185]○ FAIL[/]"
            probe_table.add_row(item.name, status, item.detail or item.url)

        ok_count = sum(1 for item in report.results if item.ok)
        total = len(report.results)

        if ok_count == total:
            probe_status = f"[#a3e635]● {ok_count}/{total} OK[/]"
        elif ok_count == 0:
            probe_status = f"[#fb7185]○ {ok_count}/{total} Failed[/]"
        else:
            probe_status = f"[#f6c177]◐ {ok_count}/{total} Partial[/]"
        self.query_one("#ai-probe-status", Label).update(probe_status)
        self.query_one("#btn-ai-probe", Button).disabled = False
        self._probe_running = False
        self.notify("Probe complete", severity="information")

    def _fail_probe_ai(self, error: Exception) -> None:
        if not self.is_mounted:
            self._probe_running = False
            return
        self.query_one("#ai-probe-status", Label).update(f"[#fb7185]Probe failed: {error}[/]")
        self.query_one("#btn-ai-probe", Button).disabled = False
        self._probe_running = False
        self.notify(f"Probe failed: {error}", severity="error")

    def action_switch_us_sg(self) -> None:
        try:
            service = QueryService(self.paths)
            groups = service.get_ai_status_groups()
            manual = groups.get("AI-MANUAL")
            auto = groups.get("AI-AUTO")

            if not all([manual, auto]):
                return

            auto_mode = manual.current == "AI-AUTO"
            active_group_name = auto.current if auto_mode else manual.current

            target = "AI-SG" if active_group_name == "AI-US" else "AI-US"

            if auto_mode:
                service.switch_group("AI-AUTO", target)
            else:
                service.switch_group("AI-MANUAL", target)

            self.notify(f"Switched: {active_group_name} → {target}", severity="information")
            self.refresh_data()

        except APIUnavailableError:
            self.notify("API unavailable", severity="error")
        except Exception as e:
            self.notify(f"Switch failed: {e}", severity="error")
