from __future__ import annotations

from datetime import datetime

from textual import work
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.timer import Timer
from textual.widget import Widget
from textual.widgets import Label

from ...config import AppPaths
from ...process import get_status
from ...services.query import QueryService


class DashboardScreen(Widget):
    def __init__(self, paths: AppPaths, **kwargs):
        super().__init__(**kwargs)
        self.paths = paths
        self._refresh_timer: Timer | None = None

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label("概览", classes="page-title")
            yield Label("等待刷新", id="dash-refresh-status", classes="status-strip")

            with Horizontal(id="dashboard-grid"):
                with Vertical(classes="status-card runtime-card"):
                    yield Label("运行状态", classes="status-card-title")
                    with Horizontal(classes="dashboard-row"):
                        yield Label("进程", classes="label-key")
                        yield Label("─", id="dash-status", classes="metric-value")
                    with Horizontal(classes="dashboard-row"):
                        yield Label("API", classes="label-key")
                        yield Label("─", id="dash-api-status", classes="metric-value")
                    with Horizontal(classes="dashboard-row"):
                        yield Label("端口", classes="label-key")
                        yield Label("─", id="dash-port", classes="metric-value")
                    with Horizontal(classes="dashboard-row"):
                        yield Label("控制接口", classes="label-key")
                        yield Label("─", id="dash-controller", classes="metric-value")
                    with Horizontal(classes="dashboard-row"):
                        yield Label("PID", classes="label-key")
                        yield Label("─", id="dash-pid", classes="metric-value")

                with Vertical(classes="status-card ai-card"):
                    yield Label("AI 路由", classes="status-card-title")
                    with Horizontal(classes="dashboard-row"):
                        yield Label("模式", classes="label-key")
                        yield Label("─", id="dash-ai-mode", classes="metric-value")
                    with Horizontal(classes="dashboard-row"):
                        yield Label("当前", classes="label-key")
                        yield Label("─", id="dash-ai-active", classes="metric-value")
                    with Horizontal(classes="dashboard-row"):
                        yield Label("备用", classes="label-key")
                        yield Label("─", id="dash-ai-standby", classes="metric-value")

                with Vertical(classes="status-card traffic-card"):
                    yield Label("实时流量", classes="status-card-title")
                    with Horizontal(classes="dashboard-row"):
                        yield Label("上传", classes="label-key")
                        yield Label("─", id="dash-upload", classes="metric-value")
                    with Horizontal(classes="dashboard-row"):
                        yield Label("下载", classes="label-key")
                        yield Label("─", id="dash-download", classes="metric-value")
                    with Horizontal(classes="dashboard-row"):
                        yield Label("连接", classes="label-key")
                        yield Label("─", id="dash-connections", classes="metric-value")

    def on_mount(self) -> None:
        self.call_later(self.refresh_data)
        self._refresh_timer = self.set_interval(5, self.refresh_data)

    def on_unmount(self) -> None:
        if self._refresh_timer:
            self._refresh_timer.stop()

    def refresh_data(self) -> None:
        try:
            self.query_one("#dash-refresh-status", Label).update("[#f6c177]刷新中…[/]")
        except Exception:
            return
        self._load_dashboard()

    @work(thread=True, exclusive=True, group="dashboard-refresh")
    def _load_dashboard(self) -> None:
        result: dict = {"snapshot": None, "status_error": None, "groups": None, "api_error": None, "traffic": None}
        try:
            result["snapshot"] = get_status(self.paths)
        except Exception as exc:
            result["status_error"] = str(exc)

        service = QueryService(self.paths)
        try:
            result["groups"] = service.get_ai_status_groups()
        except Exception as exc:
            result["api_error"] = str(exc)
        try:
            data = service.api.request("GET", "/connections")
            result["traffic"] = {
                "upload": data.get("uploadTotal", 0),
                "download": data.get("downloadTotal", 0),
                "connections": len(data.get("connections", [])),
            }
        except Exception:
            result["traffic"] = None
        self.app.call_from_thread(self._apply_dashboard, result)

    def _apply_dashboard(self, result: dict) -> None:
        snapshot = result["snapshot"]
        if snapshot is not None:
            status_text = "[#a3e635]● 运行中[/]" if snapshot.running else "[#fb7185]○ 已停止[/]"
            self.query_one("#dash-status", Label).update(status_text)
            self.query_one("#dash-port", Label).update(str(snapshot.port))
            self.query_one("#dash-controller", Label).update(snapshot.controller)
            self.query_one("#dash-pid", Label).update(str(snapshot.pid) if snapshot.pid else "─")
        else:
            self.query_one("#dash-status", Label).update(f"[#fb7185]错误: {result['status_error']}[/]")

        groups = result["groups"]
        if groups is not None:
            self._apply_ai_route(groups)
        else:
            self.query_one("#dash-api-status", Label).update("[#fb7185]○ 不可访问[/]")
            self.query_one("#dash-ai-mode", Label).update("[#8b98aa]─[/]")
            self.query_one("#dash-ai-active", Label).update("[#8b98aa]─[/]")
            self.query_one("#dash-ai-standby", Label).update("[#8b98aa]─[/]")

        traffic = result["traffic"]
        if traffic is None:
            for selector in ("#dash-upload", "#dash-download", "#dash-connections"):
                self.query_one(selector, Label).update("[#8b98aa]─[/]")
        else:
            self.query_one("#dash-upload", Label).update(f"[#5eead4]{self._format_bytes(traffic['upload'])}[/]")
            self.query_one("#dash-download", Label).update(f"[#5eead4]{self._format_bytes(traffic['download'])}[/]")
            self.query_one("#dash-connections", Label).update(f"[#5eead4]{traffic['connections']}[/]")
        self.query_one("#dash-refresh-status", Label).update(
            f"[#8b98aa]最后刷新 {datetime.now():%H:%M:%S} · F5 手动刷新[/]"
        )

    def _apply_ai_route(self, groups: dict) -> None:
        try:
            self.query_one("#dash-api-status", Label).update("[#a3e635]● 已连接[/]")

            manual = groups.get("AI-MANUAL")
            auto = groups.get("AI-AUTO")
            if manual and auto:
                auto_mode = manual.current == "AI-AUTO"
                active_group_name = auto.current if auto_mode else manual.current
                active = groups.get(active_group_name)

                mode_text = "自动" if auto_mode else f"固定（{manual.current}）"
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
                    alive_str = (
                        "[#a3e635]●[/]" if standby.alive
                        else "[#fb7185]○[/]" if standby.alive is False
                        else "[#8b98aa]?[/]"
                    )
                    self.query_one("#dash-ai-standby", Label).update(
                        f"{standby_name} → {standby.current} ({delay_str}) {alive_str}"
                    )

        except Exception:
            pass

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
