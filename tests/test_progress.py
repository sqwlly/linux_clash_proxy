import io
import re

from scripts.progress import ProgressBar, style_text

ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _progress_lines(output: str) -> list[str]:
    return [line.rstrip("\n") for line in output.split("\r") if line.rstrip("\n")]


def _strip_ansi(value: str) -> str:
    return ANSI_RE.sub("", value)


def test_progress_bar_renders_plain_left_aligned_updates(monkeypatch):
    monkeypatch.setenv("CPROXY_COLOR", "always")
    monkeypatch.setenv("FORCE_COLOR", "1")
    stream = io.StringIO()
    progress = ProgressBar(total=15, label="轮 1/5", count_width=2, stream=stream)

    progress.update(1, "节点 A 120ms")
    progress.update(15, "节点 B 80ms")
    progress.finish("完成")

    output = stream.getvalue()
    plain_lines = [_strip_ansi(line) for line in _progress_lines(output)]
    assert "轮 1/5" in output
    assert "\x1b[" not in output
    assert "████████████" in output
    assert "节点 A 120ms" not in output
    assert "节点 B 80ms" not in output
    assert "完成" not in output
    assert "15/15" in output
    assert "100%" in output
    assert all(line.startswith("轮 1/5 |") for line in plain_lines)
    assert all(not line.rsplit("| ", 1)[1].startswith(" ") for line in plain_lines)

    line_widths = {len(line) for line in plain_lines}
    assert len(line_widths) == 1
    assert next(iter(line_widths)) <= 36


def test_style_text_keeps_progress_copy_plain(monkeypatch):
    monkeypatch.setenv("CPROXY_COLOR", "always")
    monkeypatch.setenv("FORCE_COLOR", "1")

    assert style_text("完成 | 摘要如下", "green") == "完成 | 摘要如下"


def test_progress_bar_disabled_is_silent():
    stream = io.StringIO()
    progress = ProgressBar(total=4, label="探测", enabled=False, stream=stream)

    progress.update(1, "节点 A 120ms")
    progress.finish("完成")

    assert stream.getvalue() == ""
