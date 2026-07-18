import os
import subprocess
import sys
from pathlib import Path

import yaml

ROOT_DIR = Path(__file__).resolve().parents[1]
SRC_DIR = ROOT_DIR / "src"

VALID_CONFIG = """
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
"""


def _write_config(paths, text: str = VALID_CONFIG) -> None:
    paths.config_dir.mkdir(parents=True, exist_ok=True)
    (paths.config_dir / "config.yaml").write_text(text.strip() + "\n", encoding="utf-8")


def test_render_snapshots_previous_runtime(tmp_path: Path):
    from cproxy.backend.runtime import RuntimeBackend
    from cproxy.config import default_paths, runtime_file
    from cproxy.snapshots import list_snapshots

    paths = default_paths(tmp_path)
    _write_config(paths)

    RuntimeBackend(paths).render_runtime()
    first_runtime = runtime_file(paths).read_text(encoding="utf-8")
    assert list_snapshots(paths) == []

    # 第二次 render 覆盖前应留下第一份运行配置的快照
    _write_config(paths, VALID_CONFIG.replace("mixed-port: 7890", "mixed-port: 7891"))
    RuntimeBackend(paths).render_runtime()

    snapshots = list_snapshots(paths, "runtime")
    assert len(snapshots) == 1
    assert snapshots[0].read_text(encoding="utf-8") == first_runtime
    assert (snapshots[0].stat().st_mode & 0o777) == 0o600
    assert runtime_file(paths).read_text(encoding="utf-8") != first_runtime


def test_render_skips_snapshot_when_content_unchanged(tmp_path: Path):
    from cproxy.backend.runtime import RuntimeBackend
    from cproxy.config import default_paths, runtime_file
    from cproxy.snapshots import list_snapshots

    paths = default_paths(tmp_path)
    _write_config(paths)

    RuntimeBackend(paths).render_runtime()
    first_runtime = runtime_file(paths).read_text(encoding="utf-8")

    # 配置未变化时再次 render：不重写、不留快照（防止定时任务掏空快照历史）
    RuntimeBackend(paths).render_runtime()

    assert list_snapshots(paths) == []
    assert runtime_file(paths).read_text(encoding="utf-8") == first_runtime


def test_rollback_restores_previous_runtime(tmp_path: Path):
    from cproxy.backend.runtime import RuntimeBackend
    from cproxy.config import default_paths, runtime_file
    from cproxy.snapshots import list_snapshots, restore_snapshot

    paths = default_paths(tmp_path)
    _write_config(paths)

    RuntimeBackend(paths).render_runtime()
    first_runtime = runtime_file(paths).read_text(encoding="utf-8")
    _write_config(paths, VALID_CONFIG.replace("mixed-port: 7890", "mixed-port: 7891"))
    RuntimeBackend(paths).render_runtime()

    snapshot = list_snapshots(paths, "runtime")[0]
    target = restore_snapshot(paths, snapshot)

    assert target == runtime_file(paths)
    assert target.read_text(encoding="utf-8") == first_runtime


def test_snapshot_retention_keeps_latest_ten(tmp_path: Path):
    from cproxy.backend.runtime import RuntimeBackend
    from cproxy.config import default_paths
    from cproxy.snapshots import SNAPSHOT_KEEP, list_snapshots

    paths = default_paths(tmp_path)
    _write_config(paths)

    for idx in range(SNAPSHOT_KEEP + 2):
        _write_config(paths, VALID_CONFIG.replace("mixed-port: 7890", f"mixed-port: {7900 + idx}"))
        RuntimeBackend(paths).render_runtime()

    assert len(list_snapshots(paths, "runtime")) == SNAPSHOT_KEEP


def test_cli_snapshots_and_rollback(tmp_path: Path):
    env = os.environ.copy()
    env["PYTHONPATH"] = str(SRC_DIR)
    env["HOME"] = str(tmp_path)

    def run_cli(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "cproxy.cli", *args],
            capture_output=True,
            text=True,
            cwd=ROOT_DIR,
            env=env,
        )

    config_dir = tmp_path / ".config" / "cproxy"
    config_dir.mkdir(parents=True)
    (config_dir / "config.yaml").write_text(VALID_CONFIG.strip() + "\n", encoding="utf-8")

    assert run_cli("render").returncode == 0
    (config_dir / "config.yaml").write_text(
        VALID_CONFIG.replace("mixed-port: 7890", "mixed-port: 7891").strip() + "\n",
        encoding="utf-8",
    )
    assert run_cli("render").returncode == 0

    list_result = run_cli("snapshots", "--raw")
    assert list_result.returncode == 0
    snapshot_names = [line for line in list_result.stdout.splitlines() if line.strip()]
    assert len(snapshot_names) == 1
    assert snapshot_names[0].startswith("runtime-")

    runtime_path = tmp_path / ".local" / "share" / "cproxy" / "runtime.yaml"
    second_runtime = yaml.safe_load(runtime_path.read_text(encoding="utf-8"))

    rollback_result = run_cli("rollback")
    assert rollback_result.returncode == 0
    assert "已恢复快照" in rollback_result.stdout
    assert "代理未运行" in rollback_result.stdout

    restored = yaml.safe_load(runtime_path.read_text(encoding="utf-8"))
    assert restored.get("mixed-port") == 7890
    assert restored != second_runtime

    missing = run_cli("rollback", "no-such-snapshot.yaml")
    assert missing.returncode != 0
    assert "快照不存在" in missing.stderr
