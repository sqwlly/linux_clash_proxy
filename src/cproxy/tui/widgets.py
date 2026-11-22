from __future__ import annotations

from inspect import isawaitable
from typing import Any, Callable

from textual import events
from textual.binding import Binding
from textual.widget import Widget
from textual.widgets import DataTable, Input, TextArea


def escape_markup(value: object) -> str:
    """转义动态文本中的 Rich/Textual 标记字符，防止 MarkupError 或 `[tag]` 被剥离。"""
    text = str(value).replace("[", r"\[")
    if text.endswith("\\"):
        text += " "
    return text


def coerce_worker_error(exc: BaseException) -> Exception:
    """将工作线程中捕获的 Exception / SystemExit 转为可读 Exception，防止杀掉 TUI 进程。"""
    if isinstance(exc, Exception):
        return exc
    if isinstance(exc, SystemExit):
        detail = exc.code if isinstance(exc.code, str) and exc.code.strip() else f"操作中断 (exit {exc.code})"
        return RuntimeError(detail)
    return RuntimeError(str(exc))


def safe_call_from_thread(widget: Widget, callback: Callable[..., Any], *args: Any) -> bool:
    """在工作线程中安全回调主线程；组件已卸载或应用正在关闭时静默忽略。"""
    if not widget.is_mounted:
        return False
    try:
        widget.app.call_from_thread(callback, *args)
    except Exception:
        return False
    return True


class NavigationInput(Input):
    BINDINGS = [*Input.BINDINGS, Binding("escape", "app.back", show=False, priority=True)]

    async def _on_key(self, event: events.Key) -> None:
        if event.key in {"escape", "esc"}:
            res = self.app.action_back()
            if isawaitable(res):
                await res
            event.stop()
            return
        result = super()._on_key(event)
        if isawaitable(result):
            await result


class NavigationTextArea(TextArea):
    BINDINGS = [*TextArea.BINDINGS, Binding("escape", "app.back", show=False, priority=True)]

    async def _on_key(self, event: events.Key) -> None:
        if event.key in {"escape", "esc"}:
            res = self.app.action_back()
            if isawaitable(res):
                await res
            event.stop()
            return
        result = super()._on_key(event)
        if isawaitable(result):
            await result


class NavigationDataTable(DataTable):
    navigation_next_handler: Callable[[], None] | None = None
    navigation_previous_handler: Callable[[], None] | None = None

    async def _on_key(self, event: events.Key) -> None:
        if event.key == "right":
            self._focus_next()
            event.stop()
            return
        if event.key == "left":
            self._focus_previous()
            event.stop()
            return
        if event.key == "down" and self._at_last_row():
            self._focus_next()
            event.stop()
            return
        if event.key == "up" and self._at_first_row():
            self._focus_previous()
            event.stop()
            return

        result = super()._on_key(event)
        if isawaitable(result):
            await result

    def _at_first_row(self) -> bool:
        return self.row_count <= 0 or self.cursor_row is None or self.cursor_row <= 0

    def _at_last_row(self) -> bool:
        return self.row_count <= 0 or self.cursor_row is None or self.cursor_row >= self.row_count - 1

    def _focus_next(self) -> None:
        if self.navigation_next_handler is not None:
            self.navigation_next_handler()
            return
        self.app.action_focus_next()

    def _focus_previous(self) -> None:
        if self.navigation_previous_handler is not None:
            self.navigation_previous_handler()
            return
        self.app.action_focus_previous()
