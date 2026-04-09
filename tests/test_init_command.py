import os
import subprocess
import sys
from pathlib import Path


def test_init_creates_user_config_layout(tmp_path: Path):
    env = os.environ.copy()
    env["PYTHONPATH"] = "/root/clash_proxy/src"
    env["HOME"] = str(tmp_path)

    result = subprocess.run(
        [sys.executable, "-m", "cproxy.cli", "init"],
        capture_output=True,
        text=True,
        cwd="/root/clash_proxy",
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
