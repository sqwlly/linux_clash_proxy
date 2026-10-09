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


def test_snapshot_deduplication(tmp_path: Path):
    from cproxy.config import default_paths
    from cproxy.snapshots import list_snapshots, snapshot_file

    paths = default_paths(tmp_path)
    cfg_file = tmp_path / "test-config.yaml"
    cfg_file.write_text("dummy: 1\n", encoding="utf-8")

    first = snapshot_file(paths, cfg_file, "config")
    assert first is not None
    assert len(list_snapshots(paths, "config")) == 1

    # 内容未变时再次生成快照：去重跳过，返回同一快照
    second = snapshot_file(paths, cfg_file, "config", deduplicate=True)
    assert second == first
    assert len(list_snapshots(paths, "config")) == 1

    # 内容改变后再次生成快照：生成新快照
    cfg_file.write_text("dummy: 2\n", encoding="utf-8")
    third = snapshot_file(paths, cfg_file, "config", deduplicate=True)
    assert third != first
    assert len(list_snapshots(paths, "config")) == 2


def test_compact_snapshots_tiered_ladder(tmp_path: Path):
    from datetime import datetime, timedelta, timezone

    from cproxy.config import default_paths
    from cproxy.snapshots import compact_snapshots, list_snapshots, snapshots_dir

    paths = default_paths(tmp_path)
    s_dir = snapshots_dir(paths)
    s_dir.mkdir(parents=True, exist_ok=True)

    base_time = datetime(2026, 10, 9, 12, 0, 0, tzinfo=timezone.utc)

    # 10 个近期快照（过去 30 分钟内生成）
    for i in range(10):
        t = base_time - timedelta(minutes=(30 - i * 3))
        (s_dir / f"runtime-{t.strftime('%Y%m%dT%H%M%S.%fZ')}.yaml").write_text(f"port: {7900 + i}\n")

    # 3 个处于 2 小时前的快照（同一小时 10:00 内产生多份）
    for i in range(3):
        t = base_time - timedelta(hours=2, minutes=i * 5)
        (s_dir / f"runtime-{t.strftime('%Y%m%dT%H%M%S.%fZ')}.yaml").write_text(f"h2_{i}: 1\n")

    # 2 个处于 3 天前的快照（同一天内产生多份）
    for i in range(2):
        t = base_time - timedelta(days=3, hours=i)
        (s_dir / f"runtime-{t.strftime('%Y%m%dT%H%M%S.%fZ')}.yaml").write_text(f"d3_{i}: 1\n")

    # 1 个 40 天前过期的快照
    t_expired = base_time - timedelta(days=40)
    (s_dir / f"runtime-{t_expired.strftime('%Y%m%dT%H%M%S.%fZ')}.yaml").write_text("expired: 1\n")

    total_created = 10 + 3 + 2 + 1
    assert len(list_snapshots(paths, "runtime")) == total_created

    # 1. 演练（dry_run）：报告识别出清理目标，但不删除磁盘文件
    dry_report = compact_snapshots(paths, kind="runtime", dry_run=True, now=base_time)
    assert dry_report.dry_run is True
    assert dry_report.total_before == total_created
    assert len(dry_report.pruned) > 0
    assert dry_report.bytes_reclaimed > 0
    assert len(list_snapshots(paths, "runtime")) == total_created

    # 2. 实际执行压缩清理
    real_report = compact_snapshots(paths, kind="runtime", dry_run=False, now=base_time)
    assert real_report.dry_run is False
    assert real_report.total_before == total_created
    assert len(real_report.pruned) == len(dry_report.pruned)
    # 验证磁盘实际只剩下保留的快照
    assert len(list_snapshots(paths, "runtime")) == real_report.total_after
    # 验证 40 天前快照已被删除
    assert not any("20260830" in p.name for p in list_snapshots(paths, "runtime"))


def test_cli_snapshots_compact(tmp_path: Path):
    import json

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

    # 创建两次运行配置产生一个快照
    assert run_cli("render").returncode == 0
    (config_dir / "config.yaml").write_text(
        VALID_CONFIG.replace("mixed-port: 7890", "mixed-port: 7891").strip() + "\n",
        encoding="utf-8",
    )
    assert run_cli("render").returncode == 0

    # 列表 JSON 测试
    list_json = run_cli("snapshots", "--json")
    assert list_json.returncode == 0
    list_data = json.loads(list_json.stdout)
    assert list_data["command"] == "snapshots"
    assert list_data["data"]["count"] == 1
    assert len(list_data["data"]["snapshots"]) == 1

    # 压缩演练 CLI 测试
    compact_dry = run_cli("snapshots", "compact", "--dry-run")
    assert compact_dry.returncode == 0
    assert "演练" in compact_dry.stdout

    # 压缩 JSON 输出测试
    compact_json = run_cli("snapshots", "compact", "--dry-run", "--json")
    assert compact_json.returncode == 0
    compact_data = json.loads(compact_json.stdout)
    assert compact_data["command"] == "snapshots_compact"
    assert compact_data["data"]["dry_run"] is True
    assert compact_data["data"]["total_before"] == 1

    # diff CLI 测试
    diff_cli = run_cli("snapshots", "diff")
    assert diff_cli.returncode == 0
    assert "配置差异对比" in diff_cli.stdout
    assert "-mixed-port: 7891" in diff_cli.stdout or "+mixed-port: 7890" in diff_cli.stdout

    # diff JSON 测试
    diff_json = run_cli("snapshots", "diff", "--json")
    assert diff_json.returncode == 0
    diff_data = json.loads(diff_json.stdout)
    assert diff_data["command"] == "snapshots_diff"
    assert diff_data["data"]["has_diff"] is True
    assert len(diff_data["data"]["diff"]) > 0


def test_interactive_rollback_preview_and_confirm(tmp_path: Path, monkeypatch):
    import io
    from cproxy.backend.runtime import RuntimeBackend
    from cproxy.cli_render import _run_rollback
    from cproxy.config import default_paths, runtime_file

    paths = default_paths(tmp_path)
    _write_config(paths)
    RuntimeBackend(paths).render_runtime()
    _write_config(paths, VALID_CONFIG.replace("mixed-port: 7890", "mixed-port: 7891"))
    RuntimeBackend(paths).render_runtime()

    # 模拟交互式终端环境
    monkeypatch.setattr("cproxy.interactive.interactive_supported", lambda: True)

    # 模拟用户选择第一个快照，并确认回滚 ("y")
    monkeypatch.setattr("cproxy.interactive.select_one", lambda title, items, annotations=None: items[0])
    monkeypatch.setattr("builtins.input", lambda prompt="": "y")

    capture = io.StringIO()
    monkeypatch.setattr("sys.stdout", capture)

    exit_code = _run_rollback(paths, None)
    assert exit_code == 0
    out = capture.getvalue()
    assert "差异预览" in out
    assert "已恢复快照" in out
    # 确认端口回退到 7890
    assert "mixed-port: 7890" in runtime_file(paths).read_text(encoding="utf-8")


