from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widget import Widget
from textual.widgets import Button, Label

from ...audit import write_audit_event
from ...config import AppPaths, config_file
from ...process import restart_process
from ...runtime import render_runtime
from ..widgets import NavigationTextArea as TextArea


class ConfigEditorScreen(Widget):
    BINDINGS = [
        Binding("ctrl+s", "save_config", "Save", priority=True),
        Binding("ctrl+r", "render_config", "Render", priority=True),
    ]

    def __init__(self, paths: AppPaths, **kwargs):
        super().__init__(**kwargs)
        self.paths = paths
        self._modified = False

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label("Config Editor", classes="page-title")
            with Vertical(classes="panel output-panel"):
                yield Label("Source Config", classes="panel-title")
                with Horizontal():
                    yield Label("─", id="config-file-label", classes="path-label")
                    yield Label("", id="config-modified-label", classes="status-strip")
                with Horizontal(classes="toolbar"):
                    yield Button("Save", id="btn-config-save", classes="action-button success-button")
                    yield Button("Render", id="btn-config-render", classes="action-button primary-button")
                    yield Button("Restart", id="btn-config-restart", classes="action-button danger-button")
                    yield Button("Reload File", id="btn-config-reload", classes="action-button muted-button")
                yield TextArea(id="config-editor", classes="config-editor")
                yield Label("─", id="config-log", classes="action-status")

    def on_mount(self) -> None:
        if not list(self.app.query("#main-tabs")):
            self._load_config()

    def refresh_data(self) -> None:
        if self._modified:
            self.query_one("#config-log", Label).update("[#f6c177]Config modified; refresh skipped[/]")
            return
        self._load_config()

    def _load_config(self) -> None:
        config_path = config_file(self.paths)
        self.query_one("#config-file-label", Label).update(f"[#8b98aa]{config_path}[/]")
        self.query_one("#config-modified-label", Label).update("")

        editor = self.query_one("#config-editor", TextArea)
        try:
            editor.theme = "monokai"
        except Exception:
            pass

        if config_path.exists():
            content = config_path.read_text(encoding="utf-8")
            editor.load_text(content)
        else:
            editor.load_text(f"# Config not found: {config_path}\n# Run 'cproxy init' to initialize")

        self._modified = False

    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        self._modified = True
        self.query_one("#config-modified-label", Label).update("[#f6c177](modified)[/]")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-config-save":
            self.action_save_config()
        elif event.button.id == "btn-config-render":
            self.action_render_config()
        elif event.button.id == "btn-config-restart":
            self.action_restart_config()
        elif event.button.id == "btn-config-reload":
            self._load_config()

    def action_save_config(self) -> None:
        config_path = config_file(self.paths)
        editor = self.query_one("#config-editor", TextArea)
        log_label = self.query_one("#config-log", Label)

        try:
            config_path.parent.mkdir(parents=True, exist_ok=True)
            config_path.write_text(editor.text, encoding="utf-8")
            self._modified = False

            self.query_one("#config-modified-label", Label).update("[#a3e635](saved)[/]")
            log_label.update(f"[#a3e635]Saved: {config_path}[/]")
            self.notify("Config saved", severity="information")

        except Exception as e:
            log_label.update(f"[#fb7185]Save failed: {e}[/]")
            self.notify(f"Save failed: {e}", severity="error")

    def action_render_config(self) -> None:
        log_label = self.query_one("#config-log", Label)

        if self._modified:
            log_label.update("[#f6c177]Save config before rendering[/]")
            return

        try:
            runtime_path = render_runtime(self.paths)
            log_label.update(f"[#a3e635]Rendered: {runtime_path}[/]")
            self.notify("Runtime config rendered", severity="information")

        except Exception as e:
            log_label.update(f"[#fb7185]Render failed: {e}[/]")
            self.notify(f"Render failed: {e}", severity="error")

    def action_restart_config(self) -> None:
        log_label = self.query_one("#config-log", Label)

        if self._modified:
            log_label.update("[#f6c177]Save config before restarting[/]")
            return

        try:
            runtime_path = render_runtime(self.paths)
            pid = restart_process(self.paths)
            write_audit_event(
                self.paths,
                action="config_restart",
                target=str(runtime_path),
                result="ok",
                detail={"pid": pid},
            )
            log_label.update(f"[#a3e635]Rendered {runtime_path}; restarted PID {pid}[/]")
            self.notify("Runtime config rendered and proxy restarted", severity="information")

        except Exception as e:
            log_label.update(f"[#fb7185]Restart failed: {e}[/]")
            self.notify(f"Restart failed: {e}", severity="error")
