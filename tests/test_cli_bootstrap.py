import os
import subprocess
import sys


def test_python_module_help_runs():
    env = os.environ.copy()
    env["PYTHONPATH"] = "/root/clash_proxy/src"

    result = subprocess.run(
        [sys.executable, "-m", "cproxy.cli", "--help"],
        capture_output=True,
        text=True,
        cwd="/root/clash_proxy",
        env=env,
    )

    assert result.returncode == 0
    assert "cproxy" in result.stdout
