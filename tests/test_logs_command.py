import os
import subprocess
import sys
from pathlib import Path


def test_logs_command_reads_recent_lines(tmp_path: Path):
    env = os.environ.copy()
    env["PYTHONPATH"] = "/root/clash_proxy/src"
    env["HOME"] = str(tmp_path)

    state_dir = tmp_path / ".local" / "state" / "cproxy"
    state_dir.mkdir(parents=True)
    (state_dir / "cproxy.log").write_text(
        "line-1\nline-2\nline-3\n",
        encoding="utf-8",
    )

    result = subprocess.run(
        [sys.executable, "-m", "cproxy.cli", "logs", "--lines", "2"],
        capture_output=True,
        text=True,
        cwd="/root/clash_proxy",
        env=env,
    )

    assert result.returncode == 0
    assert "日志" in result.stdout
    assert "日志文件:" in result.stdout
    assert "line-2" in result.stdout
    assert "line-3" in result.stdout
    assert "line-1" not in result.stdout
