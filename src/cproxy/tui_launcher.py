from __future__ import annotations

import sys

from .config import AppPaths


def run_tui(paths: AppPaths | None = None) -> None:
    try:
        from .tui.app import run_tui as _run_tui
    except ModuleNotFoundError as exc:
        if exc.name == "textual":
            print(
                "错误: TUI 可选依赖 Textual 尚未安装。\n"
                "请执行: pipx inject cproxy 'textual>=5.0'\n"
                "或从仓库安装: pipx install '/path/to/clash_proxy[tui]'",
                file=sys.stderr,
            )
            raise SystemExit(1) from None
        raise
    _run_tui(paths)


def main() -> None:
    run_tui()
