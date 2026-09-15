"""交互式选择器与 `switch` 缺参接入的测试。

交互路径通过 `keys=` 注入按键流来测，因此不需要真终端；终端相关的行为
（tcsetattr 恢复）用 monkeypatch 断言。
"""

from __future__ import annotations

from argparse import Namespace

import pytest

from cproxy.backend.models import ProxyGroup
from cproxy.cli import _resolve_switch
from cproxy.interactive import NotATerminalError, _window, select_one


class _FakeStdin:
    def isatty(self) -> bool:
        return True

    def fileno(self) -> int:
        return 0


class _FakeService:
    def __init__(self, groups: list[ProxyGroup] | None = None):
        self._groups = groups or [_group("G1", "Selector", ["node-1", "node-2"])]

    def list_groups(self):
        return list(self._groups)

    def get_group(self, name: str) -> ProxyGroup:
        for group in self._groups:
            if group.name == name:
                return group
        raise AssertionError(f"未预期的分组: {name}")


def _group(name: str, group_type: str, candidates: list[str]) -> ProxyGroup:
    return ProxyGroup(name=name, type=group_type, current=candidates[0], candidates=list(candidates))


# --- 选择器核心行为（注入按键流）---


def test_enter_confirms_first_item():
    assert select_one("标题", ["a", "b", "c"], keys=["\r"]) == "a"


def test_j_and_down_move_selection():
    assert select_one("标题", ["a", "b", "c"], keys=["j", "\r"]) == "b"
    assert select_one("标题", ["a", "b", "c"], keys=["\x1b[B", "\x1b[B", "\r"]) == "c"


def test_k_and_up_move_selection():
    assert select_one("标题", ["a", "b", "c"], keys=["j", "k", "\r"]) == "a"
    # 首项再上移绕到末尾
    assert select_one("标题", ["a", "b", "c"], keys=["\x1b[A", "\r"]) == "c"


def test_selection_wraps_forward():
    assert select_one("标题", ["a", "b"], keys=["j", "j", "\r"]) == "a"


@pytest.mark.parametrize("key", ["q", "\x1b", "\x03"])
def test_cancel_keys_return_none(key):
    assert select_one("标题", ["a", "b"], keys=[key]) is None


def test_keyboard_interrupt_is_treated_as_cancel():
    def keys():
        yield "j"
        raise KeyboardInterrupt

    assert select_one("标题", ["a", "b"], keys=keys()) is None


def test_exhausted_key_stream_is_cancel():
    assert select_one("标题", ["a", "b"], keys=[]) is None


def test_empty_items_returns_none():
    assert select_one("标题", [], keys=["\r"]) is None


def test_current_item_is_preselected():
    assert select_one("标题", ["a", "b", "c"], current="b", keys=["\r"]) == "b"


def test_unknown_current_falls_back_to_first():
    assert select_one("标题", ["a", "b"], current="不存在", keys=["\r"]) == "a"


def test_non_tty_raises_not_a_terminal(monkeypatch):
    monkeypatch.setattr("cproxy.interactive.sys.stdin", __import__("io").StringIO())
    monkeypatch.setattr("cproxy.interactive.sys.stdout", __import__("io").StringIO())

    with pytest.raises(NotATerminalError):
        select_one("标题", ["a"])


# --- 窗口计算 ---


def test_window_shows_everything_when_short():
    assert _window(5, 2) == (0, 5)


def test_window_keeps_current_visible():
    start, end = _window(100, 50)
    assert end - start == 12
    assert start <= 50 < end


def test_window_clamps_at_both_edges():
    assert _window(100, 0)[0] == 0
    assert _window(100, 99)[1] == 100


# --- 终端状态恢复 ---


def test_terminal_is_restored_even_on_exception(monkeypatch):
    """异常路径必须还原 termios，否则用户的终端会留在 cbreak 模式。"""
    restored: list[object] = []
    monkeypatch.setattr("cproxy.interactive.interactive_supported", lambda: True)
    monkeypatch.setattr("cproxy.interactive.sys.stdin", _FakeStdin())
    monkeypatch.setattr("cproxy.interactive.termios.tcgetattr", lambda fd: ["saved-attrs"])
    monkeypatch.setattr("cproxy.interactive.termios.tcsetattr", lambda fd, when, attrs: restored.append(attrs))
    monkeypatch.setattr("cproxy.interactive.tty.setcbreak", lambda fd: None)
    monkeypatch.setattr("cproxy.interactive._write", lambda text: None)

    def boom(fd: int, size: int) -> bytes:
        raise RuntimeError("读取按键失败")

    monkeypatch.setattr("cproxy.interactive.os.read", boom)

    with pytest.raises(RuntimeError):
        select_one("标题", ["a", "b"])

    assert restored == [["saved-attrs"]]


# --- switch 的接入 ---


def test_both_args_pass_through_without_touching_service():
    """两个参数都给时原样返回——service 传 None 也不会被解引用。"""
    assert _resolve_switch(None, Namespace(group="G", target="N")) == ("G", "N")


def test_single_arg_keeps_previous_error(capsys):
    with pytest.raises(SystemExit) as exc:
        _resolve_switch(None, Namespace(group="G", target=None))

    assert exc.value.code == 2
    assert "缺少必需参数" in capsys.readouterr().err


def test_no_args_uses_selector(monkeypatch):
    monkeypatch.setattr("cproxy.cli.select_one", lambda title, items, **kwargs: items[0])

    assert _resolve_switch(_FakeService(), Namespace(group=None, target=None)) == ("G1", "node-1")


def test_no_args_only_offers_selectable_groups():
    """url-test 组不能手动切换，不该出现在候选里。"""
    service = _FakeService([_group("url", "URLTest", ["n"])])

    with pytest.raises(SystemExit) as exc:
        _resolve_switch(service, Namespace(group=None, target=None))

    assert exc.value.code == 1


def test_cancel_exits_zero(monkeypatch):
    """用户按 q 取消不是错误，不该给非 0 退出码。"""
    monkeypatch.setattr("cproxy.cli.select_one", lambda *args, **kwargs: None)

    with pytest.raises(SystemExit) as exc:
        _resolve_switch(_FakeService(), Namespace(group=None, target=None))

    assert exc.value.code == 0


def test_non_tty_lists_candidates_and_exits_two(monkeypatch, capsys):
    def boom(*args, **kwargs):
        raise NotATerminalError("当前不是交互终端")

    monkeypatch.setattr("cproxy.cli.select_one", boom)

    with pytest.raises(SystemExit) as exc:
        _resolve_switch(_FakeService(), Namespace(group=None, target=None))

    assert exc.value.code == 2
    stderr = capsys.readouterr().err
    assert "不是交互终端" in stderr
    assert "G1" in stderr
