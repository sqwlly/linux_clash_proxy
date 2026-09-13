"""`cproxy status` 的按进程流量归因、退役 parity 指标与路径压缩。"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
SRC_DIR = ROOT_DIR / "src"

# codex 实测路径，用于验证“智能压缩保留辨识尾段”
LONG_CODEX_PATH = (
    "/root/versions/node/v22.23.1/lib/node_modules/@openai/.codex-qWKjdDCx"
    "/node_modules/@openai/codex-linux-x64/vendor/x86_64-unknown-linux-musl/bin/codex"
)


def _write_config(tmp_path: Path) -> None:
    config_dir = tmp_path / ".config" / "cproxy"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "config.yaml").write_text(
        "mixed-port: 7890\nexternal-controller: 127.0.0.1:19090\n",
        encoding="utf-8",
    )


def _seed_today(tmp_path: Path, process_rows: list[tuple[str, str, int, int]]) -> None:
    """写入今日流量样本。

    ``process_rows`` 每项为 ``(process, chain, download, upload)``，同时按
    host 维度写入 ``traffic_samples``，让 status 的流量区块有数据。
    """
    from cproxy.config import default_paths, traffic_db_file
    from cproxy.services.traffic import _SCHEMA

    paths = default_paths(tmp_path)
    db_path = traffic_db_file(paths)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    today = datetime.now().strftime("%Y-%m-%d")

    with sqlite3.connect(db_path) as conn:
        conn.executescript(_SCHEMA)
        for index, (process, chain, download, upload) in enumerate(process_rows):
            conn.execute(
                "INSERT INTO traffic_process_samples VALUES (?,?,?,?,?,?)",
                (today, "10", process, chain, download, upload),
            )
            conn.execute(
                "INSERT INTO traffic_samples VALUES (?,?,?,?,?,?,?)",
                (today, "10", chain, "Match", f"host{index}.example", download, upload),
            )


def _run_status(tmp_path: Path, *args: str) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(SRC_DIR)
    env["HOME"] = str(tmp_path)
    env["CPROXY_COLOR"] = "never"
    return subprocess.run(
        [sys.executable, "-m", "cproxy.cli", "status", *args],
        capture_output=True,
        text=True,
        cwd=ROOT_DIR,
        env=env,
    )


# ---------------------------------------------------------------- 单元：路径压缩


def test_shorten_path_keeps_home_relative_when_it_fits():
    from cproxy.cli_render import _shorten_path

    assert _shorten_path("/usr/bin/curl", 46) == "/usr/bin/curl"
    assert _shorten_path(LONG_CODEX_PATH, 200).startswith("~/")


def test_shorten_path_compresses_long_path_keeping_identifying_tail():
    from cproxy.cli_render import _shorten_path

    shortened = _shorten_path(LONG_CODEX_PATH, 46)
    # 噪声段（versions/node/vX/lib/node_modules/vendor/平台三元组/bin）被跳过
    for noise in ("node_modules", "vendor", "x86_64", "versions"):
        assert noise not in shortened
    # 保留最能区分进程的尾段
    assert shortened.startswith("…/")
    assert shortened.endswith("/codex")
    assert "@openai" in shortened
    assert len(shortened) <= 46


def test_shorten_path_passes_through_non_paths():
    from cproxy.cli_render import _shorten_path

    assert _shorten_path("-", 46) == "-"
    assert _shorten_path("", 46) == ""


def test_shorten_path_clamps_when_last_segment_alone_overflows():
    """末段自身超宽时也必须满足 width 契约（循环只能丢中间段，压不进去）。"""
    from cproxy.cli_render import _display_width, _shorten_path

    out = _shorten_path("/usr/bin/" + "x" * 80, 46)
    assert _display_width(out) <= 46
    assert out.endswith("…")  # 保头部截断


def test_traffic_bar_reserves_width_for_zero_value_rows():
    """0 字节行在表格内需占位，否则该行标签左移、整表错位。"""
    from cproxy.cli_render import _TRAFFIC_BAR_WIDTH, _traffic_bar

    assert _traffic_bar(0, 100) == " " * _TRAFFIC_BAR_WIDTH
    # 行尾场景（pad=False）仍返回空串，不留行尾空白
    assert _traffic_bar(0, 100, pad=False) == ""


def test_chain_traffic_bar_splits_by_proxy_ratio(monkeypatch):
    """双色流量条：条宽按总量、绿段长度按代理占比，0 字节保持占位契约。"""
    import re

    from cproxy.cli_render import ANSI_GREEN, ANSI_RESET, ANSI_YELLOW, _TRAFFIC_BAR_WIDTH, _chain_traffic_bar

    monkeypatch.setenv("CPROXY_COLOR", "always")
    # total=max → 满宽条；proxy:direct=3:1 → 绿 15 格、黄 5 格
    bar = _chain_traffic_bar(300, 100, 400)
    plain = re.sub(r"\x1b\[[0-9;]*m", "", bar)
    assert plain == "█" * _TRAFFIC_BAR_WIDTH
    assert f"{ANSI_GREEN}{'█' * 15}{ANSI_RESET}" in bar
    assert f"{ANSI_YELLOW}{'█' * 5}{ANSI_RESET}" in bar
    # 纯代理/纯直连退化为单色整条
    assert ANSI_YELLOW not in _chain_traffic_bar(400, 0, 400)
    assert ANSI_GREEN not in _chain_traffic_bar(0, 400, 400)
    # 0 字节占位与 pad=False 契约
    assert _chain_traffic_bar(0, 0, 400) == " " * _TRAFFIC_BAR_WIDTH
    assert _chain_traffic_bar(0, 0, 400, pad=False) == ""


def test_process_table_uses_chain_split_bar(monkeypatch, capsys):
    """按进程表的「流量」列用双色条：同一条内绿段=代理、黄段=直连。"""
    from cproxy.cli_render import ANSI_GREEN, ANSI_YELLOW, _chain_split_columns, _process_row_bar, _render_traffic_table
    from cproxy.services.traffic import ProcessTrafficRow

    monkeypatch.setenv("CPROXY_COLOR", "always")
    row = ProcessTrafficRow(label="/usr/bin/x", download=1000, upload=0, proxy_download=250, proxy_upload=0)
    _render_traffic_table(
        [row], lambda r: r.label, header="进程", columns=_chain_split_columns(), total=1000, bar_of=_process_row_bar
    )
    data_line = capsys.readouterr().out.splitlines()[1]
    assert ANSI_GREEN in data_line and ANSI_YELLOW in data_line
    # 绿段在前（代理），黄段紧随（直连）：250B 代理 + 750B 直连
    assert data_line.index(ANSI_GREEN) < data_line.index(ANSI_YELLOW)


def test_process_display_label_is_full_path_without_width():
    from cproxy.cli_render import _process_display_label

    # 不传 width 时维持“完整路径 + 剥离 (deleted)”的既有语义
    assert _process_display_label(f"{LONG_CODEX_PATH} (deleted)") == LONG_CODEX_PATH
    assert _process_display_label(f"{LONG_CODEX_PATH} (deleted)", width=46).endswith("/codex")


# ------------------------------------------------------------------ 单元：格式化


def test_format_uptime():
    from cproxy.cli_render import _format_uptime

    assert _format_uptime(0) == "0h 00m 00s"
    assert _format_uptime(7034) == "1h 57m 14s"
    assert _format_uptime(-5) == "0h 00m 00s"


def test_chain_split_columns_render_proxy_and_direct_separately():
    from cproxy.cli_render import ANSI_CYAN, ANSI_GREEN, ANSI_MAGENTA, ANSI_YELLOW, _chain_split_columns
    from cproxy.services.traffic import ProcessTrafficRow

    row = ProcessTrafficRow(
        label="/usr/bin/x", download=1000, upload=200, proxy_download=300, proxy_upload=40
    )
    rendered = {title: value_of(row) for title, value_of, _ in _chain_split_columns()}
    assert rendered == {
        "代理↓": "300 B",
        "代理↑": "40 B",
        "直连↓": "700 B",
        "直连↑": "160 B",
    }
    # 链路着色契约：代理绿/青、直连黄/洋红，表头即图例
    colors = {title: color for title, _, color in _chain_split_columns()}
    assert colors == {
        "代理↓": ANSI_GREEN,
        "代理↑": ANSI_CYAN,
        "直连↓": ANSI_YELLOW,
        "直连↑": ANSI_MAGENTA,
    }


def test_traffic_renders_chain_colors(monkeypatch, capsys):
    """代理与直连用不同颜色区分：直连行数字黄/洋红、条形黄，代理行维持绿/青。"""
    from cproxy.cli_render import (
        ANSI_CYAN,
        ANSI_GREEN,
        ANSI_MAGENTA,
        ANSI_RESET,
        ANSI_YELLOW,
        _render_traffic_totals,
    )

    monkeypatch.setenv("CPROXY_COLOR", "always")
    traffic = {
        "proxy_download": 1000,
        "proxy_upload": 100,
        "direct_download": 3000,
        "direct_upload": 300,
    }
    _render_traffic_totals(traffic)
    lines = capsys.readouterr().out.splitlines()
    proxy_line = next(line for line in lines if line.startswith("代理"))
    direct_line = next(line for line in lines if line.startswith("直连"))
    assert ANSI_GREEN in proxy_line and ANSI_CYAN in proxy_line
    assert ANSI_YELLOW not in proxy_line and ANSI_MAGENTA not in proxy_line
    assert ANSI_YELLOW in direct_line and ANSI_MAGENTA in direct_line
    # 直连流量条也用黄色（与该行链路色一致），而不是默认绿
    assert ANSI_YELLOW + "█" in direct_line.replace(ANSI_RESET, "")
    assert ANSI_GREEN + "█" not in direct_line.replace(ANSI_RESET, "")


def test_traffic_table_colors_column_headers_as_legend(monkeypatch, capsys):
    """进程表表头单元格带链路色（加粗），数值同色——列色即图例且对齐不受 ANSI 干扰。"""
    from cproxy.cli_render import (
        ANSI_CYAN,
        ANSI_GREEN,
        ANSI_MAGENTA,
        ANSI_YELLOW,
        _chain_split_columns,
        _render_traffic_table,
    )
    from cproxy.services.traffic import ProcessTrafficRow

    monkeypatch.setenv("CPROXY_COLOR", "always")
    row = ProcessTrafficRow(
        label="/usr/bin/x", download=1000, upload=200, proxy_download=300, proxy_upload=40
    )
    _render_traffic_table(
        [row], lambda r: r.label, header="进程", columns=_chain_split_columns(), total=1300
    )
    lines = capsys.readouterr().out.splitlines()
    # 表头与数据行都带全套链路色：代理绿/青、直连黄/洋红
    for line in (lines[0], lines[1]):
        assert ANSI_GREEN in line and ANSI_CYAN in line
        assert ANSI_YELLOW in line and ANSI_MAGENTA in line
    assert lines[0].index("代理↓") < lines[0].index("直连↓")


def test_render_traffic_table_tolerates_empty_rows(capsys):
    """空行集不应崩溃——列宽计算必须能退化为仅表头宽度。"""
    from cproxy.cli_render import _render_traffic_table, _total_size_columns

    _render_traffic_table([], lambda row: row.label, header="主机", columns=_total_size_columns(), total=0)
    out = capsys.readouterr().out
    assert "↓下载" in out and "↑上传" in out and "主机" in out


def test_render_kv_aligns_cjk_labels(capsys):
    from cproxy.cli_render import _render_kv

    _render_kv([("状态", "运行中"), ("运行配置", "已就绪")])
    out = capsys.readouterr().out.splitlines()
    # 值列起点一致：最长标签（8 显示列）+ 间距 4 = 12
    assert out[0] == "状态        运行中"
    assert out[1] == "运行配置    已就绪"


def test_runtime_staleness_requires_both_fingerprints():
    """runtime 路径恒定，只能靠内容指纹判断；任一侧缺失都不得误报“未跟随”。"""
    from cproxy.backend.models import ProcessOwner
    from cproxy.backend.process import ProcessBackend

    owner = ProcessOwner(pid=1, program="mihomo", runtime="/x/runtime.yaml", runtime_hash="aaa")
    assert ProcessBackend._runtime_is_stale(owner, "bbb") is True  # 内容已变
    assert ProcessBackend._runtime_is_stale(owner, "aaa") is False  # 已跟随最近一次 render
    assert ProcessBackend._runtime_is_stale(owner, "") is False  # 当前指纹不可得
    assert ProcessBackend._runtime_is_stale(None, "bbb") is False  # 未运行

    # 旧版 process_meta_file 没有 runtime_hash：不可判定，不提示
    legacy = ProcessOwner(pid=1, program="mihomo", runtime="/x/runtime.yaml")
    assert ProcessBackend._runtime_is_stale(legacy, "bbb") is False


def test_render_kv_skips_empty_values(capsys):
    from cproxy.cli_render import _render_kv

    _render_kv([("连接数", ""), ("日志", "")])
    assert capsys.readouterr().out == ""


# -------------------------------------------------------------- 单元：运行时指标


def test_runtime_metrics_missing_pid_yields_none(tmp_path):
    from cproxy.backend.runtime_metrics import collect_runtime_metrics
    from cproxy.config import default_paths

    metrics = collect_runtime_metrics(default_paths(tmp_path), None)
    assert metrics.uptime_seconds is None
    assert metrics.memory_bytes is None


def test_runtime_metrics_self_process_is_readable(tmp_path):
    from cproxy.backend.runtime_metrics import collect_runtime_metrics
    from cproxy.config import default_paths

    metrics = collect_runtime_metrics(default_paths(tmp_path), os.getpid())
    assert metrics.uptime_seconds is not None and metrics.uptime_seconds >= 0
    assert metrics.memory_bytes is not None and metrics.memory_bytes > 0


# ------------------------------------------------------------ 端到端：status 面板


def test_status_splits_process_traffic_by_chain(tmp_path: Path):
    _write_config(tmp_path)
    _seed_today(
        tmp_path,
        [
            ("/usr/bin/curl", "DIRECT", 800, 80),
            ("/usr/bin/python3", "AI-MANUAL -> Node1", 3000, 150),
            (LONG_CODEX_PATH, "AI-MANUAL -> Node1", 100, 100),
        ],
    )

    result = _run_status(tmp_path)
    assert result.returncode == 0, result.stderr

    out = result.stdout
    assert "进程 Top 3" in out
    # 四列拆分：代理/直连 各自给出上下行字节数
    for column in ("代理↓", "代理↑", "直连↓", "直连↑"):
        assert column in out
    # 全量口径：直连进程同样在列
    assert "/usr/bin/curl" in out
    assert "/usr/bin/python3" in out
    # curl 全直连：字节落在直连两列，代理为 0
    assert "800 B" in out
    assert "80 B" in out
    # python3 全代理：字节落在代理两列（3000 B / 150 B），直连为 0
    assert "2.93 KB" in out
    assert "150 B" in out
    assert "0 B" in out
    # 超长路径被压缩后展示
    assert LONG_CODEX_PATH not in out
    assert "…/@openai/codex-linux-x64/codex" in out


def test_status_no_process_hides_process_table(tmp_path: Path):
    _write_config(tmp_path)
    _seed_today(tmp_path, [("/usr/bin/curl", "DIRECT", 800, 80)])

    assert "进程 Top" in _run_status(tmp_path).stdout
    hidden = _run_status(tmp_path, "--no-process").stdout
    assert "进程 Top" not in hidden
    # 汇总区块仍在
    assert "今日总量" in hidden


def test_status_top_limits_process_rows(tmp_path: Path):
    _write_config(tmp_path)
    _seed_today(
        tmp_path,
        [
            ("/usr/bin/curl", "DIRECT", 9000, 900),
            ("/usr/bin/python3", "AI-MANUAL", 3000, 150),
            ("/usr/bin/awk", "DIRECT", 100, 10),
        ],
    )

    out = _run_status(tmp_path, "--top", "2").stdout
    assert "进程 Top 2" in out
    assert "/usr/bin/curl" in out
    assert "/usr/bin/awk" not in out


def test_status_default_process_top_matches_cli_contract(tmp_path: Path):
    """不带参数时应显示 STATUS_PROCESS_TOP_DEFAULT 行。

    锁的是端到端的有效默认值：argparse 的 default 与渲染层默认参数必须一致，
    否则两处会各自漂移（此前默认值 5 就硬编码在两个文件里）。
    """
    from cproxy.output import STATUS_PROCESS_TOP_DEFAULT

    _write_config(tmp_path)
    _seed_today(
        tmp_path,
        [
            (f"/usr/bin/proc{index}", "DIRECT", 1000 - index, 100)
            for index in range(STATUS_PROCESS_TOP_DEFAULT + 2)
        ],
    )

    out = _run_status(tmp_path).stdout
    assert f"进程 Top {STATUS_PROCESS_TOP_DEFAULT}" in out
    assert f"进程 Top {STATUS_PROCESS_TOP_DEFAULT + 1}" not in out


def test_status_paths_are_home_relative(tmp_path: Path):
    _write_config(tmp_path)
    result = _run_status(tmp_path)
    assert "~/." in result.stdout
    assert str(tmp_path) not in result.stdout
