import os
import subprocess
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
SRC_DIR = ROOT_DIR / "src"


def test_python_module_help_runs():
    env = os.environ.copy()
    env["PYTHONPATH"] = str(SRC_DIR)

    result = subprocess.run(
        [sys.executable, "-m", "cproxy.cli", "--help"],
        capture_output=True,
        text=True,
        cwd=ROOT_DIR,
        env=env,
    )

    assert result.returncode == 0
    assert "cproxy" in result.stdout
