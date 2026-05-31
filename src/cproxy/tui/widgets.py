from __future__ import annotations

from inspect import isawaitable

from textual import events
from textual.binding import Binding
from textual.widgets import DataTable, Input, TextArea


class NavigationInput(Input):
    BINDINGS = [*Input.BINDINGS, Binding("escape", "app.back", show=False, priority=True)]

    async def _on_key(self, event: events.Key) -> None:
        if event.key in {"escape", "esc"}:
            self.app.action_back()
            event.stop()
            return
        result = super()._on_key(event)
        if isawaitable(result):
            await result


class NavigationTextArea(TextArea):
    BINDINGS = [*TextArea.BINDINGS, Binding("escape", "app.back", show=False, priority=True)]

    async def _on_key(self, event: events.Key) -> None:
        if event.key in {"escape", "esc"}:
            self.app.action_back()
            event.stop()
            return
        result = super()._on_key(event)
        if isawaitable(result):
            await result


class NavigationDataTable(DataTable):
    async def _on_key(self, event: events.Key) -> None:
        if event.key == "right":
            self.app.action_focus_next()
            event.stop()
            return
        if event.key == "left":
            self.app.action_focus_previous()
            event.stop()
            return
        if event.key == "down" and self._at_last_row():
            self.app.action_focus_next()
            event.stop()
            return
        if event.key == "up" and self._at_first_row():
            self.app.action_focus_previous()
            event.stop()
            return

        result = super()._on_key(event)
        if isawaitable(result):
            await result

    def _at_first_row(self) -> bool:
        return self.row_count <= 0 or self.cursor_row is None or self.cursor_row <= 0

    def _at_last_row(self) -> bool:
        return self.row_count <= 0 or self.cursor_row is None or self.cursor_row >= self.row_count - 1
