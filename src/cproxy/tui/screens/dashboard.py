from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.timer import Timer
from textual.widget import Widget
from textual.widgets import Label

from ...config import AppPaths
from ...process import get_status
from ...api import APIUnavailableError
from ...services.query import QueryService


class DashboardScreen(Widget):
    def __init__(self, paths: AppPaths, **kwargs):
        super().__init__(**kwargs)
        self.paths = paths
        self._refresh_timer: Timer | None = None

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label("Overview", classes="page-title")

            with Horizontal(id="dashboard-grid"):
                with Vertical(classes="status-card runtime-card"):
                    yield Label("Runtime", classes="status-card-title")
                    with Horizontal(classes="dashboard-row"):
                        yield Label("Process", classes="label-key")
                        yield Label("─", id="dash-status", classes="metric-value")
                    with Horizontal(classes="dashboard-row"):
                        yield Label("API", classes="label-key")
                        yield Label("─", id="dash-api-status", classes="metric-value")
                    with Horizontal(classes="dashboard-row"):
                        yield Label("Port", classes="label-key")
                        yield Label("─", id="dash-port", classes="metric-value")
                    with Horizontal(classes="dashboard-row"):
                        yield Label("Controller", classes="label-key")
                        yield Label("─", id="dash-controller", classes="metric-value")
                    with Horizontal(classes="dashboard-row"):
                        yield Label("PID", classes="label-key")
                        yield Label("─", id="dash-pid", classes="metric-value")

                with Vertical(classes="status-card ai-card"):
                    yield Label("AI Route", classes="status-card-title")
                    with Horizontal(classes="dashboard-row"):
                        yield Label("Mode", classes="label-key")
                        yield Label("─", id="dash-ai-mode", classes="metric-value")
                    with Horizontal(classes="dashboard-row"):
                        yield Label("Active", classes="label-key")
                        yield Label("─", id="dash-ai-active", classes="metric-value")
                    with Horizontal(classes="dashboard-row"):
                        yield Label("Standby", classes="label-key")
                        yield Label("─", id="dash-ai-standby", classes="metric-value")

                with Vertical(classes="status-card traffic-card"):
                    yield Label("Traffic", classes="status-card-title")
                    with Horizontal(classes="dashboard-row"):
                        yield Label("Upload", classes="label-key")
                        yield Label("─", id="dash-upload", classes="metric-value")
                    with Horizontal(classes="dashboard-row"):
                        yield Label("Download", classes="label-key")
                        yield Label("─", id="dash-download", classes="metric-value")
                    with Horizontal(classes="dashboard-row"):
                        yield Label("Connections", classes="label-key")
                        yield Label("─", id="dash-connections", classes="metric-value")

    def on_mount(self) -> None:
        self.call_later(self.refresh_data)
        self._refresh_timer = self.set_interval(5, self.refresh_data)

    def on_unmount(self) -> None:
        if self._refresh_timer:
            self._refresh_timer.stop()

    def refresh_data(self) -> None:
        self._update_status()
        service = QueryService(self.paths)
        self._update_ai_route(service)
        self._update_traffic(service)

    def _update_status(self) -> None:
        try:
            snapshot = get_status(self.paths)
            status_text = "[#a3e635]● Running[/]" if snapshot.running else "[#fb7185]○ Stopped[/]"
            self.query_one("#dash-status", Label).update(status_text)
            self.query_one("#dash-port", Label).update(str(snapshot.port))
            self.query_one("#dash-controller", Label).update(snapshot.controller)
            self.query_one("#dash-pid", Label).update(str(snapshot.pid) if snapshot.pid else "─")
        except Exception as e:
            self.query_one("#dash-status", Label).update(f"[#fb7185]Error: {e}[/]")

    def _update_ai_route(self, service: QueryService) -> None:
        try:
            groups = service.get_ai_status_groups()
            self.query_one("#dash-api-status", Label).update("[#a3e635]● Connected[/]")

            manual = groups.get("AI-MANUAL")
            auto = groups.get("AI-AUTO")
            if manual and auto:
                auto_mode = manual.current == "AI-AUTO"
                active_group_name = auto.current if auto_mode else manual.current
                active = groups.get(active_group_name)

                mode_text = "Auto" if auto_mode else f"Manual ({manual.current})"
                self.query_one("#dash-ai-mode", Label).update(f"[#f6c177]{mode_text}[/]")

                if active:
                    delay_str = f"{active.delay}ms" if active.delay else "─"
                    alive_str = "[#a3e635]●[/]" if active.alive else "[#fb7185]○[/]" if active.alive is False else "[#8b98aa]?[/]"
                    self.query_one("#dash-ai-active", Label).update(
                        f"{active_group_name} → {active.current} ({delay_str}) {alive_str}"
                    )

                standby_name = "AI-SG" if active_group_name == "AI-US" else "AI-US"
                standby = groups.get(standby_name)
                if standby:
                    delay_str = f"{standby.delay}ms" if standby.delay else "─"
                    alive_str = "[#a3e635]●[/]" if standby.alive else "[#fb7185]○[/]" if standby.alive is False else "[#8b98aa]?[/]"
                    self.query_one("#dash-ai-standby", Label).update(
                        f"{standby_name} → {standby.current} ({delay_str}) {alive_str}"
                    )

        except APIUnavailableError:
            self.query_one("#dash-api-status", Label).update("[#fb7185]○ Disconnected[/]")
            self.query_one("#dash-ai-mode", Label).update("[#8b98aa]─[/]")
            self.query_one("#dash-ai-active", Label).update("[#8b98aa]─[/]")
            self.query_one("#dash-ai-standby", Label).update("[#8b98aa]─[/]")
        except Exception:
            pass

    def _update_traffic(self, service: QueryService) -> None:
        try:
            api = service.api
            data = api.request("GET", "/connections")
            upload_total = data.get("uploadTotal", 0)
            download_total = data.get("downloadTotal", 0)
            active_count = len(data.get("connections", []))

            self.query_one("#dash-upload", Label).update(f"[#5eead4]{self._format_bytes(upload_total)}[/]")
            self.query_one("#dash-download", Label).update(f"[#5eead4]{self._format_bytes(download_total)}[/]")
            self.query_one("#dash-connections", Label).update(f"[#5eead4]{active_count}[/]")
        except Exception:
            self.query_one("#dash-upload", Label).update("[#8b98aa]─[/]")
            self.query_one("#dash-download", Label).update("[#8b98aa]─[/]")
            self.query_one("#dash-connections", Label).update("[#8b98aa]─[/]")

    def _format_bytes(self, bytes_val: int) -> str:
        if bytes_val == 0:
            return "0 B"
        units = ["B", "KB", "MB", "GB", "TB"]
        i = 0
        val = float(bytes_val)
        while val >= 1024 and i < len(units) - 1:
            val /= 1024
            i += 1
        return f"{val:.1f} {units[i]}"
