import io

from scripts.progress import ProgressBar


def test_progress_bar_renders_single_line_updates_and_finish():
    stream = io.StringIO()
    progress = ProgressBar(total=4, label="探测", width=8, stream=stream)

    progress.update(1, "节点 A 120ms")
    progress.update(4, "节点 B 80ms")
    progress.finish("完成")

    output = stream.getvalue()
    assert "探测 | 节点 A 120ms" in output
    assert "探测 | 节点 B 80ms" in output
    assert "探测 | 完成" in output
    assert "1/4" in output
    assert "4/4" in output


def test_progress_bar_disabled_is_silent():
    stream = io.StringIO()
    progress = ProgressBar(total=4, label="探测", enabled=False, stream=stream)

    progress.update(1, "节点 A 120ms")
    progress.finish("完成")

    assert stream.getvalue() == ""
