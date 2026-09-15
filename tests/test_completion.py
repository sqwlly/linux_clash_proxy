"""`cproxy __complete` 与 `cproxy completion` 的契约测试。

补全的硬约束只有两条：答得准，以及**永远不卡住 <Tab>**（任何失败都静默为空）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cproxy.backend.models import ProxyGroup
from cproxy.completion import _candidates, run_complete, run_completion
from cproxy.config import default_paths


def _paths(tmp_path: Path):
    return default_paths(tmp_path)


def _group(name: str, group_type: str, candidates: list[str]) -> ProxyGroup:
    return ProxyGroup(name=name, type=group_type, current=candidates[0], candidates=list(candidates))


def _stub_groups(monkeypatch, groups: list[ProxyGroup]) -> None:
    monkeypatch.setattr("cproxy.completion.QueryService.list_groups", lambda self, **kwargs: list(groups))


# --- 静态候选 ---


def test_completes_command_names(tmp_path):
    assert _candidates(_paths(tmp_path), ["cproxy", "sw"], 1) == ["switch"]
    assert "list-groups" in _candidates(_paths(tmp_path), ["cproxy", "list-"], 1)


def test_completes_root_options(tmp_path):
    assert _candidates(_paths(tmp_path), ["cproxy", "--"], 1) == ["--help", "--version"]


def test_completes_command_options(tmp_path):
    options = _candidates(_paths(tmp_path), ["cproxy", "status", "--"], 2)
    assert "--raw" in options and "--top" in options
    # 单字母选项不进候选，避免噪音
    assert "-h" not in options


def test_completes_enum_values(tmp_path):
    paths = _paths(tmp_path)
    assert _candidates(paths, ["cproxy", "probe-stable-node", "--profile", ""], 3) == [
        "codex",
        "chatgpt",
        "github",
        "claude",
    ]
    assert _candidates(paths, ["cproxy", "probe-stable-node", "--strategy", ""], 3) == [
        "conservative",
        "balanced",
        "aggressive",
    ]
    assert _candidates(paths, ["cproxy", "traffic", ""], 2) == ["show", "collect", "audit"]


# --- 动态候选 ---


def test_completes_group_names(tmp_path, monkeypatch):
    _stub_groups(monkeypatch, [_group("AI-MANUAL", "Selector", ["n1"])])

    assert _candidates(_paths(tmp_path), ["cproxy", "switch", ""], 2) == ["AI-MANUAL"]


def test_switch_only_offers_selectable_groups(tmp_path, monkeypatch):
    """switch 只能作用于 selector，补出 url-test 组会让用户白敲一次。"""
    _stub_groups(
        monkeypatch,
        [_group("sel", "Selector", ["a"]), _group("url", "URLTest", ["a"])],
    )
    paths = _paths(tmp_path)

    assert _candidates(paths, ["cproxy", "switch", ""], 2) == ["sel"]
    # current / list-nodes 接受任意组
    assert _candidates(paths, ["cproxy", "current", ""], 2) == ["sel", "url"]


def test_completes_node_names_from_previous_group(tmp_path, monkeypatch):
    _stub_groups(monkeypatch, [_group("G1", "Selector", ["n1", "n2"])])

    assert _candidates(_paths(tmp_path), ["cproxy", "switch", "G1", ""], 3) == ["n1", "n2"]


def test_node_candidates_for_unknown_group_are_empty(tmp_path, monkeypatch):
    _stub_groups(monkeypatch, [_group("G1", "Selector", ["n1"])])

    assert _candidates(_paths(tmp_path), ["cproxy", "switch", "nope", ""], 3) == []


def test_group_flag_value_uses_groups(tmp_path, monkeypatch):
    _stub_groups(monkeypatch, [_group("G1", "Selector", ["n1"])])

    assert _candidates(_paths(tmp_path), ["cproxy", "probe-stable-node", "--group", ""], 3) == ["G1"]


def test_node_flag_uses_preceding_group_flag(tmp_path, monkeypatch):
    _stub_groups(monkeypatch, [_group("G1", "Selector", ["n1", "n2"])])

    words = ["cproxy", "ip-check", "--group", "G1", "--node", ""]
    assert _candidates(_paths(tmp_path), words, 5) == ["n1", "n2"]


def test_prefix_filter_is_case_insensitive(tmp_path):
    assert _candidates(_paths(tmp_path), ["cproxy", "SW"], 1) == ["switch"]


# --- 边界与容错 ---


def test_point_past_last_word_is_empty_prefix(tmp_path, monkeypatch):
    """bash 在光标处没有词时不一定传空串——下标越界必须当作空前缀。

    否则 `cproxy switch `（光标在空白处）会被误判成"正在敲 switch 这个词"。
    """
    _stub_groups(monkeypatch, [_group("G1", "Selector", ["n1"])])

    assert _candidates(_paths(tmp_path), ["cproxy", "switch"], 2) == ["G1"]


def test_api_failure_is_silent_and_exit_zero(tmp_path, monkeypatch, capsys):
    def boom(self, **kwargs):
        raise RuntimeError("api down")

    monkeypatch.setattr("cproxy.completion.QueryService.list_groups", boom)

    assert run_complete(_paths(tmp_path), ["2", "cproxy", "switch", ""]) == 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_malformed_point_is_silent(tmp_path, capsys):
    """下标不是整数时不能崩，也不能吐栈给 shell。"""
    assert run_complete(_paths(tmp_path), ["not-a-number", "cproxy"]) == 0
    assert capsys.readouterr().err == ""


def test_run_complete_prints_one_candidate_per_line(tmp_path, capsys):
    assert run_complete(_paths(tmp_path), ["1", "cproxy", "sw"]) == 0

    assert capsys.readouterr().out == "switch\n"


# --- 隐藏性与脚本分发 ---


def test_complete_command_is_hidden_from_help():
    from cproxy.output import build_root_parser

    help_text = build_root_parser().format_help()
    assert "__complete" not in help_text


def test_completion_command_is_listed(tmp_path, capsys):
    from cproxy.output import _COMMAND_HELP

    assert "completion" in _COMMAND_HELP


@pytest.mark.parametrize("shell", ["bash", "zsh"])
def test_completion_script_uses_our_protocol(shell, capsys):
    assert run_completion(shell, install=False) == 0

    script = capsys.readouterr().out
    assert "cproxy __complete" in script


def test_bash_script_registers_completion(capsys):
    run_completion("bash", install=False)

    script = capsys.readouterr().out
    assert "complete " in script and "_cproxy_complete cproxy" in script


def test_completion_install_writes_standard_path(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))

    assert run_completion("bash", install=True) == 0

    target = tmp_path / "bash-completion" / "completions" / "cproxy"
    assert target.exists()
    assert "cproxy __complete" in target.read_text(encoding="utf-8")
    assert str(target) in capsys.readouterr().out
