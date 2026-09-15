"""顶层 help 与「裸 cproxy」引导的契约测试。

这两处是用户对新 CLI 的第一印象，所以断言得具体些：命令要按功能分组、
usage 行不再把 34 个命令名罗列两遍、文案是中文。
"""

from __future__ import annotations

import os
import subprocess
import sys
from argparse import _SubParsersAction
from pathlib import Path

from cproxy.output import _COMMAND_GROUPS, _COMMAND_HELP

ROOT_DIR = Path(__file__).resolve().parents[1]
SRC_DIR = ROOT_DIR / "src"


def _run(home: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(SRC_DIR)
    env["HOME"] = str(home)
    env["CPROXY_COLOR"] = "never"  # 断言文案时不受 ANSI 干扰
    return subprocess.run(
        [sys.executable, "-m", "cproxy.cli", *args],
        capture_output=True,
        text=True,
        cwd=ROOT_DIR,
        env=env,
    )


def test_bare_cproxy_prints_grouped_overview(tmp_path):
    """不带参数不再静默退出：给出分组清单与下一步提示。"""
    result = _run(tmp_path)

    assert result.returncode == 0
    for group, _ in _COMMAND_GROUPS:
        assert f"{group}：" in result.stdout
    assert "提示: cproxy status" in result.stdout


def test_bare_cproxy_lists_every_command(tmp_path):
    stdout = _run(tmp_path).stdout

    for _, commands in _COMMAND_GROUPS:
        for name, summary in commands:
            assert name in stdout
            assert summary in stdout


def test_usage_does_not_enumerate_commands(tmp_path):
    """痛点回归：usage 行曾是 5 行，把 34 个命令名整个列一遍。"""
    stdout = _run(tmp_path, "--help").stdout
    usage_block = stdout.split("\n\n")[0]

    assert "list-groups" not in usage_block
    assert "migrate-from-legacy" not in usage_block


def test_command_name_appears_exactly_once(tmp_path):
    """命令名不再同时出现在 usage 与 positional 列表两处。"""
    stdout = _run(tmp_path, "--help").stdout

    assert stdout.count("list-groups") == 1
    assert stdout.count("migrate-from-legacy") == 1


def test_root_help_has_chinese_section_titles(tmp_path):
    stdout = _run(tmp_path, "--help").stdout

    assert "用法: cproxy" in stdout
    assert "选项:" in stdout
    assert "usage:" not in stdout
    assert "options:" not in stdout
    assert "positional arguments:" not in stdout


def test_subcommand_help_is_chinese(tmp_path):
    result = _run(tmp_path, "status", "--help")

    assert result.returncode == 0
    assert "用法: cproxy status" in result.stdout
    assert _COMMAND_HELP["status"] in result.stdout


def test_command_table_matches_registered_subcommands():
    """命令表与真实注册的子命令必须同集合，防「加了命令忘了登记」。"""
    from cproxy.output import build_root_parser

    registered = {
        name
        for action in build_root_parser()._actions
        if isinstance(action, _SubParsersAction)
        for name in action.choices
    }

    assert registered == set(_COMMAND_HELP)
