import os
import subprocess
import sys
from pathlib import Path


def _run(env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "cproxy.cli", *args],
        capture_output=True,
        text=True,
        cwd="/root/clash_proxy",
        env=env,
    )


def test_bootstrap_auto_migrates_legacy_and_starts_process(tmp_path: Path):
    fake_bin = tmp_path / "fake-mihomo.sh"
    fake_bin.write_text(
        """#!/bin/bash
trap 'exit 0' TERM INT
while true; do
  sleep 1
done
""",
        encoding="utf-8",
    )
    fake_bin.chmod(0o755)

    legacy_root = tmp_path / "legacy"
    legacy_root.mkdir()
    (legacy_root / "config.yaml").write_text(
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

    env = os.environ.copy()
    env["PYTHONPATH"] = "/root/clash_proxy/src"
    env["HOME"] = str(tmp_path)
    env["CPROXY_LEGACY_ROOT"] = str(legacy_root)

    bootstrap_result = _run(env, "bootstrap")
    assert bootstrap_result.returncode == 0
    assert "一键部署完成" in bootstrap_result.stdout
    assert "已自动迁移旧配置" in bootstrap_result.stdout

    status_result = _run(env, "status", "--raw")
    assert status_result.returncode == 0
    assert "状态: 运行中" in status_result.stdout

    stop_result = _run(env, "stop")
    assert stop_result.returncode == 0
    assert "代理已停止" in stop_result.stdout


def test_bootstrap_fails_when_config_empty_and_no_legacy(tmp_path: Path):
    env = os.environ.copy()
    env["PYTHONPATH"] = "/root/clash_proxy/src"
    env["HOME"] = str(tmp_path)
    env["CPROXY_LEGACY_ROOT"] = str(tmp_path / "missing-legacy")

    result = _run(env, "bootstrap")

    assert result.returncode != 0
    assert "错误: 当前配置为空，且未找到可迁移配置" in result.stderr
