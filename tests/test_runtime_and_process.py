import os
import subprocess
import sys
import time
from pathlib import Path


def test_render_creates_runtime_and_status_reports_not_running(tmp_path: Path):
    env = os.environ.copy()
    env["PYTHONPATH"] = "/root/clash_proxy/src"
    env["HOME"] = str(tmp_path)

    config_dir = tmp_path / ".config" / "cproxy"
    config_dir.mkdir(parents=True)
    (config_dir / "config.yaml").write_text(
        """
mixed-port: 7890
external-controller: 127.0.0.1:9090
proxy-groups:
  - name: SSRDOG
    type: select
    proxies:
      - Auto
      - DIRECT
  - name: Auto
    type: fallback
    proxies:
      - ProxyA
  - name: 🇺🇸 United States
    type: select
    proxies:
      - 🇺🇸 United States丨01
  - name: 🇸🇬 Singapore
    type: select
    proxies:
      - 🇸🇬 Singapore丨01
rules:
  - RULE-SET,ChinaMax,DIRECT
  - MATCH,SSRDOG
        """.strip()
        + "\n",
        encoding="utf-8",
    )

    render_result = subprocess.run(
        [sys.executable, "-m", "cproxy.cli", "render"],
        capture_output=True,
        text=True,
        cwd="/root/clash_proxy",
        env=env,
    )

    runtime_file = tmp_path / ".local" / "share" / "cproxy" / "runtime.yaml"
    runtime_text = runtime_file.read_text(encoding="utf-8") if runtime_file.exists() else ""

    assert render_result.returncode == 0
    assert runtime_file.is_file()
    assert "name: AI-MANUAL" in runtime_text
    assert "name: AI-AUTO" in runtime_text
    assert "DOMAIN-SUFFIX,openai.com,AI-MANUAL" in runtime_text
    assert "GEOIP,CN,DIRECT,no-resolve" in runtime_text

    status_result = subprocess.run(
        [sys.executable, "-m", "cproxy.cli", "status"],
        capture_output=True,
        text=True,
        cwd="/root/clash_proxy",
        env=env,
    )

    assert status_result.returncode == 0
    assert "运行摘要" in status_result.stdout
    assert "状态: 未运行" in status_result.stdout
    assert "运行配置状态: 已就绪" in status_result.stdout


def test_start_stop_restart_manage_user_process(tmp_path: Path):
    env = os.environ.copy()
    env["PYTHONPATH"] = "/root/clash_proxy/src"
    env["HOME"] = str(tmp_path)

    fake_bin = tmp_path / "fake-mihomo.sh"
    args_log = tmp_path / "args.log"
    fake_bin.write_text(
        f"""#!/bin/bash
printf '%s\n' "$@" > "{args_log}"
trap 'exit 0' TERM INT
while true; do
  sleep 1
done
""",
        encoding="utf-8",
    )
    fake_bin.chmod(0o755)

    config_dir = tmp_path / ".config" / "cproxy"
    config_dir.mkdir(parents=True)
    (config_dir / "config.yaml").write_text(
        f"""
mixed-port: 7890
external-controller: 127.0.0.1:9090
program-path: {fake_bin}
proxy-groups:
  - name: SSRDOG
    type: select
    proxies:
      - Auto
      - DIRECT
  - name: Auto
    type: fallback
    proxies:
      - ProxyA
  - name: 🇺🇸 United States
    type: select
    proxies:
      - 🇺🇸 United States丨01
  - name: 🇸🇬 Singapore
    type: select
    proxies:
      - 🇸🇬 Singapore丨01
rules:
  - RULE-SET,ChinaMax,DIRECT
  - MATCH,SSRDOG
        """.strip()
        + "\n",
        encoding="utf-8",
    )

    render_result = subprocess.run(
        [sys.executable, "-m", "cproxy.cli", "render"],
        capture_output=True,
        text=True,
        cwd="/root/clash_proxy",
        env=env,
    )
    assert render_result.returncode == 0

    start_result = subprocess.run(
        [sys.executable, "-m", "cproxy.cli", "start"],
        capture_output=True,
        text=True,
        cwd="/root/clash_proxy",
        env=env,
    )
    assert start_result.returncode == 0
    assert "代理已启动" in start_result.stdout

    status_running = subprocess.run(
        [sys.executable, "-m", "cproxy.cli", "status", "--raw"],
        capture_output=True,
        text=True,
        cwd="/root/clash_proxy",
        env=env,
    )
    assert status_running.returncode == 0
    assert "状态: 运行中" in status_running.stdout
    assert args_log.is_file()

    restart_result = subprocess.run(
        [sys.executable, "-m", "cproxy.cli", "restart"],
        capture_output=True,
        text=True,
        cwd="/root/clash_proxy",
        env=env,
    )
    assert restart_result.returncode == 0
    assert "代理已启动" in restart_result.stdout

    stop_result = subprocess.run(
        [sys.executable, "-m", "cproxy.cli", "stop"],
        capture_output=True,
        text=True,
        cwd="/root/clash_proxy",
        env=env,
    )
    assert stop_result.returncode == 0
    assert "代理已停止" in stop_result.stdout

    status_stopped = subprocess.run(
        [sys.executable, "-m", "cproxy.cli", "status", "--raw"],
        capture_output=True,
        text=True,
        cwd="/root/clash_proxy",
        env=env,
    )
    assert status_stopped.returncode == 0
    assert "状态: 未运行" in status_stopped.stdout


def test_stop_does_not_kill_unowned_process_from_stale_pidfile(tmp_path: Path):
    env = os.environ.copy()
    env["PYTHONPATH"] = "/root/clash_proxy/src"
    env["HOME"] = str(tmp_path)

    state_dir = tmp_path / ".local" / "state" / "cproxy"
    state_dir.mkdir(parents=True)

    sleeper = subprocess.Popen(["/bin/sh", "-c", "sleep 30"])
    try:
        (state_dir / "cproxy.pid").write_text(f"{sleeper.pid}\n", encoding="utf-8")

        stop_result = subprocess.run(
            [sys.executable, "-m", "cproxy.cli", "stop"],
            capture_output=True,
            text=True,
            cwd="/root/clash_proxy",
            env=env,
        )

        time.sleep(0.2)
        assert stop_result.returncode != 0
        assert "不属于 cproxy 管理的进程" in stop_result.stderr
        assert sleeper.poll() is None
    finally:
        sleeper.terminate()
        sleeper.wait(timeout=5)
