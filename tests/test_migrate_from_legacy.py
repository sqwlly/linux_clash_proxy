import os
import subprocess
import sys
from pathlib import Path


def test_migrate_from_legacy_copies_config_only(tmp_path: Path):
    legacy_dir = tmp_path / "legacy"
    legacy_dir.mkdir()
    (legacy_dir / "config.yaml").write_text("mixed-port: 7890\n", encoding="utf-8")
    (legacy_dir / "clash.log").write_text("log\n", encoding="utf-8")
    (legacy_dir / "mihomo.pid").write_text("1234\n", encoding="utf-8")

    env = os.environ.copy()
    env["PYTHONPATH"] = "/root/clash_proxy/src"
    env["HOME"] = str(tmp_path)

    result = subprocess.run(
        [sys.executable, "-m", "cproxy.cli", "migrate-from-legacy", str(legacy_dir)],
        capture_output=True,
        text=True,
        cwd="/root/clash_proxy",
        env=env,
    )

    config_file = tmp_path / ".config" / "cproxy" / "config.yaml"
    state_dir = tmp_path / ".local" / "state" / "cproxy"

    assert result.returncode == 0
    assert config_file.is_file()
    assert config_file.read_text(encoding="utf-8") == "mixed-port: 7890\n"
    assert not (state_dir / "clash.log").exists()
    assert not (state_dir / "mihomo.pid").exists()
