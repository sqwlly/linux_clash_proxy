import json
import os
import socket
import sqlite3
import subprocess
import sys
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Thread

import pytest

ROOT_DIR = Path(__file__).resolve().parents[1]
SRC_DIR = ROOT_DIR / "src"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def test_render_creates_runtime_and_status_reports_not_running(tmp_path: Path):
    env = os.environ.copy()
    env["PYTHONPATH"] = str(SRC_DIR)
    env["HOME"] = str(tmp_path)

    config_dir = tmp_path / ".config" / "cproxy"
    config_dir.mkdir(parents=True)
    controller_port = _free_port()
    (config_dir / "config.yaml").write_text(
        f"""
mixed-port: 7890
external-controller: 127.0.0.1:{controller_port}
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
        cwd=ROOT_DIR,
        env=env,
    )

    runtime_file = tmp_path / ".local" / "share" / "cproxy" / "runtime.yaml"
    runtime_text = runtime_file.read_text(encoding="utf-8") if runtime_file.exists() else ""

    assert render_result.returncode == 0
    assert runtime_file.is_file()
    assert "name: AI-MANUAL" in runtime_text
    assert "name: AI-AUTO" in runtime_text
    assert "url: https://cp.cloudflare.com/generate_204" in runtime_text
    assert "DOMAIN-SUFFIX,openai.com,AI-MANUAL" in runtime_text
    assert "GEOIP,CN,DIRECT,no-resolve" in runtime_text

    status_result = subprocess.run(
        [sys.executable, "-m", "cproxy.cli", "status"],
        capture_output=True,
        text=True,
        cwd=ROOT_DIR,
        env=env,
    )

    assert status_result.returncode == 0
    assert "摘要" in status_result.stdout
    assert "资源" in status_result.stdout
    assert "路径" in status_result.stdout
    # 摘要区块的值列对齐到 12 显示列（最长标签“运行配置/通用出口”8 + 间距 4）
    assert "状态        未运行" in status_result.stdout
    assert "运行配置    已就绪" in status_result.stdout


def test_render_resolves_secret_file_into_runtime_secret(tmp_path: Path):
    env = os.environ.copy()
    env["PYTHONPATH"] = str(SRC_DIR)
    env["HOME"] = str(tmp_path)

    config_dir = tmp_path / ".config" / "cproxy"
    config_dir.mkdir(parents=True)
    secret_path = config_dir / "controller-secret"
    secret_path.write_text("runtime-controller-secret\n", encoding="utf-8")
    (config_dir / "config.yaml").write_text(
        f"""
mixed-port: 7890
external-controller: 127.0.0.1:9090
external-controller-tls: 127.0.0.1:9443
secret-file: {secret_path}
proxy-groups:
  - name: 🇺🇸 United States
    type: select
    proxies:
      - 🇺🇸 United States丨01
  - name: 🇸🇬 Singapore
    type: select
    proxies:
      - 🇸🇬 Singapore丨01
rules:
  - MATCH,DIRECT
        """.strip()
        + "\n",
        encoding="utf-8",
    )

    render_result = subprocess.run(
        [sys.executable, "-m", "cproxy.cli", "render"],
        capture_output=True,
        text=True,
        cwd=ROOT_DIR,
        env=env,
    )

    runtime_file = tmp_path / ".local" / "share" / "cproxy" / "runtime.yaml"
    runtime_text = runtime_file.read_text(encoding="utf-8") if runtime_file.exists() else ""

    assert render_result.returncode == 0
    assert "secret: runtime-controller-secret" in runtime_text
    assert "secret-file:" not in runtime_text


def test_status_includes_today_traffic_summary(tmp_path: Path):
    from cproxy.config import default_paths, traffic_db_file
    from cproxy.services.traffic import _SCHEMA

    paths = default_paths(tmp_path)
    paths.config_dir.mkdir(parents=True)
    (paths.config_dir / "config.yaml").write_text(
        "mixed-port: 7890\nexternal-controller: 127.0.0.1:19090\n",
        encoding="utf-8",
    )

    db_path = traffic_db_file(paths)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    today = datetime.now().strftime("%Y-%m-%d")
    with sqlite3.connect(db_path) as conn:
        conn.executescript(_SCHEMA)
        conn.execute(
            "INSERT INTO traffic_samples VALUES (?,?,?,?,?,?,?)",
            (today, "10", "DIRECT", "DomainSuffix(cn)", "a.cn", 2000, 500),
        )
        conn.execute(
            "INSERT INTO traffic_samples VALUES (?,?,?,?,?,?,?)",
            (today, "10", "AI-MANUAL", "DomainSuffix(chatgpt.com)", "chatgpt.com", 1000, 300),
        )

    env = os.environ.copy()
    env["PYTHONPATH"] = str(SRC_DIR)
    env["HOME"] = str(tmp_path)
    result = subprocess.run(
        [sys.executable, "-m", "cproxy.cli", "status"],
        capture_output=True,
        text=True,
        cwd=ROOT_DIR,
        env=env,
    )

    assert result.returncode == 0, result.stderr
    assert "流量" in result.stdout
    assert "今日总量" in result.stdout
    assert "↓2.93 KB" in result.stdout  # 3000 B 总下载
    assert "直连" in result.stdout and "代理" in result.stdout
    assert "34.2%" in result.stdout  # 代理 (1000+300) / 总 (3000+800)


def test_status_prefers_tls_controller_when_configured(tmp_path: Path):
    from cproxy.backend.process import ProcessBackend
    from cproxy.config import default_paths

    paths = default_paths(tmp_path)
    paths.config_dir.mkdir(parents=True)
    (paths.config_dir / "config.yaml").write_text(
        "external-controller: 127.0.0.1:9090\nexternal-controller-tls: 127.0.0.1:9443\n",
        encoding="utf-8",
    )

    snapshot = ProcessBackend(paths).status()

    assert snapshot.controller == "127.0.0.1:9443"


def test_status_reports_ai_route_mode_and_delay_when_api_available(tmp_path: Path):
    state = {
        "AI-MANUAL": {
            "type": "Selector",
            "now": "AI-AUTO",
            "alive": True,
            "all": ["AI-AUTO", "AI-US", "AI-SG"],
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
            "history": [{"delay": 101}],
        },
    }

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/version":
                self._send({"version": "test"})
                return
            if self.path == "/proxies":
                self._send({"proxies": state})
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

    api_port = _free_port()
    mixed_port = _free_port()

    env = os.environ.copy()
    env["PYTHONPATH"] = str(SRC_DIR)
    env["HOME"] = str(tmp_path)

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

    config_dir = tmp_path / ".config" / "cproxy"
    config_dir.mkdir(parents=True)
    (config_dir / "config.yaml").write_text(
        f"""
mixed-port: {mixed_port}
external-controller: 127.0.0.1:{api_port}
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

    server = None
    thread = None
    try:
        render_result = subprocess.run(
            [sys.executable, "-m", "cproxy.cli", "render"],
            capture_output=True,
            text=True,
            cwd=ROOT_DIR,
            env=env,
        )
        assert render_result.returncode == 0

        start_result = subprocess.run(
            [sys.executable, "-m", "cproxy.cli", "start"],
            capture_output=True,
            text=True,
            cwd=ROOT_DIR,
            env=env,
        )
        assert start_result.returncode == 0

        # cproxy 先拉起（假）进程，随后模拟 mihomo 自身开始监听 controller
        server = HTTPServer(("127.0.0.1", api_port), Handler)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()

        status_result = subprocess.run(
            [sys.executable, "-m", "cproxy.cli", "status"],
            capture_output=True,
            text=True,
            cwd=ROOT_DIR,
            env=env,
        )

        assert status_result.returncode == 0
        assert "状态        运行中" in status_result.stdout
        assert "AI 路由     自动切换" in status_result.stdout
        assert "通用出口    AI-US -> United States 01 (95ms)" in status_result.stdout
    finally:
        subprocess.run(
            [sys.executable, "-m", "cproxy.cli", "stop"],
            capture_output=True,
            text=True,
            cwd=ROOT_DIR,
            env=env,
        )
        if server is not None:
            server.shutdown()
            thread.join()


def test_start_stop_restart_manage_user_process(tmp_path: Path, request):
    env = os.environ.copy()
    env["PYTHONPATH"] = str(SRC_DIR)
    env["HOME"] = str(tmp_path)
    request.addfinalizer(
        lambda: subprocess.run(
            [sys.executable, "-m", "cproxy.cli", "stop"],
            capture_output=True,
            text=True,
            cwd=ROOT_DIR,
            env=env,
        )
    )

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
    mixed_port = _free_port()
    controller_port = _free_port()
    (config_dir / "config.yaml").write_text(
        f"""
mixed-port: {mixed_port}
external-controller: 127.0.0.1:{controller_port}
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
        cwd=ROOT_DIR,
        env=env,
    )
    assert render_result.returncode == 0

    start_result = subprocess.run(
        [sys.executable, "-m", "cproxy.cli", "start"],
        capture_output=True,
        text=True,
        cwd=ROOT_DIR,
        env=env,
    )
    assert start_result.returncode == 0
    assert "代理已启动" in start_result.stdout

    status_running = subprocess.run(
        [sys.executable, "-m", "cproxy.cli", "status", "--raw"],
        capture_output=True,
        text=True,
        cwd=ROOT_DIR,
        env=env,
    )
    assert status_running.returncode == 0
    assert "状态: 运行中" in status_running.stdout
    assert args_log.is_file()

    restart_result = subprocess.run(
        [sys.executable, "-m", "cproxy.cli", "restart"],
        capture_output=True,
        text=True,
        cwd=ROOT_DIR,
        env=env,
    )
    assert restart_result.returncode == 0
    assert "代理已启动" in restart_result.stdout

    stop_result = subprocess.run(
        [sys.executable, "-m", "cproxy.cli", "stop"],
        capture_output=True,
        text=True,
        cwd=ROOT_DIR,
        env=env,
    )
    assert stop_result.returncode == 0
    assert "代理已停止" in stop_result.stdout

    status_stopped = subprocess.run(
        [sys.executable, "-m", "cproxy.cli", "status", "--raw"],
        capture_output=True,
        text=True,
        cwd=ROOT_DIR,
        env=env,
    )
    assert status_stopped.returncode == 0
    assert "状态: 未运行" in status_stopped.stdout


def test_stop_does_not_kill_unowned_process_from_stale_pidfile(tmp_path: Path):
    env = os.environ.copy()
    env["PYTHONPATH"] = str(SRC_DIR)
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
            cwd=ROOT_DIR,
            env=env,
        )

        time.sleep(0.2)
        assert stop_result.returncode != 0
        assert "不属于 cproxy 管理的进程" in stop_result.stderr
        assert sleeper.poll() is None
    finally:
        sleeper.terminate()
        sleeper.wait(timeout=5)


def test_start_refuses_when_controller_address_already_listening(tmp_path: Path):
    from cproxy.backend.process import ForeignInstanceError, ProcessBackend
    from cproxy.config import default_paths, runtime_file

    paths = default_paths(tmp_path)
    paths.config_dir.mkdir(parents=True)
    paths.data_dir.mkdir(parents=True)
    runtime_file(paths).write_text("mode: rule\n", encoding="utf-8")

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = int(listener.getsockname()[1])
    try:
        (paths.config_dir / "config.yaml").write_text(
            f"external-controller: 127.0.0.1:{port}\nprogram-path: /bin/true\n",
            encoding="utf-8",
        )
        with pytest.raises(ForeignInstanceError) as excinfo:
            ProcessBackend(paths).start()
        assert "已有服务在监听" in str(excinfo.value)
        # 提示指向真正的生产单元（用户级 cproxy.service），不是已删除的 legacy 名
        assert "systemctl --user stop cproxy.service" in str(excinfo.value)
    finally:
        listener.close()


def test_start_refuses_when_mixed_port_already_listening(tmp_path: Path):
    from cproxy.backend.process import ForeignInstanceError, ProcessBackend
    from cproxy.config import default_paths, runtime_file

    paths = default_paths(tmp_path)
    paths.config_dir.mkdir(parents=True)
    paths.data_dir.mkdir(parents=True)
    runtime_file(paths).write_text("mode: rule\n", encoding="utf-8")

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = int(listener.getsockname()[1])
    try:
        (paths.config_dir / "config.yaml").write_text(
            f"mixed-port: {port}\nprogram-path: /bin/true\n",
            encoding="utf-8",
        )
        with pytest.raises(ForeignInstanceError, match="已有服务在监听"):
            ProcessBackend(paths).start()
    finally:
        listener.close()


def test_restart_succeeds_when_user_cproxy_service_is_active(tmp_path: Path, monkeypatch):
    """`cproxy.service` 就是 cproxy 自身（`ExecStart=cproxy start` / `ExecReload=cproxy restart`）。

    回归：若 `start()` 因 `systemctl --user is-active cproxy.service == 0` 抛
    `ForeignInstanceError`，则 `restart()` 在 `stop()` 杀掉旧进程后会立刻被自己的
    `start()` 拦死（`ExecReload` 时单元状态为 `reloading`，`is-active` 同样返回 0）。
    这里不 mock `backend.start`，走真实的 `start() -> restart() -> stop()` 完整链路。
    """
    from cproxy.backend import process as process_module
    from cproxy.backend.process import SYSTEMD_SERVICE_NAME, ProcessBackend
    from cproxy.config import default_paths, runtime_file

    assert SYSTEMD_SERVICE_NAME == "cproxy.service"

    paths = default_paths(tmp_path)
    paths.config_dir.mkdir(parents=True)
    paths.data_dir.mkdir(parents=True)
    runtime_file(paths).write_text("mode: rule\n", encoding="utf-8")

    fake_bin = tmp_path / "fake-mihomo.sh"
    fake_bin.write_text(
        "#!/bin/bash\ntrap 'exit 0' TERM INT\nwhile true; do sleep 1; done\n",
        encoding="utf-8",
    )
    fake_bin.chmod(0o755)
    (paths.config_dir / "config.yaml").write_text(
        f"mixed-port: 0\nexternal-controller: 127.0.0.1:{_free_port()}\nprogram-path: {fake_bin}\n",
        encoding="utf-8",
    )

    ran: list[list[str]] = []

    class _ActiveResult:
        returncode = 0

    def _fake_run(argv, **kwargs):
        ran.append(list(argv))
        return _ActiveResult()

    monkeypatch.delenv("CPROXY_NO_SYSTEMD", raising=False)
    monkeypatch.setattr(process_module.subprocess, "run", _fake_run)

    backend = ProcessBackend(paths)
    try:
        pid1 = backend.start()
        assert pid1 > 0
        pid2 = backend.restart()
        assert pid2 > 0
        assert pid2 != pid1
        assert backend.is_running() is True
        assert ran == []  # start/restart 均不调 systemctl
    finally:
        backend.stop()


def test_incident_queries_user_level_cproxy_service(tmp_path: Path, monkeypatch):
    """`cproxy incident` 的服务诊断必须查用户级 `--user cproxy.service`。"""
    from cproxy.config import default_paths
    from cproxy.services import ops as ops_module

    calls: list[list[str]] = []

    class _Result:
        returncode = 0
        stdout = "ActiveState=active\nSubState=running\n"

    def _fake_run(argv, **kwargs):
        calls.append(list(argv))
        return _Result()

    monkeypatch.setattr(ops_module.subprocess, "run", _fake_run)
    sections = ops_module.build_incident(default_paths(tmp_path))
    service_sec = next(sec for sec in sections if sec.title == "service")

    assert calls[0][:4] == ["systemctl", "--user", "show", "cproxy.service"]
    assert "ActiveState=active" in service_sec.lines


def test_render_auto_creates_region_groups_from_proxy_names(tmp_path: Path):
    env = os.environ.copy()
    env["PYTHONPATH"] = str(SRC_DIR)
    env["HOME"] = str(tmp_path)

    config_dir = tmp_path / ".config" / "cproxy"
    config_dir.mkdir(parents=True)
    (config_dir / "config.yaml").write_text(
        """
mixed-port: 7890
external-controller: 127.0.0.1:9090
proxies:
  - name: '🇭🇰香港 01'
    type: ss
    server: hk.example.com
    port: 443
    cipher: aes-256-gcm
    password: secret
  - name: '🇺🇸美国 01'
    type: ss
    server: us.example.com
    port: 443
    cipher: aes-256-gcm
    password: secret
  - name: '🇸🇬新加坡 01'
    type: ss
    server: sg.example.com
    port: 443
    cipher: aes-256-gcm
    password: secret
proxy-groups:
  - name: CyberGuard
    type: select
    proxies:
      - 自动选择
      - DIRECT
rules:
  - MATCH,CyberGuard
        """.strip()
        + "\n",
        encoding="utf-8",
    )

    render_result = subprocess.run(
        [sys.executable, "-m", "cproxy.cli", "render"],
        capture_output=True,
        text=True,
        cwd=ROOT_DIR,
        env=env,
    )

    runtime_file = tmp_path / ".local" / "share" / "cproxy" / "runtime.yaml"
    runtime_text = runtime_file.read_text(encoding="utf-8") if runtime_file.exists() else ""

    assert render_result.returncode == 0, render_result.stderr
    assert runtime_file.is_file()
    assert "name: 美国" in runtime_text
    assert "name: 🇸🇬 Singapore" not in runtime_text
    assert "name: 🇺🇸 United States" not in runtime_text
    assert "name: AI-MANUAL" in runtime_text
    assert "name: AI-AUTO" in runtime_text


def test_render_strips_dns_fallback_filter_geosite(tmp_path: Path):
    env = os.environ.copy()
    env["PYTHONPATH"] = str(SRC_DIR)
    env["HOME"] = str(tmp_path)

    config_dir = tmp_path / ".config" / "cproxy"
    config_dir.mkdir(parents=True)
    (config_dir / "config.yaml").write_text(
        """
mixed-port: 7890
external-controller: 127.0.0.1:9090
dns:
  enable: true
  enhanced-mode: fake-ip
  nameserver:
    - 223.5.5.5
  fallback:
    - 1.1.1.1
  fallback-filter:
    geoip: true
    geoip-code: CN
    geosite:
      - gfw
    ipcidr:
      - 240.0.0.0/4
proxies:
  - name: '🇺🇸美国 01'
    type: ss
    server: us.example.com
    port: 443
    cipher: aes-256-gcm
    password: secret
  - name: '🇸🇬新加坡 01'
    type: ss
    server: sg.example.com
    port: 443
    cipher: aes-256-gcm
    password: secret
proxy-groups:
  - name: 🇺🇸 United States
    type: select
    proxies:
      - '🇺🇸美国 01'
  - name: 🇸🇬 Singapore
    type: select
    proxies:
      - '🇸🇬新加坡 01'
rules:
  - MATCH,DIRECT
        """.strip()
        + "\n",
        encoding="utf-8",
    )

    render_result = subprocess.run(
        [sys.executable, "-m", "cproxy.cli", "render"],
        capture_output=True,
        text=True,
        cwd=ROOT_DIR,
        env=env,
    )

    runtime_file = tmp_path / ".local" / "share" / "cproxy" / "runtime.yaml"
    runtime_text = runtime_file.read_text(encoding="utf-8") if runtime_file.exists() else ""

    assert render_result.returncode == 0, render_result.stderr
    assert runtime_file.is_file()
    assert "geosite:" not in runtime_text
    assert "fallback-filter:" in runtime_text


def test_render_injects_chinamax_when_missing_from_source(tmp_path: Path):
    """订阅覆盖后源配置丢失 rule-providers/ChinaMax 和 RULE-SET 规则时，render 应自动补回。"""
    env = os.environ.copy()
    env["PYTHONPATH"] = str(SRC_DIR)
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
  - DOMAIN-SUFFIX,example.com,SSRDOG
  - DOMAIN-SUFFIX,anthropic.com,PROXY
  - DOMAIN-KEYWORD,claude,PROXY
  - DOMAIN-SUFFIX,cursor.sh,DIRECT
  - DOMAIN,safebrowsing.googleapis.com,DIRECT
  - GEOIP,CN,DIRECT
  - MATCH,SSRDOG
        """.strip()
        + "\n",
        encoding="utf-8",
    )

    render_result = subprocess.run(
        [sys.executable, "-m", "cproxy.cli", "render"],
        capture_output=True,
        text=True,
        cwd=ROOT_DIR,
        env=env,
    )

    runtime_file = tmp_path / ".local" / "share" / "cproxy" / "runtime.yaml"
    runtime_text = runtime_file.read_text(encoding="utf-8") if runtime_file.exists() else ""

    assert render_result.returncode == 0, render_result.stderr
    assert runtime_file.is_file()

    import yaml as _yaml

    runtime_data = _yaml.safe_load(runtime_text)

    providers = runtime_data.get("rule-providers", {})
    assert "ChinaMax" in providers
    assert providers["ChinaMax"]["path"] == "./ruleset/ChinaMax.yml"

    rules = runtime_data.get("rules", [])
    assert "RULE-SET,ChinaMax,DIRECT" in rules

    idx_chinamax = rules.index("RULE-SET,ChinaMax,DIRECT")
    idx_geoip = rules.index("GEOIP,CN,DIRECT,no-resolve")
    idx_match = next(i for i, r in enumerate(rules) if r.startswith("MATCH,"))
    idx_ai = next(i for i, r in enumerate(rules) if "AI-MANUAL" in r)

    assert idx_ai < idx_chinamax < idx_geoip < idx_match

    # 裸 GEOIP（无 no-resolve）与有害直连规则应被清理
    assert "GEOIP,CN,DIRECT" not in rules
    assert "DOMAIN-SUFFIX,cursor.sh,DIRECT" not in rules
    assert "DOMAIN,safebrowsing.googleapis.com,DIRECT" not in rules

    # 任意订阅商的 AI 冲突规则（不限 SSRDOG 组名）都应被清理
    assert "DOMAIN-SUFFIX,anthropic.com,PROXY" not in rules
    assert "DOMAIN-KEYWORD,claude,PROXY" not in rules

    # AI 域名覆盖与下载直连规则应注入，且前移到订阅规则之前
    assert "DOMAIN-SUFFIX,claudeusercontent.com,AI-MANUAL" in rules
    assert "DOMAIN-SUFFIX,sora.com,AI-MANUAL" in rules
    assert "DOMAIN-SUFFIX,grok.com,AI-MANUAL" in rules
    assert "DOMAIN-SUFFIX,openai.azure.com,AI-MANUAL" in rules
    assert "DOMAIN,cdn.auth0.com,AI-MANUAL" in rules
    assert "DOMAIN-SUFFIX,challenges.cloudflare.com,AI-MANUAL" in rules
    assert "DOMAIN-SUFFIX,google.com,AI-GEMINI" in rules
    assert "DOMAIN-SUFFIX,googleapis.com,AI-GEMINI" in rules
    assert "DOMAIN-KEYWORD,antigravity,AI-GEMINI" in rules
    assert "DOMAIN-SUFFIX,goog,AI-GEMINI" in rules
    assert "DOMAIN-SUFFIX,appspot.com,AI-GEMINI" in rules
    assert "PROCESS-NAME,agy,AI-MANUAL" in rules
    assert rules.index("DOMAIN-SUFFIX,google.com,AI-GEMINI") < rules.index("PROCESS-NAME,agy,AI-MANUAL")
    idx_pytorch = rules.index("DOMAIN-SUFFIX,pytorch.org,DIRECT")
    assert "DOMAIN-SUFFIX,npmjs.org,DIRECT" in rules
    assert idx_ai < idx_pytorch < idx_match
    idx_subscription = rules.index("DOMAIN-SUFFIX,example.com,SSRDOG")
    assert idx_ai < idx_pytorch < idx_subscription


def test_probe_history_trims_to_max_lines(tmp_path: Path, monkeypatch):
    from cproxy.services import probe_history

    monkeypatch.setattr(probe_history, "MAX_HISTORY_LINES", 5)
    history_path = tmp_path / "probe_history.jsonl"
    for i in range(8):
        probe_history.record_probe_history(
            history_path,
            profile="codex",
            strategy_name="conservative",
            group="AI-MANUAL",
            url="https://chatgpt.com",
            rounds=5,
            timeout_ms=8000,
            current=f"node-{i}",
            current_stable=True,
            current_reason="ok",
            best=f"node-{i}",
            stable=True,
            reason="ok",
            switch_requested=False,
            switched=False,
            skip_reason="",
            nodes=[{"name": f"node-{i}", "success": 5, "failures": 0}],
        )

    lines = history_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 5
    rows = probe_history.load_history_rows(history_path, limit=10)
    assert [row["current"] for row in rows] == ["node-3", "node-4", "node-5", "node-6", "node-7"]


def test_cli_render_apply_reloads_when_stale(tmp_path: Path, monkeypatch, capsys):
    from cproxy import cli as cli_mod
    from cproxy.config import default_paths

    paths = default_paths(tmp_path)
    runtime_path = paths.data_dir / "runtime.yaml"
    runtime_path.parent.mkdir(parents=True, exist_ok=True)
    runtime_path.write_text("mixed-port: 7890\n", encoding="utf-8")

    reloaded: list[str] = []
    applied: list[Path] = []

    class _FakeProc:
        def __init__(self, p):
            pass

        def is_running(self):
            return True

        def is_runtime_stale(self, path=None):
            return True

        def mark_runtime_applied(self, path=None):
            applied.append(Path(path))
            return True

    class _FakeAPI:
        def __init__(self, p):
            pass

        def reload_config(self, path: str):
            reloaded.append(path)

    monkeypatch.setattr(cli_mod, "default_paths", lambda: paths)
    monkeypatch.setattr(cli_mod, "render_runtime", lambda p: runtime_path)
    monkeypatch.setattr(cli_mod, "ProcessBackend", _FakeProc)
    monkeypatch.setattr(cli_mod, "APIBackend", _FakeAPI)

    rc = cli_mod.run(["render", "--apply"])
    out = capsys.readouterr().out
    assert rc == 0
    assert reloaded == [str(runtime_path)]
    assert applied == [runtime_path]
    assert "已热重载运行配置" in out
