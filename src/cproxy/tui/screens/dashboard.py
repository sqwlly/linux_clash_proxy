from __future__ import annotations

import threading
from datetime import datetime

from textual import work
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.timer import Timer
from textual.widget import Widget
from textual.widgets import Label

from ...backend.runtime import resolve_ai_route
from ...config import AppPaths
from ...process import get_status
from ...services.query import QueryService
from ...services.speed import RateMeter, format_rate
from ..ai_status import IDLE, exit_line_or_idle, mode_markup
from ..widgets import coerce_worker_error, escape_markup, safe_call_from_thread


class DashboardScreen(Widget):
    def __init__(self, paths: AppPaths, **kwargs):
        super().__init__(**kwargs)
        self.paths = paths
        self._refresh_timer: Timer | None = None
        self._timer_paused = False
        self._rate_meter = RateMeter()
        self._rate_lock = threading.Lock()

    def compose(self) -> ComposeResult:
        with Vertical():
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
                        yield Label("通用", classes="label-key")
                        yield Label("─", id="dash-ai-active", classes="metric-value")
                    with Horizontal(classes="dashboard-row"):
                        yield Label("Gemini", classes="label-key")
                        yield Label("─", id="dash-ai-gemini", classes="metric-value")
                    with Horizontal(classes="dashboard-row"):
                        yield Label("备用", classes="label-key")
                        yield Label("─", id="dash-ai-standby", classes="metric-value")

                with Vertical(classes="status-card traffic-card"):
                    yield Label("实时网速", classes="status-card-title")
                    with Horizontal(classes="dashboard-row"):
                        yield Label("下载", classes="label-key")
                        yield Label("─", id="dash-download", classes="metric-value")
                    with Horizontal(classes="dashboard-row"):
                        yield Label("上传", classes="label-key")
                        yield Label("─", id="dash-upload", classes="metric-value")
                    with Horizontal(classes="dashboard-row"):
                        yield Label("累计", classes="label-key")
                        yield Label("─", id="dash-total", classes="metric-value")
                    with Horizontal(classes="dashboard-row"):
                        yield Label("连接", classes="label-key")
                        yield Label("─", id="dash-connections", classes="metric-value")

    def on_mount(self) -> None:
        self.call_later(self.refresh_data)
        self._refresh_timer = self.set_interval(5, self.refresh_data)

    def on_hide(self) -> None:
        # 切走 tab 后暂停轮询，避免空转
        if self._refresh_timer is not None:
            self._refresh_timer.pause()
            self._timer_paused = True

    def on_show(self) -> None:
        # 初次挂载也会收到 Show；那时 timer 尚未暂停，交给 on_mount 的 call_later 刷新
        if self._refresh_timer is None or not self._timer_paused:
            return
        self._timer_paused = False
        self._refresh_timer.resume()
        self.refresh_data()

    def on_unmount(self) -> None:
        if self._refresh_timer:
            self._refresh_timer.stop()
            self._refresh_timer = None

    def refresh_data(self) -> None:
        if not self.is_mounted:
            return
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
        except (Exception, SystemExit) as exc:
            result["status_error"] = str(coerce_worker_error(exc))

        service = QueryService(self.paths)
        try:
            result["groups"] = service.get_ai_status_groups()
        except (Exception, SystemExit) as exc:
            result["api_error"] = str(coerce_worker_error(exc))
        try:
            data = service.api.request("GET", "/connections")
            if not isinstance(data, dict):
                raise ValueError("connections 响应格式无效")
            upload = int(data.get("uploadTotal") or 0)
            download = int(data.get("downloadTotal") or 0)
            raw_conns = data.get("connections")
            conn_count = len(raw_conns) if isinstance(raw_conns, list) else 0
            with self._rate_lock:
                rate = self._rate_meter.sample(upload, download)
            result["traffic"] = {
                "upload": upload,
                "download": download,
                "rate": rate,
                "connections": conn_count,
            }
        except (Exception, SystemExit):
            result["traffic"] = None
        safe_call_from_thread(self, self._apply_dashboard, result)

    def _reset_ai_labels(self) -> None:
        for selector in ("#dash-ai-mode", "#dash-ai-active", "#dash-ai-gemini", "#dash-ai-standby"):
            self.query_one(selector, Label).update(IDLE)

    def _apply_dashboard(self, result: dict) -> None:
        if not self.is_mounted:
            return
        snapshot = result["snapshot"]
        if snapshot is not None:
            status_text = "[#a3e635]● 运行中[/]" if snapshot.running else "[#fb7185]○ 已停止[/]"
            self.query_one("#dash-status", Label).update(status_text)
            self.query_one("#dash-port", Label).update(escape_markup(snapshot.port))
            self.query_one("#dash-controller", Label).update(escape_markup(snapshot.controller))
            self.query_one("#dash-pid", Label).update(escape_markup(snapshot.pid) if snapshot.pid else "─")
        else:
            self.query_one("#dash-status", Label).update(
                f"[#fb7185]错误: {escape_markup(result['status_error'])}[/]"
            )
            for selector in ("#dash-port", "#dash-controller", "#dash-pid"):
                self.query_one(selector, Label).update(IDLE)

        groups = result["groups"]
        if groups is not None:
            self._apply_ai_route(groups)
        else:
            self.query_one("#dash-api-status", Label).update("[#fb7185]○ 不可访问[/]")
            self._reset_ai_labels()

        traffic = result["traffic"]
        if traffic is None:
            for selector in ("#dash-download", "#dash-upload", "#dash-total", "#dash-connections"):
                self.query_one(selector, Label).update(IDLE)
        else:
            rate = traffic.get("rate")
            if rate is None:
                self.query_one("#dash-download", Label).update("[#8b98aa]等待采样[/]")
                self.query_one("#dash-upload", Label).update("[#8b98aa]等待采样[/]")
            else:
                self.query_one("#dash-download", Label).update(f"[#5eead4]↓ {format_rate(rate.download_bps)}[/]")
                self.query_one("#dash-upload", Label).update(f"[#5eead4]↑ {format_rate(rate.upload_bps)}[/]")
            self.query_one("#dash-total", Label).update(
                f"[#8b98aa]{self._format_bytes(traffic['upload'] + traffic['download'])}[/]"
            )
            self.query_one("#dash-connections", Label).update(f"[#5eead4]{traffic['connections']}[/]")
        self.query_one("#dash-refresh-status", Label).update(
            f"[#8b98aa]最后刷新 {datetime.now():%H:%M:%S} · F5 手动刷新[/]"
        )

    def _apply_ai_route(self, groups: dict) -> None:
        if not self.is_mounted:
            return
        self.query_one("#dash-api-status", Label).update("[#a3e635]● 已连接[/]")
        try:
            route = resolve_ai_route(groups)
        except Exception as exc:
            self._reset_ai_labels()
            self.query_one("#dash-ai-mode", Label).update(f"[#fb7185]错误: {escape_markup(exc)}[/]")
            return

        if not route.present:
            self._reset_ai_labels()
            return

        self.query_one("#dash-ai-mode", Label).update(mode_markup(route))
        self.query_one("#dash-ai-active", Label).update(exit_line_or_idle(route.active, groups))
        self.query_one("#dash-ai-gemini", Label).update(exit_line_or_idle(route.gemini, groups))
        self.query_one("#dash-ai-standby", Label).update(exit_line_or_idle(route.standby, groups))

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
