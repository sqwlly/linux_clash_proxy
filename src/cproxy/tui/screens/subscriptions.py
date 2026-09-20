from __future__ import annotations

import shutil
import tempfile
import threading
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widget import Widget
from textual.widgets import Button, Label

from ...backend.runtime import RuntimeBackend
from ...config import AppPaths, config_file, read_config
from ...services.refresh import RefreshService, preview_subscription
from ..widgets import NavigationDataTable as DataTable
from ..widgets import NavigationInput as Input
from ..widgets import NavigationTextArea as TextArea


def redact_subscription_url(url: str) -> str:
    parsed = urlsplit(url)
    if not parsed.scheme or not parsed.netloc:
        return url[:80] + ("..." if len(url) > 80 else "")
    path = parsed.path
    if len(path) > 24:
        path = path[:12] + "..." + path[-8:]
    query = "..." if parsed.query else ""
    return urlunsplit((parsed.scheme, parsed.netloc, path, query, ""))


def subscription_group_rows(config: dict) -> list[tuple[str, str, str, str]]:
    groups = [group for group in config.get("proxy-groups", []) if isinstance(group, dict)]
    parents_by_child: dict[str, list[str]] = {}
    for group in groups:
        parent_name = str(group.get("name", ""))
        for child in group.get("proxies", []) or []:
            parents_by_child.setdefault(str(child), []).append(parent_name)

    rows = []
    for group in groups:
        name = str(group.get("name", ""))
        if not name:
            continue
        group_type = str(group.get("type", ""))
        proxies = [str(item) for item in group.get("proxies", []) or []]
        parents = [parent for parent in parents_by_child.get(name, []) if parent != name]
        rows.append((name, group_type, str(len(proxies)), ", ".join(parents) or "─"))
    return rows


class SubscriptionsScreen(Widget):
    def __init__(self, paths: AppPaths, **kwargs):
        super().__init__(**kwargs)
        self.paths = paths
        self._subscription_running = False

    def compose(self) -> ComposeResult:
        with Vertical():
            with Vertical(classes="panel form-panel"):
                yield Label("导入订阅", classes="panel-title")
                yield Input(
                    placeholder="订阅 URL（Clash / VLESS / Base64）…",
                    id="sub-url-input",
                    classes="subscription-input",
                )
                with Horizontal(classes="input-row"):
                    yield Input(
                        placeholder="附加订阅分组名（留空表示主订阅）",
                        id="sub-group-input",
                        classes="subscription-input",
                    )
                    yield Input(
                        placeholder="挂载到选择器（默认不挂，与主订阅同级）",
                        id="sub-attach-input",
                        classes="subscription-input",
                    )
                with Horizontal(classes="toolbar"):
                    yield Button("预览", id="btn-sub-preview", classes="action-button muted-button")
                    yield Button("应用", id="btn-sub-apply", classes="action-button success-button")
                    yield Button("验证", id="btn-sub-update", classes="action-button primary-button")

            with Horizontal(classes="workbench-row"):
                with Vertical(classes="panel output-panel split-main"):
                    yield Label("输出", classes="panel-title")
                    yield TextArea(id="sub-output", read_only=True, classes="output-area")

                with Vertical(classes="panel split-sidebar summary-panel"):
                    yield Label("订阅分组", classes="panel-title")
                    yield Label("─", id="sub-current-info", classes="current-info")
                    yield DataTable(id="sub-groups-table")

    def on_mount(self) -> None:
        output = self.query_one("#sub-output", TextArea)
        try:
            output.theme = "monokai"
        except Exception:
            pass
        groups_table = self.query_one("#sub-groups-table", DataTable)
        groups_table.add_columns("分组", "类型", "节点数", "挂载到")
        groups_table.cursor_type = "row"
        groups_table.show_header = True
        if not list(self.app.query("#main-tabs")):
            self._load_current_info()

    def refresh_data(self) -> None:
        self._load_current_info()

    def _load_current_info(self) -> None:
        try:
            config = read_config(self.paths)
            config_path = config_file(self.paths)

            proxies = config.get("proxies", [])
            groups = config.get("proxy-groups", [])
            group_rows = subscription_group_rows(config)

            info_text = "\n".join(
                [
                    f"[#8b98aa]路径[/]\n{config_path}",
                    f"[#8b98aa]节点[/] {len(proxies)}",
                    f"[#8b98aa]分组[/] {len(groups)}",
                    f"[#8b98aa]端口[/] {config.get('mixed-port', '─')}",
                    f"[#8b98aa]模式[/] {config.get('mode', '─')}",
                ]
            )
            self.query_one("#sub-current-info", Label).update(info_text)
            groups_table = self.query_one("#sub-groups-table", DataTable)
            groups_table.clear()
            for row in group_rows:
                groups_table.add_row(*row, key=row[0])
            if not group_rows:
                groups_table.add_row("没有分组", "─", "0", "─")
        except Exception as e:
            self.query_one("#sub-current-info", Label).update(f"[#fb7185]错误: {e}[/]")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-sub-preview":
            self._import_subscription(dry_run=True)
        elif event.button.id == "btn-sub-apply":
            self._import_subscription(dry_run=False)
        elif event.button.id == "btn-sub-update":
            self._update_config()

    def _import_subscription(self, dry_run: bool) -> None:
        if self._subscription_running:
            return
        url_input = self.query_one("#sub-url-input", Input)
        group_input = self.query_one("#sub-group-input", Input)
        attach_input = self.query_one("#sub-attach-input", Input)
        output = self.query_one("#sub-output", TextArea)

        url = url_input.value.strip()
        if not url:
            output.load_text("请输入订阅 URL")
            return

        group = group_input.value.strip()
        attach_to = attach_input.value.strip()
        if attach_to and not group:
            output.load_text("填写挂载目标时，也必须填写附加订阅分组名。")
            return

        output.load_text(
            "\n".join(
                [
                    "正在预览订阅…" if dry_run else "正在应用订阅…",
                    f"URL: {redact_subscription_url(url)}",
                    "请稍候…",
                ]
            )
        )

        try:
            self._set_subscription_busy(True)
            threading.Thread(
                target=self._native_subscription_worker,
                args=(url, group, attach_to, dry_run),
                daemon=True,
            ).start()
        except Exception as e:
            output.load_text(f"错误: {e}")
            self._set_subscription_busy(False)

    def _native_subscription_worker(self, url: str, group: str, attach_to: str, dry_run: bool) -> None:
        try:
            if dry_run:
                preview = preview_subscription(self.paths, url)
                output_text = (
                    "预览: 正常\n"
                    f"格式: {preview.source}\n"
                    f"大小: {preview.bytes} bytes\n"
                    f"节点: {preview.proxy_count}\n"
                    f"分组: {preview.group_count}\n"
                    "未写入任何文件。"
                )
                imported = False
            else:
                service = RefreshService(self.paths)
                if group:
                    report = service.refresh_extra_subscription(group, url, attach_to)
                else:
                    report = service.refresh(subscription_url=url, groups=[])
                extra = next((item for item in report.extra_subscriptions if item.name == group), None)
                if attach_to:
                    target_text = f"\n挂载到: {attach_to}"
                elif group:
                    target_text = "\n未挂载（与默认流量同级，切此订阅不会改 MATCH）"
                else:
                    target_text = ""
                extra_text = f"\n附加订阅: {extra.status}（{extra.detail}）" if extra is not None else ""
                output_text = (
                    f"应用: {'附加订阅已更新' if group else report.subscription}\n"
                    f"分组: {group or '主订阅'}{target_text}{extra_text}\n"
                    f"运行配置: {report.runtime_path}\n"
                    f"热重载: {'是' if report.hot_reloaded else '否'}\n"
                    f"进程重启: {'是' if report.restarted else '否'}"
                )
                imported = report.subscription != "失败"
            self._call_from_subscription_thread(self._finish_import_subscription, output_text, imported)
        except Exception as exc:
            self._call_from_subscription_thread(self._fail_subscription_command, f"错误: {exc}")

    def _finish_import_subscription(self, output_text: str, imported: bool) -> None:
        if not self.is_mounted:
            self._subscription_running = False
            return
        self.query_one("#sub-output", TextArea).load_text(output_text)
        self._set_subscription_busy(False)
        if imported:
            self._load_current_info()
            self.notify("订阅已应用", severity="information")

    def _fail_subscription_command(self, output_text: str) -> None:
        if not self.is_mounted:
            self._subscription_running = False
            return
        self.query_one("#sub-output", TextArea).load_text(output_text)
        self._set_subscription_busy(False)

    def _update_config(self) -> None:
        output = self.query_one("#sub-output", TextArea)
        config_path = config_file(self.paths)

        if self._subscription_running:
            return

        if not config_path.exists():
            output.load_text(f"配置不存在: {config_path}")
            return

        output.load_text("正在隔离目录中验证配置，不会覆盖当前运行配置…")

        self._set_subscription_busy(True)
        threading.Thread(target=self._validate_config_worker, daemon=True).start()

    def _validate_config_worker(self) -> None:
        try:
            with tempfile.TemporaryDirectory(prefix="cproxy-validate-") as temp_dir:
                root = Path(temp_dir)
                paths = AppPaths(root / "config", root / "data", root / "state")
                paths.config_dir.mkdir(parents=True)
                shutil.copy2(config_file(self.paths), config_file(paths))
                runtime_path = RuntimeBackend(paths).render_runtime()
                output_text = f"验证通过\n隔离运行配置: {runtime_path}\n当前运行配置未修改。"
            self._call_from_subscription_thread(self._finish_validate_config, output_text)
        except Exception as e:
            self._call_from_subscription_thread(self._fail_subscription_command, f"验证失败: {e}")

    def _finish_validate_config(self, output_text: str) -> None:
        if not self.is_mounted:
            self._subscription_running = False
            return
        self.query_one("#sub-output", TextArea).load_text(output_text)
        self._set_subscription_busy(False)

    def _call_from_subscription_thread(self, callback, *args) -> None:
        try:
            self.app.call_from_thread(callback, *args)
        except Exception:
            self._subscription_running = False

    def _set_subscription_busy(self, busy: bool) -> None:
        self._subscription_running = busy
        for selector in ("#btn-sub-preview", "#btn-sub-apply", "#btn-sub-update"):
            self.query_one(selector, Button).disabled = busy
