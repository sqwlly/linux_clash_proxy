import os
import subprocess
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
SRC_DIR = ROOT_DIR / "src"


def test_init_creates_user_config_layout(tmp_path: Path):
    env = os.environ.copy()
    env["PYTHONPATH"] = str(SRC_DIR)
    env["HOME"] = str(tmp_path)

    result = subprocess.run(
        [sys.executable, "-m", "cproxy.cli", "init"],
        capture_output=True,
        text=True,
        cwd=ROOT_DIR,
        env=env,
    )

    config_dir = tmp_path / ".config" / "cproxy"
    data_dir = tmp_path / ".local" / "share" / "cproxy"
    state_dir = tmp_path / ".local" / "state" / "cproxy"
    config_file = config_dir / "config.yaml"

    assert result.returncode == 0
    assert config_dir.is_dir()
    assert data_dir.is_dir()
    assert state_dir.is_dir()
    assert config_file.is_file()
    assert "mixed-port:" in config_file.read_text(encoding="utf-8")
