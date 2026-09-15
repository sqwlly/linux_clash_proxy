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


def _write_fake_id(fake_bin: Path, uid: int) -> None:
    """伪造 id 命令——install.sh 用 `id -u` 判断是否 root。

    测试必须显式指定 uid：CI 与开发机常常就是 root，不 mock 的话会走到
    系统级分支，把「pipx / --user 回退」两个用例的断言打翻。
    """
    fake_id = fake_bin / "id"
    fake_id.write_text(
        f"""#!/bin/bash
if [ "${{1:-}}" = "-u" ]; then
  echo {uid}
  exit 0
fi
exec /usr/bin/id "$@"
""",
        encoding="utf-8",
    )
    fake_id.chmod(0o755)


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
    python_log = tmp_path / "python.log"
    fake_python = fake_bin / "python3"
    _write_fake_python(fake_python, python_log)
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
    # 显式模拟非 root：否则在 root 环境（CI/开发机）会走系统级分支，
    # 与本用例要验证的 pipx / --user 回退路径不符
    _write_fake_id(fake_bin, 1000)
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
    assert python_log.is_file()
    log_text = pipx_log.read_text(encoding="utf-8")
    python_log_text = python_log.read_text(encoding="utf-8")
    assert f"install --force --editable {ROOT_DIR}" in log_text
    assert "-m cproxy.cli init" in python_log_text
    assert "-m cproxy.cli bootstrap" in python_log_text
    assert not (tmp_path / ".local" / "state" / "cproxy" / "cproxy.pid").exists()
    assert "安装完成" in result.stdout
    country_mmdb = tmp_path / ".local" / "share" / "cproxy" / "country.mmdb"
    assert country_mmdb.is_file()
    assert f"GeoIP 数据: 已从 {ROOT_DIR}/Country.mmdb 安装到" in result.stdout
    assert str(country_mmdb) in result.stdout
    assert "未检测到 GeoIP 数据文件" not in result.stderr
    # legacy 入口（clash-proxy / clash-proxy-update）已停用，安装脚本默认不再安装；
    # 确需回滚时由 CPROXY_INSTALL_SYSTEM_COMMANDS=legacy 显式触发（该分支的安装逻辑
    # 由 tests/system_command_installer_test.sh 覆盖）
    assert not (tmp_path / "system-bin" / "clash-proxy").exists()
    assert not (tmp_path / "system-bin" / "cproxy").exists()
    assert not (tmp_path / "system-lib").exists()
    assert "系统命令安装: 已跳过（legacy 入口已停用" in result.stdout


def test_install_script_falls_back_to_user_pip_when_pipx_missing(tmp_path: Path):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    python_log = tmp_path / "python.log"
    fake_python = fake_bin / "python3"
    _write_fake_python(fake_python, python_log)
    # 显式模拟非 root：否则在 root 环境（CI/开发机）会走系统级分支，
    # 与本用例要验证的 pipx / --user 回退路径不符
    _write_fake_id(fake_bin, 1000)
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
    assert "-m cproxy.cli init" in log_text
    assert "-m cproxy.cli bootstrap" in log_text
    assert not (tmp_path / ".local" / "state" / "cproxy" / "cproxy.pid").exists()
    assert "安装完成" in result.stdout
    country_mmdb = tmp_path / ".local" / "share" / "cproxy" / "country.mmdb"
    assert country_mmdb.is_file()
    assert f"GeoIP 数据: 已从 {ROOT_DIR}/Country.mmdb 安装到" in result.stdout
    assert str(country_mmdb) in result.stdout
    assert "未检测到 GeoIP 数据文件" not in result.stderr
    # legacy 入口（clash-proxy / clash-proxy-update）已停用，安装脚本默认不再安装；
    # 确需回滚时由 CPROXY_INSTALL_SYSTEM_COMMANDS=legacy 显式触发（该分支的安装逻辑
    # 由 tests/system_command_installer_test.sh 覆盖）
    assert not (tmp_path / "system-bin" / "clash-proxy").exists()
    assert not (tmp_path / "system-bin" / "cproxy").exists()
    assert not (tmp_path / "system-lib").exists()
    assert "系统命令安装: 已跳过（legacy 入口已停用" in result.stdout


def _install_env(tmp_path: Path, fake_bin: Path, logrotate_dir: Path) -> dict:
    env = os.environ.copy()
    env["HOME"] = str(tmp_path)
    env["PATH"] = f"{fake_bin}:{env['PATH']}"
    env["PYTHONPATH"] = str(SRC_DIR)
    env["CPROXY_LOGROTATE_DIR"] = str(logrotate_dir)
    env["BINDIR"] = str(tmp_path / "system-bin")
    env["LIBDIR"] = str(tmp_path / "system-lib")
    env["CPROXY_EDITABLE"] = "0"
    return env


def test_install_script_uses_system_wide_install_when_root(tmp_path: Path):
    """root 下必须走系统级安装，不落 --user 副本。

    `cproxy.service` 硬编码 `/usr/local/bin/cproxy`，而 PATH 里 `~/.local/bin`
    排在 `/usr/local/bin` 之前；root 若也装一份 --user 副本，交互命令与服务就会
    跑不同版本的代码（STATUS.md 记录过这种跨副本遮蔽）。
    """
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    python_log = tmp_path / "python.log"
    _write_fake_python(fake_bin / "python3", python_log)
    _write_fake_id(fake_bin, 0)
    _write_fake_crontab(fake_bin / "crontab", tmp_path / "crontab.txt")
    logrotate_dir = tmp_path / "logrotate.d"
    logrotate_dir.mkdir()

    result = subprocess.run(
        ["/bin/bash", str(ROOT_DIR / "scripts" / "install.sh")],
        capture_output=True,
        text=True,
        cwd=ROOT_DIR,
        env=_install_env(tmp_path, fake_bin, logrotate_dir),
    )

    assert result.returncode == 0
    assert "系统级安装" in result.stdout

    pip_lines = [line for line in python_log.read_text(encoding="utf-8").splitlines() if "-m pip" in line]
    assert pip_lines, "root 路径下应至少调用一次 pip"
    for line in pip_lines:
        assert "--user" not in line, f"root 下不应使用 --user：{line}"
    assert any("--force-reinstall" in line and "--no-deps" in line for line in pip_lines), (
        "非 editable 的系统级安装应带 --force-reinstall --no-deps"
    )


def test_root_branch_takes_precedence_over_pipx(tmp_path: Path):
    """root 下即便装了 pipx 也不该用它——pipx 同样会产生用户级副本。"""
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_fake_python(fake_bin / "python3", tmp_path / "python.log")
    _write_fake_id(fake_bin, 0)
    _write_fake_crontab(fake_bin / "crontab", tmp_path / "crontab.txt")
    pipx_log = tmp_path / "pipx.log"
    fake_pipx = fake_bin / "pipx"
    fake_pipx.write_text(f'#!/bin/bash\nprintf \'%s\\n\' "$*" >> "{pipx_log}"\nexit 0\n', encoding="utf-8")
    fake_pipx.chmod(0o755)
    logrotate_dir = tmp_path / "logrotate.d"
    logrotate_dir.mkdir()

    result = subprocess.run(
        ["/bin/bash", str(ROOT_DIR / "scripts" / "install.sh")],
        capture_output=True,
        text=True,
        cwd=ROOT_DIR,
        env=_install_env(tmp_path, fake_bin, logrotate_dir),
    )

    assert result.returncode == 0
    assert not pipx_log.exists(), "root 分支应优先于 pipx，不应调用 pipx"
    assert "系统级安装" in result.stdout


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

    # 显式模拟非 root：否则在 root 环境（CI/开发机）会走系统级分支，
    # 与本用例要验证的 pipx / --user 回退路径不符
    _write_fake_id(fake_bin, 1000)
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
