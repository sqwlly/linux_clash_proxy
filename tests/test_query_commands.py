import json
import os
import re
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Thread

import yaml

ROOT_DIR = Path(__file__).resolve().parents[1]
SRC_DIR = ROOT_DIR / "src"


class _Handler(BaseHTTPRequestHandler):
    payload = {
        "proxies": {
            "AI-MANUAL": {
                "type": "Selector",
                "now": "AI-AUTO",
                "alive": True,
                "all": ["AI-AUTO", "AI-US", "AI-SG", "🇺🇸 United States", "🇸🇬 Singapore"],
                "history": [],
            },
            "AI-AUTO": {
                "type": "Fallback",
                "now": "AI-US",
                "alive": True,
                "all": ["AI-US", "AI-SG"],
                "history": [],
            },
            "AI-US": {
                "type": "Fallback",
                "now": "🇺🇸 United States丨01",
                "alive": True,
                "all": ["🇺🇸 United States丨01"],
                "history": [{"delay": 95}],
            },
            "AI-SG": {
                "type": "Fallback",
                "now": "🇸🇬 Singapore丨01",
                "alive": True,
                "all": ["🇸🇬 Singapore丨01"],
                "history": [{"delay": 99}],
            },
            "AI-GEMINI": {
                "type": "Fallback",
                "now": "🇰🇷 Korea丨01",
                "alive": True,
                "all": ["🇰🇷 Korea丨01"],
                "history": [{"delay": 88}],
            },
            "🇺🇸 United States": {
                "type": "Selector",
                "now": "🇺🇸 United States丨01",
                "alive": True,
                "all": ["🇺🇸 United States丨01"],
                "history": [{"delay": 96}],
            },
            "🇸🇬 Singapore": {
                "type": "Selector",
                "now": "🇸🇬 Singapore丨01",
                "alive": True,
                "all": ["🇸🇬 Singapore丨01"],
                "history": [{"delay": 97}],
            },
        }
    }

    def do_GET(self):
        if self.path == "/version":
            self._send({"version": "test"})
            return
        if self.path == "/proxies":
            self._send(self.payload)
            return
        self.send_response(404)
        self.end_headers()

    def _send(self, data):
        body = json.dumps(data).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        return


class _ProxyHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "http://probe.local/chatgpt":
            self._send(200, "ok")
            return
        if self.path == "http://probe.local/openai-api":
            self._send(502, "bad gateway")
            return
        if self.path == "http://probe.local/gemini":
            self._send(403, "need key")
            return
        self.send_response(404)
        self.end_headers()

    def _send(self, status: int, body_text: str):
        body = body_text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        return


def _table_cells(out: str) -> list[list[str]]:
    """把表格行还原成「单元格文本」列表。

    表格带行列分隔线、列宽由内容决定，直接断言整行会写死空格与边框字形；
    这里只取单元格值，既精确（能断言空单元格、列值）又不受排版微调影响。
    """
    rows = []
    for line in out.splitlines():
        if "│" not in line:
            continue
        rows.append([cell.strip() for cell in line.strip().strip("│").split("│")])
    return rows


def _run(env, *args):
    return subprocess.run(
        [sys.executable, "-m", "cproxy.cli", *args],
        capture_output=True,
        text=True,
        cwd=ROOT_DIR,
        env=env,
    )


def test_query_commands_use_api_output(tmp_path: Path):
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    proxy_server = HTTPServer(("127.0.0.1", 0), _ProxyHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    proxy_thread = Thread(target=proxy_server.serve_forever, daemon=True)
    thread.start()
    proxy_thread.start()

    try:
        config_dir = tmp_path / ".config" / "cproxy"
        config_dir.mkdir(parents=True)
        (config_dir / "config.yaml").write_text(
            f"external-controller: 127.0.0.1:{server.server_port}\n"
            f"mixed-port: {proxy_server.server_port}\n"
            "ai-chatgpt-url: http://probe.local/chatgpt\n"
            "ai-openai-api-url: http://probe.local/openai-api\n"
            "ai-gemini-api-url: http://probe.local/gemini\n",
            encoding="utf-8",
        )

        env = os.environ.copy()
        env["PYTHONPATH"] = str(SRC_DIR)
        env["HOME"] = str(tmp_path)

        current_result = _run(env, "current", "AI-MANUAL")
        assert current_result.returncode == 0
        assert "当前选择: AI-AUTO" in current_result.stdout

        current_raw = _run(env, "current", "AI-MANUAL", "--raw")
        assert current_raw.returncode == 0
        assert current_raw.stdout.strip() == "AI-AUTO"

        groups_result = _run(env, "list-groups")
        assert groups_result.returncode == 0
        assert "摘要" in groups_result.stdout
        assert "列表" in groups_result.stdout
        assert "当前选择" in groups_result.stdout
        assert "AI-MANUAL" in groups_result.stdout
        assert "AI 出口" in groups_result.stdout

        alias_result = _run(env, "group")
        assert alias_result.returncode == 0
        assert "AI 出口" in alias_result.stdout

        nodes_result = _run(env, "list-nodes", "AI-MANUAL")
        assert nodes_result.returncode == 0
        assert "摘要" in nodes_result.stdout
        assert "列表" in nodes_result.stdout
        assert "当前选择: AI-AUTO" in nodes_result.stdout

        display_nodes = _run(env, "list-nodes", "AI 出口")
        assert display_nodes.returncode == 0
        assert "目标组: AI 出口" in display_nodes.stdout
        assert "内部组: AI-MANUAL" in display_nodes.stdout

        untitled = _run(env, "list-nodes")
        assert untitled.returncode == 0
        assert "AI 出口" in untitled.stdout
        assert "cproxy list-nodes 'AI 出口'" in untitled.stdout

        ai_status_result = _run(env, "ai-status")
        assert ai_status_result.returncode == 0
        out = ai_status_result.stdout
        rows = _table_cells(out)
        # 三张表：出口 / 连通性 / 候选池（表头即契约）
        assert "AI 出口（自动切换）" in out
        assert ["角色", "池", "当前节点", "延迟", "状态"] in rows
        # 出口必须是解析后的**真实节点**（旧实现在 AI-MANUAL 直接钉住节点时会退化成 "-"）
        assert ["通用", "AI-US", "United States 01", "95ms", "正常"] in rows
        assert ["备用", "AI-SG", "Singapore 01", "99ms", "正常"] in rows
        assert ["Gemini", "AI-GEMINI", "Korea 01", "88ms", "正常"] in rows
        assert "连通性（探测 部分异常）" in out
        assert ["状态", "目标", "地址", "详情"] in rows
        # 探测成功时不带 detail：HTTP 401/403 是「可达但需鉴权」，会被读成故障码
        assert ["正常", "ChatGPT Web", "http://probe.local/chatgpt", ""] in rows
        assert ["正常", "Gemini API", "http://probe.local/gemini", ""] in rows
        # 失败行才带 detail
        assert ["失败", "OpenAI API", "http://probe.local/openai-api", "HTTP 502"] in rows
        # 旧「链路」区块已移除，链路信息并入「AI 出口」表
        assert "链路" not in out
        # 候选池取代旧「分组」：只讲拓扑（无延迟列），自动池要标出当前项是区域组指针
        assert ["池", "类型", "候选", "当前指向"] in rows
        assert ["AI-AUTO", "自动池", "2", "AI-US → United States 01"] in rows
        assert ["AI-MANUAL", "直连池", "5", "United States 01"] in rows
        assert "ms" not in out.split("候选池", 1)[1]  # 候选池不列延迟

        ai_status_raw = _run(env, "ai-status", "--raw")
        assert ai_status_raw.returncode == 0
        assert "AI-MANUAL: type=Selector now=AI-AUTO" in ai_status_raw.stdout
        assert "AI-GEMINI: type=Fallback now=🇰🇷 Korea丨01" in ai_status_raw.stdout
        # delay 取出口节点的真实延迟：fixed fallback 组自身 history 为空，旧实现打成 None
        assert "delay=88" in ai_status_raw.stdout
        assert "AI-MODE: auto 池=AI-US" in ai_status_raw.stdout
        assert "AI-EXIT: node=🇺🇸 United States丨01 delay=95 alive=True" in ai_status_raw.stdout
        assert "AI-STANDBY: group=AI-SG node=🇸🇬 Singapore丨01 delay=99 alive=True" in ai_status_raw.stdout
        assert "AI-PROBE: 部分异常" in ai_status_raw.stdout

        ai_status_json = _run(env, "ai-status", "--json")
        # 夹具里有一个探测失败 ⇒ 机器可读模式以退出码 1 + ok=false 上报（人类面板恒返回 0）
        assert ai_status_json.returncode == 1
        payload = json.loads(ai_status_json.stdout)
        assert payload["schema_version"] == 1
        assert payload["ok"] is False
        assert payload["recommended_actions"] == ["cproxy incident"]
        route = payload["data"]["route"]
        assert route["auto_mode"] is True
        assert route["mode_label"] == "自动切换"
        assert route["active_group"] == "AI-US"
        assert route["active_node"] == "🇺🇸 United States丨01"
        assert route["active_delay"] == 95
        assert route["standby_group"] == "AI-SG"
        assert route["gemini_present"] is True
        assert route["gemini_node"] == "🇰🇷 Korea丨01"
        assert route["gemini_delay"] == 88
        assert payload["data"]["probe_status"] == "部分异常"
    finally:
        server.shutdown()
        thread.join()
        proxy_server.shutdown()
        proxy_thread.join()


def test_ai_status_panel_labels_timeout_and_unmeasured_delays(monkeypatch, tmp_path, capsys):
    """延迟的哨兵值必须如实标注：

    生产实测 AI-US 组的 history 是 `[5000, 861, 5000, 267]`、AI-SG 是 `[644, 641, 0, 0]`。
    `5000` 是 mihomo 的探测**超时**哨兵、`0` 是**未测得**——都不是真实延迟。此前按毫秒
    展示，会把「备用节点其实在超时」渲染成「5000ms · 正常」，读起来像又快又健康。
    """
    from cproxy import cli_render
    from cproxy.backend.models import AIProbeReport, AIProbeResult, ProxyGroup

    def group(name, now, candidates=(), delay=None, alive=True):
        return ProxyGroup(
            name=name, type="Fallback", current=now, candidates=list(candidates), alive=alive, delay=delay
        )

    kr = "🇰🇷韩国 01 | IEPL 1.7X | CM"
    kr2 = "🇰🇷韩国 02 | 1X"
    groups = {
        "AI-MANUAL": group("AI-MANUAL", kr, ["AI-AUTO", kr], delay=None),
        "AI-AUTO": group("AI-AUTO", "AI-US", ["AI-US"], delay=0),
        # 备用：组自身健康检查连续超时（哨兵 5000）
        "AI-US": group("AI-US", "🇺🇸美国 01 | 1X", ["🇺🇸美国 01 | 1X"], delay=5000),
        "AI-SG": group("AI-SG", "🇸🇬新加坡 01 | 1X", ["🇸🇬新加坡 01 | 1X"], delay=0, alive=False),
        # Gemini：组自身无 history，落到的出口节点本轮未测得（0）
        "AI-GEMINI": group("AI-GEMINI", kr2, [kr2], delay=None),
        kr: ProxyGroup(name=kr, type="Vless", current="-", candidates=[], alive=True, delay=140),
        kr2: ProxyGroup(name=kr2, type="Vless", current="-", candidates=[], alive=True, delay=0),
    }
    monkeypatch.setattr(
        cli_render,
        "run_ai_probe",
        lambda paths: AIProbeReport(
            results=[AIProbeResult("ChatGPT Web", "https://chatgpt.com", True, "HTTP 200")]
        ),
    )

    assert cli_render._render_ai_status(groups, raw=False) == 0
    out = capsys.readouterr().out
    rows = _table_cells(out)

    # 非自动池的措辞：出口由 mihomo 在直连候选池里自动择优，「手动」是与行为不符的说法
    assert "AI 出口（兜底 · 直连池）" in out
    assert "兜底 · 手动" not in out
    # 哨兵如实标注：5000 → 超时、0 → 未测得，都不能按毫秒展示。
    # 状态列为**空**——延迟非 ok 时再并排一个健康标记会读成「超时 · 正常」。
    assert ["备用", "AI-US", "🇺🇸美国 01 1X", "超时", ""] in rows
    assert ["Gemini", "AI-GEMINI", "🇰🇷韩国 02 1X", "未测得", ""] in rows
    assert "5000ms" not in out
    # 注意用边界：`140ms` 里也含 `0ms` 子串，要的是「独立成值的 0ms」
    assert re.search(r"(?<!\d)0ms", out) is None
    # ok 的那条仍完整显示延迟与健康（出口从节点自身取到 140ms）
    assert ["通用", "AI-MANUAL", "🇰🇷韩国 01 IEPL 1.7X CM", "140ms", "正常"] in rows
    # 候选池只讲拓扑，不列延迟——避免同一节点在出口表与候选池出现两个口径的数
    assert ["AI-MANUAL", "直连池", "2", "🇰🇷韩国 01 IEPL 1.7X CM"] in rows
    assert ["AI-AUTO", "自动池", "1", "AI-US → 🇺🇸美国 01 1X"] in rows
    assert "ms" not in out.split("候选池", 1)[1]


def test_query_commands_fall_back_to_runtime_when_api_unavailable(tmp_path: Path):
    config_dir = tmp_path / ".config" / "cproxy"
    data_dir = tmp_path / ".local" / "share" / "cproxy"
    config_dir.mkdir(parents=True)
    data_dir.mkdir(parents=True)

    (config_dir / "config.yaml").write_text(
        "external-controller: 127.0.0.1:9\n"
        "mixed-port: 7890\n",
        encoding="utf-8",
    )
    runtime = {
        "proxy-groups": [
            {
                "name": "AI-MANUAL",
                "type": "select",
                "proxies": ["AI-AUTO", "AI-US", "AI-SG"],
            },
            {
                "name": "AI-AUTO",
                "type": "fallback",
                "proxies": ["AI-US", "AI-SG"],
            },
            {
                "name": "AI-US",
                "type": "fallback",
                "proxies": ["🇺🇸 United States丨01"],
            },
            {
                "name": "AI-SG",
                "type": "fallback",
                "proxies": ["🇸🇬 Singapore丨01"],
            },
        ]
    }
    (data_dir / "runtime.yaml").write_text(yaml.safe_dump(runtime, allow_unicode=True, sort_keys=False), encoding="utf-8")

    env = os.environ.copy()
    env["PYTHONPATH"] = str(SRC_DIR)
    env["HOME"] = str(tmp_path)

    groups_result = _run(env, "list-groups")
    assert groups_result.returncode == 0
    assert "摘要" in groups_result.stdout
    assert "AI-MANUAL" in groups_result.stdout
    assert "AI-AUTO" in groups_result.stdout

    nodes_result = _run(env, "list-nodes", "AI-MANUAL")
    assert nodes_result.returncode == 0
    assert "列表" in nodes_result.stdout
    assert "当前选择: AI-AUTO" in nodes_result.stdout

    current_result = _run(env, "current", "AI-MANUAL")
    assert current_result.returncode == 0
    assert "当前选择: AI-AUTO" in current_result.stdout


def test_ai_commands_report_friendly_error_when_api_unavailable(tmp_path: Path):
    config_dir = tmp_path / ".config" / "cproxy"
    config_dir.mkdir(parents=True)
    (config_dir / "config.yaml").write_text(
        "external-controller: 127.0.0.1:9\n"
        "mixed-port: 7890\n",
        encoding="utf-8",
    )

    env = os.environ.copy()
    env["PYTHONPATH"] = str(SRC_DIR)
    env["HOME"] = str(tmp_path)

    ai_status_result = _run(env, "ai-status")
    assert ai_status_result.returncode != 0
    assert "错误: Mihomo API 不可访问" in ai_status_result.stderr

    test_group_result = _run(env, "test-group", "AI-AUTO")
    assert test_group_result.returncode != 0
    assert "错误: Mihomo API 不可访问" in test_group_result.stderr
