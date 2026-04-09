import os
import subprocess
from pathlib import Path

import tomllib


def test_install_script_prefers_pipx_and_initializes_user_layout(tmp_path: Path):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    pipx_log = tmp_path / "pipx.log"
    fake_pipx = fake_bin / "pipx"
    fake_pipx.write_text(
        f"""#!/bin/bash
printf '%s\n' "$*" >> "{pipx_log}"
exit 0
""",
        encoding="utf-8",
    )
    fake_pipx.chmod(0o755)

    env = os.environ.copy()
    env["HOME"] = str(tmp_path)
    env["PATH"] = f"{fake_bin}:{env['PATH']}"
    env["PYTHONPATH"] = "/root/clash_proxy/src"

    result = subprocess.run(
        ["/bin/bash", "/root/clash_proxy/scripts/install.sh"],
        capture_output=True,
        text=True,
        cwd="/root/clash_proxy",
        env=env,
    )

    config_file = tmp_path / ".config" / "cproxy" / "config.yaml"
    assert result.returncode == 0
    assert config_file.is_file()
    assert pipx_log.is_file()
    log_text = pipx_log.read_text(encoding="utf-8")
    assert "install --force --editable /root/clash_proxy" in log_text
    assert "安装完成" in result.stdout


def test_install_script_falls_back_to_user_pip_when_pipx_missing(tmp_path: Path):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    python_log = tmp_path / "python.log"
    fake_python = fake_bin / "python3"
    fake_python.write_text(
        f"""#!/bin/bash
printf '%s\n' "$*" >> "{python_log}"
if [ "$1" = "-m" ] && [ "$2" = "pip" ]; then
  exit 0
fi
if [ "$1" = "-m" ] && [ "$2" = "cproxy.cli" ] && [ "$3" = "init" ]; then
  exec "{os.sys.executable}" "$@"
fi
exit 1
""",
        encoding="utf-8",
    )
    fake_python.chmod(0o755)

    env = os.environ.copy()
    env["HOME"] = str(tmp_path)
    env["PATH"] = f"{fake_bin}"
    env["PYTHONPATH"] = "/root/clash_proxy/src"

    result = subprocess.run(
        ["/bin/bash", "/root/clash_proxy/scripts/install.sh"],
        capture_output=True,
        text=True,
        cwd="/root/clash_proxy",
        env=env,
    )

    config_file = tmp_path / ".config" / "cproxy" / "config.yaml"
    assert result.returncode == 0
    assert config_file.is_file()
    assert python_log.is_file()
    log_text = python_log.read_text(encoding="utf-8")
    assert "-m pip install --user --editable /root/clash_proxy" in log_text
    assert "安装完成" in result.stdout


def test_pyproject_declares_runtime_dependencies():
    data = tomllib.loads(Path("/root/clash_proxy/pyproject.toml").read_text(encoding="utf-8"))
    dependencies = data["project"].get("dependencies", [])
    assert "PyYAML>=6" in dependencies
