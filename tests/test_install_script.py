import os
import subprocess
from pathlib import Path

import tomllib


def _write_fake_python(fake_python: Path, python_log: Path, bootstrap_message: str = "一键部署: 完成") -> None:
    fake_python.write_text(
        f"""#!/bin/bash
printf '%s\n' "$*" >> "{python_log}"
if [ "$1" = "-m" ] && [ "$2" = "pip" ]; then
  exit 0
fi
if [ "$1" = "-m" ] && [ "$2" = "cproxy.cli" ] && [ "$3" = "init" ]; then
  mkdir -p "$HOME/.config/cproxy"
  cat > "$HOME/.config/cproxy/config.yaml" <<'EOF'
mixed-port: 7890
EOF
  exit 0
fi
if [ "$1" = "-m" ] && [ "$2" = "cproxy.cli" ] && [ "$3" = "bootstrap" ]; then
  echo "{bootstrap_message}"
  exit 0
fi
exit 1
""",
        encoding="utf-8",
    )
    fake_python.chmod(0o755)


def _write_fake_crontab(fake_crontab: Path, crontab_store: Path) -> None:
    fake_crontab.write_text(
        f"""#!/bin/bash
set -euo pipefail
store="{crontab_store}"
if [ "${{1:-}}" = "-l" ]; then
  if [ -f "$store" ]; then
    cat "$store"
    exit 0
  fi
  exit 1
fi
cat > "$store"
""",
        encoding="utf-8",
    )
    fake_crontab.chmod(0o755)


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
    crontab_store = tmp_path / "crontab.txt"
    fake_crontab = fake_bin / "crontab"
    _write_fake_crontab(fake_crontab, crontab_store)
    logrotate_dir = tmp_path / "logrotate.d"
    logrotate_dir.mkdir()

    env = os.environ.copy()
    env["HOME"] = str(tmp_path)
    env["PATH"] = f"{fake_bin}:{env['PATH']}"
    env["PYTHONPATH"] = "/root/clash_proxy/src"
    env["CPROXY_LOGROTATE_DIR"] = str(logrotate_dir)

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
    assert "未检测到 GeoIP 数据文件" in result.stderr
    assert str(tmp_path / ".local" / "share" / "cproxy" / "country.mmdb") in result.stderr


def test_install_script_falls_back_to_user_pip_when_pipx_missing(tmp_path: Path):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    python_log = tmp_path / "python.log"
    fake_python = fake_bin / "python3"
    _write_fake_python(fake_python, python_log)
    crontab_store = tmp_path / "crontab.txt"
    fake_crontab = fake_bin / "crontab"
    _write_fake_crontab(fake_crontab, crontab_store)
    logrotate_dir = tmp_path / "logrotate.d"
    logrotate_dir.mkdir()

    env = os.environ.copy()
    env["HOME"] = str(tmp_path)
    env["PATH"] = f"{fake_bin}:{env['PATH']}"
    env["PYTHONPATH"] = "/root/clash_proxy/src"
    env["CPROXY_LOGROTATE_DIR"] = str(logrotate_dir)

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
    assert "未检测到 GeoIP 数据文件" in result.stderr
    assert str(tmp_path / ".local" / "share" / "cproxy" / "country.mmdb") in result.stderr


def test_pyproject_declares_runtime_dependencies():
    data = tomllib.loads(Path("/root/clash_proxy/pyproject.toml").read_text(encoding="utf-8"))
    dependencies = data["project"].get("dependencies", [])
    assert "PyYAML>=6" in dependencies


def test_install_script_writes_valid_logrotate_configs(tmp_path: Path):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    python_log = tmp_path / "python.log"
    fake_python = fake_bin / "python3"
    _write_fake_python(fake_python, python_log)

    crontab_store = tmp_path / "crontab.txt"
    fake_crontab = fake_bin / "crontab"
    _write_fake_crontab(fake_crontab, crontab_store)

    logrotate_dir = tmp_path / "logrotate.d"
    logrotate_dir.mkdir()

    env = os.environ.copy()
    env["HOME"] = str(tmp_path)
    env["PATH"] = f"{fake_bin}:{env['PATH']}"
    env["PYTHONPATH"] = "/root/clash_proxy/src"
    env["CPROXY_LOGROTATE_DIR"] = str(logrotate_dir)

    result = subprocess.run(
        ["/bin/bash", "/root/clash_proxy/scripts/install.sh"],
        capture_output=True,
        text=True,
        cwd="/root/clash_proxy",
        env=env,
    )

    cproxy_conf = logrotate_dir / "cproxy"
    legacy_conf = logrotate_dir / "clash_proxy"

    assert result.returncode == 0
    assert cproxy_conf.is_file()
    assert legacy_conf.is_file()
    cproxy_text = cproxy_conf.read_text(encoding="utf-8")
    legacy_text = legacy_conf.read_text(encoding="utf-8")
    assert "copytruncate" in cproxy_text
    assert "postrotate" not in cproxy_text
    assert "create postrotate" not in legacy_text
    assert "postrotate" in legacy_text

    cproxy_check = subprocess.run(
        ["logrotate", "-d", str(cproxy_conf)],
        capture_output=True,
        text=True,
        cwd="/root/clash_proxy",
        env=env,
    )
    legacy_check = subprocess.run(
        ["logrotate", "-d", str(legacy_conf)],
        capture_output=True,
        text=True,
        cwd="/root/clash_proxy",
        env=env,
    )

    assert cproxy_check.returncode == 0, cproxy_check.stderr
    assert legacy_check.returncode == 0, legacy_check.stderr


def test_pyproject_declares_subscription_downloader_dependencies():
    data = tomllib.loads(Path("/root/clash_proxy/pyproject.toml").read_text(encoding="utf-8"))
    dependencies = data["project"].get("dependencies", [])
    assert "requests>=2" in dependencies
