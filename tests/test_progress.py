import io

from scripts.progress import ProgressBar, display_width, fit_display


def _progress_lines(output: str) -> list[str]:
    return [line.rstrip("\n") for line in output.split("\r") if line.rstrip("\n")]


def test_progress_bar_renders_single_line_updates_and_finish():
    stream = io.StringIO()
    progress = ProgressBar(total=4, label="探测", stream=stream)

    progress.update(1, "节点 A 120ms")
    progress.update(4, "节点 B 80ms")
    progress.finish("完成")

    output = stream.getvalue()
    assert "探测" in output
    assert "节点 A 120ms" in output
    assert "节点 B 80ms" in output
    assert "完成" in output
    assert "25%" in output
    assert "100%" in output

    line_widths = {display_width(line) for line in _progress_lines(output)}
    assert len(line_widths) == 1
    assert next(iter(line_widths)) <= 70


def test_progress_bar_disabled_is_silent():
    stream = io.StringIO()
    progress = ProgressBar(total=4, label="探测", enabled=False, stream=stream)

    progress.update(1, "节点 A 120ms")
    progress.finish("完成")

    assert stream.getvalue() == ""


def test_fit_display_pads_and_truncates_to_fixed_display_width():
    assert display_width(fit_display("节点 A 120ms", 14)) == 14
    truncated = fit_display("一个非常非常长的代理节点名称 120ms", 16)
    assert display_width(truncated) == 16
    assert "…" in truncated
