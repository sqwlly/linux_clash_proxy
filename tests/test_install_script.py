import os
import subprocess
import sys
from pathlib import Path

import tomllib

ROOT_DIR = Path(__file__).resolve().parents[1]
SRC_DIR = ROOT_DIR / "src"


def _write_fake_python(fake_python: Path, python_log: Path, bootstrap_message: str = "一键部署: 完成") -> None:
    real_python = sys.executable
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
exec "{real_python}" "$@"
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
    env["PYTHONPATH"] = str(SRC_DIR)
    env["CPROXY_LOGROTATE_DIR"] = str(logrotate_dir)
    env["BINDIR"] = str(tmp_path / "system-bin")
    env["LIBDIR"] = str(tmp_path / "system-lib")
    env["DEFAULT_TMPDIR"] = str(tmp_path / "system-tmp")

    result = subprocess.run(
        ["/bin/bash", str(ROOT_DIR / "scripts" / "install.sh")],
        capture_output=True,
        text=True,
        cwd=ROOT_DIR,
        env=env,
    )

    config_file = tmp_path / ".config" / "cproxy" / "config.yaml"
    assert result.returncode == 0
    assert config_file.is_file()
    assert pipx_log.is_file()
    log_text = pipx_log.read_text(encoding="utf-8")
    assert f"install --force --editable {ROOT_DIR}" in log_text
    assert "安装完成" in result.stdout
    country_mmdb = tmp_path / ".local" / "share" / "cproxy" / "country.mmdb"
    assert country_mmdb.is_file()
    assert f"GeoIP 数据: 已从 {ROOT_DIR}/Country.mmdb 安装到" in result.stdout
    assert str(country_mmdb) in result.stdout
    assert "未检测到 GeoIP 数据文件" not in result.stderr
    assert (tmp_path / "system-bin" / "clash-proxy").is_file()
    assert (tmp_path / "system-bin" / "clash-proxy-update").is_file()
    assert not (tmp_path / "system-bin" / "cproxy").exists()
    assert (tmp_path / "system-lib" / "probe_stable_node.py").is_file()
    assert "系统命令安装: 完成" in result.stdout


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
    env["PYTHONPATH"] = str(SRC_DIR)
    env["CPROXY_LOGROTATE_DIR"] = str(logrotate_dir)
    env["BINDIR"] = str(tmp_path / "system-bin")
    env["LIBDIR"] = str(tmp_path / "system-lib")
    env["DEFAULT_TMPDIR"] = str(tmp_path / "system-tmp")

    result = subprocess.run(
        ["/bin/bash", str(ROOT_DIR / "scripts" / "install.sh")],
        capture_output=True,
        text=True,
        cwd=ROOT_DIR,
        env=env,
    )

    config_file = tmp_path / ".config" / "cproxy" / "config.yaml"
    assert result.returncode == 0
    assert config_file.is_file()
    assert python_log.is_file()
    log_text = python_log.read_text(encoding="utf-8")
    assert f"-m pip install --user --editable {ROOT_DIR}" in log_text
    assert "安装完成" in result.stdout
    country_mmdb = tmp_path / ".local" / "share" / "cproxy" / "country.mmdb"
    assert country_mmdb.is_file()
    assert f"GeoIP 数据: 已从 {ROOT_DIR}/Country.mmdb 安装到" in result.stdout
    assert str(country_mmdb) in result.stdout
    assert "未检测到 GeoIP 数据文件" not in result.stderr
    assert (tmp_path / "system-bin" / "clash-proxy").is_file()
    assert (tmp_path / "system-bin" / "clash-proxy-update").is_file()
    assert not (tmp_path / "system-bin" / "cproxy").exists()
    assert (tmp_path / "system-lib" / "probe_stable_node.py").is_file()
    assert "系统命令安装: 完成" in result.stdout


def test_pyproject_declares_runtime_dependencies():
    data = tomllib.loads((ROOT_DIR / "pyproject.toml").read_text(encoding="utf-8"))
    dependencies = data["project"].get("dependencies", [])
    assert "PyYAML>=6" in dependencies
    assert "tqdm>=4" in dependencies
    assert "urllib3>=2.7.0" in dependencies
    assert "idna>=3.15" in dependencies


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
    env["PYTHONPATH"] = str(SRC_DIR)
    env["CPROXY_LOGROTATE_DIR"] = str(logrotate_dir)
    env["BINDIR"] = str(tmp_path / "system-bin")
    env["LIBDIR"] = str(tmp_path / "system-lib")
    env["DEFAULT_TMPDIR"] = str(tmp_path / "system-tmp")

    result = subprocess.run(
        ["/bin/bash", str(ROOT_DIR / "scripts" / "install.sh")],
        capture_output=True,
        text=True,
        cwd=ROOT_DIR,
        env=env,
    )

    cproxy_conf = logrotate_dir / "cproxy"
    legacy_conf = logrotate_dir / "clash_proxy"

    assert result.returncode == 0
    assert cproxy_conf.is_file()
    assert legacy_conf.is_file()
    cproxy_text = cproxy_conf.read_text(encoding="utf-8")
    legacy_text = legacy_conf.read_text(encoding="utf-8")
    assert str(tmp_path / ".local" / "state" / "cproxy" / "cproxy.log") in cproxy_text
    assert str(tmp_path / ".local" / "state" / "clash_proxy" / "clash.log") in legacy_text
    assert "copytruncate" in cproxy_text
    assert "postrotate" not in cproxy_text
    assert "copytruncate" in legacy_text
    assert "postrotate" not in legacy_text

    cproxy_check = subprocess.run(
        ["logrotate", "-d", str(cproxy_conf)],
        capture_output=True,
        text=True,
        cwd=ROOT_DIR,
        env=env,
    )
    legacy_check = subprocess.run(
        ["logrotate", "-d", str(legacy_conf)],
        capture_output=True,
        text=True,
        cwd=ROOT_DIR,
        env=env,
    )

    assert cproxy_check.returncode == 0, cproxy_check.stderr
    assert legacy_check.returncode == 0, legacy_check.stderr


def test_pyproject_declares_subscription_downloader_dependencies():
    data = tomllib.loads((ROOT_DIR / "pyproject.toml").read_text(encoding="utf-8"))
    dependencies = data["project"].get("dependencies", [])
    assert "requests>=2" in dependencies


def test_pyproject_packages_tui_stylesheet():
    data = tomllib.loads((ROOT_DIR / "pyproject.toml").read_text(encoding="utf-8"))
    package_data = data["tool"]["setuptools"]["package-data"]
    assert package_data["cproxy.tui"] == ["styles.tcss"]
