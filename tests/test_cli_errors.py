"""错误路径的体验契约：中文报错、拼错给建议、stderr 着色。

这些都是不碰 API 的路径（argparse 与纯函数），所以可以放心断言文案。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
SRC_DIR = ROOT_DIR / "src"


def _run(home: Path, *args: str, color: str = "never") -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(SRC_DIR)
    env["HOME"] = str(home)
    env["CPROXY_COLOR"] = color
    env.pop("NO_COLOR", None)
    return subprocess.run(
        [sys.executable, "-m", "cproxy.cli", *args],
        capture_output=True,
        text=True,
        cwd=ROOT_DIR,
        env=env,
    )


def test_typo_suggests_close_command(tmp_path):
    result = _run(tmp_path, "statu")

    assert result.returncode == 2
    assert "无效的命令: statu" in result.stderr
    assert "您是不是想输入 status?" in result.stderr


def test_typo_does_not_dump_all_commands(tmp_path):
    """痛点回归：以前拼错会把全部 34 个命令名整个列一遍。"""
    stderr = _run(tmp_path, "statu").stderr

    assert "probe-stable-node" not in stderr
    assert "migrate-from-legacy" not in stderr


def test_unknown_command_points_to_help(tmp_path):
    result = _run(tmp_path, "zzzzzz")

    assert result.returncode == 2
    assert "无效的命令: zzzzzz" in result.stderr
    assert "cproxy --help" in result.stderr
    # 凑不出建议时也不该罗列全部命令
    assert "list-groups" not in result.stderr


def test_missing_required_args_is_chinese(tmp_path):
    # 用 test-group（group 仍是必需 positional）；switch 无参数已改为进交互选择，
    # 其非 TTY 降级路径由 tests/test_selector.py 覆盖
    result = _run(tmp_path, "test-group")

    assert result.returncode == 2
    assert "缺少必需参数" in result.stderr
    assert "group" in result.stderr


def test_invalid_choice_on_option_suggests(tmp_path):
    result = _run(tmp_path, "probe-stable-node", "--profile", "codexx")

    assert result.returncode == 2
    assert "参数 --profile 取值无效" in result.stderr
    assert "您是不是想输入 codex?" in result.stderr


def test_invalid_int_value_is_chinese(tmp_path):
    result = _run(tmp_path, "status", "--top", "abc")

    assert result.returncode == 2
    assert "需要整数" in result.stderr


def test_unrecognized_argument_is_chinese(tmp_path):
    result = _run(tmp_path, "start", "--nope")

    assert result.returncode == 2
    assert "无法识别的参数" in result.stderr


# --- stderr 着色：直接测出口函数，避开对 API 的依赖 ---


def test_print_error_colors_whole_message(monkeypatch, capsys):
    """整条消息包裹 ANSI，而不是只染「错误: 」前缀。

    只染前缀会把复位码插进 `错误: ...` 这个子串中间，打爆按子串断言的测试与脚本。
    """
    monkeypatch.setenv("CPROXY_COLOR", "always")
    from cproxy.cli_render import print_error

    print_error("错误: Mihomo API 不可访问")

    stderr = capsys.readouterr().err
    assert stderr.startswith("\x1b[31m")
    assert "错误: Mihomo API 不可访问" in stderr


def test_print_error_plain_when_color_never(monkeypatch, capsys):
    monkeypatch.setenv("CPROXY_COLOR", "never")
    from cproxy.cli_render import print_error

    print_error("错误: 测试")

    assert "\x1b[" not in capsys.readouterr().err


def test_print_error_plain_under_no_color(monkeypatch, capsys):
    monkeypatch.delenv("CPROXY_COLOR", raising=False)
    monkeypatch.delenv("FORCE_COLOR", raising=False)
    monkeypatch.setenv("NO_COLOR", "1")
    from cproxy.cli_render import print_error

    print_error("错误: 测试")

    assert "\x1b[" not in capsys.readouterr().err
