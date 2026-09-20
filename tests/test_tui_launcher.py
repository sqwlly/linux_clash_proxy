from __future__ import annotations

import builtins

import pytest

from cproxy import tui_launcher


def test_tui_launcher_explains_how_to_install_missing_textual(monkeypatch, capsys):
    original_import = builtins.__import__

    def fake_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "cproxy.tui.app" or (level == 1 and name == "tui.app"):
            error = ModuleNotFoundError("No module named 'textual'")
            error.name = "textual"
            raise error
        return original_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    with pytest.raises(SystemExit) as exc_info:
        tui_launcher.run_tui()

    assert exc_info.value.code == 1
    error = capsys.readouterr().err
    assert "Textual 尚未安装" in error
    assert "pipx inject cproxy" in error
    assert "clash_proxy[tui]" in error
