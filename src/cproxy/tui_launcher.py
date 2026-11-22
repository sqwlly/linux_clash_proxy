from __future__ import annotations

import sys

from .config import AppPaths


def run_tui(paths: AppPaths | None = None) -> None:
    try:
        from .tui.app import run_tui as _run_tui
    except ModuleNotFoundError as exc:
        missing = exc.name or ""
        if missing == "textual" or missing.startswith("textual."):
            print(
                "错误: TUI 可选依赖 Textual 尚未安装。\n"
                "请安装 TUI 可选依赖: pip install 'cproxy[tui]'（或仓库内 pip install '.[tui]'）\n"
                "若使用 pipx，可执行: pipx inject cproxy 'textual>=5.0'",
                file=sys.stderr,
            )
            raise SystemExit(1) from None
        raise
    _run_tui(paths)


def main() -> None:
    run_tui()
