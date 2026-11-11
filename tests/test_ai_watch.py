"""AI 出口看门狗：语义判定、连续失败切换、冷却期与状态持久化。"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from urllib.error import HTTPError

ROOT_DIR = Path(__file__).resolve().parents[1]
SRC_DIR = ROOT_DIR / "src"
sys.path.insert(0, str(SRC_DIR))

from cproxy.backend.models import ProxyGroup
from cproxy.config import AppPaths
from cproxy.services import ai_watch as ai_watch_mod
from cproxy.services.ai_watch import AIWatchService
from cproxy.services.diagnostics import PROBE_DEAD, PROBE_GEO_BLOCKED, PROBE_OK, _probe_target

# --- 语义分类 ---

class _FakeResponse:
    def __init__(self, status: int):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def getcode(self):
        return self.status


class _FakeHTTPError(HTTPError):
    def __init__(self, code: int, body: str):
        self.code = code
        self._body = body

    def read(self, amt=None):
        data = self._body.encode()
        return data if amt is None else data[:amt]


class _FakeOpener:
    def __init__(self, behavior: str):
        self._behavior = behavior

    def open(self, request, timeout=None):
        if self._behavior == "ok":
            return _FakeResponse(200)
        if self._behavior == "geo":
            raise _FakeHTTPError(400, "User location is not supported for the API use.")
        if self._behavior == "dead":
            raise _FakeHTTPError(500, "internal server error")
        raise AssertionError(f"unknown behavior {self._behavior}")


def test_probe_target_classifies():
    """retries 显式传 0：直接表达「不重试」，不必再 monkeypatch 模块常量。"""
    assert _probe_target(_FakeOpener("ok"), "http://x", 5, 0)[0] == PROBE_OK
    assert _probe_target(_FakeOpener("geo"), "http://x", 5, 0)[0] == PROBE_GEO_BLOCKED
    assert _probe_target(_FakeOpener("dead"), "http://x", 5, 0)[0] == PROBE_DEAD


# --- 看门狗 ---

def _paths(tmp_path: Path) -> AppPaths:
    return AppPaths(config_dir=tmp_path / "cfg", data_dir=tmp_path / "data", state_dir=tmp_path / "state")


def _group(name: str, candidates: list[str], current: str) -> ProxyGroup:
    return ProxyGroup(name=name, type="fallback", current=current, candidates=list(candidates))


def _groups() -> dict[str, ProxyGroup]:
    return {
        "AI-MANUAL": _group("AI-MANUAL", ["US-01", "JP-01", "AI-AUTO"], "US-01"),
        "AI-GEMINI": _group("AI-GEMINI", ["KR-01", "GB-01"], "KR-01"),
        "AI-AUTO": _group("AI-AUTO", ["US-01", "JP-01"], "US-01"),
    }


class _FakeQuery:
    def __init__(self, groups: dict[str, ProxyGroup]):
        self.api = type("_API", (), {"get_groups": lambda self: groups})()
        self.switches: list[tuple[str, str]] = []

    def switch_group(self, group: str, target: str):
        self.switches.append((group, target))


def test_watch_switches_after_two_consecutive_failures(tmp_path, monkeypatch):
    paths = _paths(tmp_path)
    groups = _groups()
    svc = AIWatchService(paths)
    fake_q = _FakeQuery(groups)
    svc.query = fake_q
    monkeypatch.setattr(ai_watch_mod, "ai_target_verdict", lambda p, url: PROBE_GEO_BLOCKED)

    report1 = svc.watch()
    assert not any(r.switched for r in report1.results), "第一轮失败不切换（连续失败阈值 2）"

    report2 = svc.watch()
    manual = next(r for r in report2.results if r.group == "AI-MANUAL")
    gemini = next(r for r in report2.results if r.group == "AI-GEMINI")
    assert manual.switched and manual.target == "JP-01"
    assert gemini.switched and gemini.target == "GB-01"
    assert ("AI-MANUAL", "JP-01") in fake_q.switches
    assert ("AI-GEMINI", "GB-01") in fake_q.switches


def test_watch_ok_resets_failure_count(tmp_path, monkeypatch):
    paths = _paths(tmp_path)
    groups = _groups()
    monkeypatch.setattr(ai_watch_mod, "ai_target_verdict", lambda p, url: PROBE_GEO_BLOCKED)

    svc = AIWatchService(paths)
    svc.query = _FakeQuery(groups)
    svc.watch()  # fail_count=1

    # 第二轮恢复正常：不应切换，且计数清零
    monkeypatch.setattr(ai_watch_mod, "ai_target_verdict", lambda p, url: PROBE_OK)
    fake_q2 = _FakeQuery(groups)
    svc2 = AIWatchService(paths)
    svc2.query = fake_q2
    report = svc2.watch()
    assert not any(r.switched for r in report.results)
    assert fake_q2.switches == []

    # 第三轮又失败：计数应从 0 重新开始，仍不切换（不足 2 次连续）
    monkeypatch.setattr(ai_watch_mod, "ai_target_verdict", lambda p, url: PROBE_GEO_BLOCKED)
    svc3 = AIWatchService(paths)
    svc3.query = _FakeQuery(groups)
    assert not any(r.switched for r in svc3.watch().results)


def test_pick_next_skips_cooldown_and_nested_group(tmp_path):
    svc = AIWatchService(_paths(tmp_path))
    groups = _groups()
    group = groups["AI-MANUAL"]
    now = time.time()
    cooldown = {"JP-01": now}  # JP-01 在冷却期
    # candidates: US-01(current)、JP-01(冷却)、AI-AUTO(嵌套组) → 无可切
    assert svc._pick_next(groups, group, "US-01", cooldown, now) is None
    # 无冷却时 JP-01 可选
    assert svc._pick_next(groups, group, "US-01", {}, now) == "JP-01"


def test_state_persists_across_instances(tmp_path, monkeypatch):
    paths = _paths(tmp_path)
    groups = _groups()
    monkeypatch.setattr(ai_watch_mod, "ai_target_verdict", lambda p, url: PROBE_DEAD)

    svc1 = AIWatchService(paths)
    svc1.query = _FakeQuery(groups)
    svc1.watch()  # fail_count=1，写状态

    fake_q2 = _FakeQuery(groups)
    svc2 = AIWatchService(paths)
    svc2.query = fake_q2
    report = svc2.watch()  # 读到 fail_count=1 → 变 2 → 切换
    assert any(r.switched for r in report.results)
    assert fake_q2.switches


def test_watch_skips_auto_mode(tmp_path, monkeypatch):
    paths = _paths(tmp_path)
    groups = {
        "AI-MANUAL": _group("AI-MANUAL", ["US-01", "JP-01", "AI-AUTO"], "AI-AUTO"),
        "AI-GEMINI": _group("AI-GEMINI", ["KR-01", "GB-01"], "KR-01"),
        "AI-AUTO": _group("AI-AUTO", ["AI-US", "AI-SG"], "AI-US"),
        "AI-US": _group("AI-US", ["US-01"], "US-01"),
        "AI-SG": _group("AI-SG", ["SG-01"], "SG-01"),
    }
    monkeypatch.setattr(ai_watch_mod, "ai_target_verdict", lambda p, url: PROBE_GEO_BLOCKED)
    svc = AIWatchService(paths)
    fake_q = _FakeQuery(groups)
    svc.query = fake_q

    svc.watch()
    report = svc.watch()  # 连续两轮失败，但 AI-MANUAL 在自动模式 → 不应被切走
    manual = next(r for r in report.results if r.group == "AI-MANUAL")
    assert not manual.switched
    assert not any(sw[0] == "AI-MANUAL" for sw in fake_q.switches)


class _FailingQuery:
    def __init__(self, groups):
        self.api = type("_API", (), {"get_groups": lambda self: groups})()

    def switch_group(self, group, target):
        raise RuntimeError("switch boom")


def test_watch_switch_failure_preserves_state(tmp_path, monkeypatch):
    from cproxy.config import ai_watch_state_file

    paths = _paths(tmp_path)
    groups = _groups()
    monkeypatch.setattr(ai_watch_mod, "ai_target_verdict", lambda p, url: PROBE_DEAD)

    svc = AIWatchService(paths)
    svc.query = _FailingQuery(groups)
    svc.watch()  # fail_count=1

    svc2 = AIWatchService(paths)
    svc2.query = _FailingQuery(groups)
    report = svc2.watch()  # 触发切换但 switch_group 抛异常 → 不崩溃，计数保留
    assert not any(r.switched for r in report.results)
    state = json.loads(ai_watch_state_file(paths).read_text(encoding="utf-8"))
    assert state["AI-MANUAL"]["fail_count"] == 2  # 保留计数，下一轮重试


def test_pick_next_with_leaf_entries_in_groups_and_health_preference(tmp_path):
    """回归：真实 mihomo GET /proxies 会把每个叶子节点也放进 groups（candidates=[]），
    _pick_next 不能因 `cand in groups` 把所有叶子误判成嵌套组；同时优先跳到存活叶子。"""
    svc = AIWatchService(_paths(tmp_path))
    now = time.time()
    groups = {
        "AI-MANUAL": _group("AI-MANUAL", ["US-01", "DEAD-01", "TIMEOUT-01", "JP-01", "AI-AUTO"], "US-01"),
        "AI-AUTO": _group("AI-AUTO", ["US-01", "JP-01"], "US-01"),
        # 真实 mihomo 叶子条目：candidates=[]
        "US-01": ProxyGroup(name="US-01", type="Shadowsocks", current="-", candidates=[], delay=180, alive=True),
        "DEAD-01": ProxyGroup(name="DEAD-01", type="Shadowsocks", current="-", candidates=[], delay=None, alive=False),
        "TIMEOUT-01": ProxyGroup(
            name="TIMEOUT-01", type="Shadowsocks", current="-", candidates=[], delay=5000, alive=True
        ),
        "JP-01": ProxyGroup(name="JP-01", type="Shadowsocks", current="-", candidates=[], delay=210, alive=True),
    }
    group = groups["AI-MANUAL"]
    # 跳过 DEAD-01（alive=False）与 TIMEOUT-01（delay=5000 超时），优先选健康叶子 JP-01
    assert svc._pick_next(groups, group, "US-01", {}, now) == "JP-01"
    # 当唯一健康叶子 JP-01 也在冷却期时，回退到首个非冷却叶子 DEAD-01，而非彻底返回 None
    assert svc._pick_next(groups, group, "US-01", {"JP-01": now}, now) == "DEAD-01"


def _make_fake_proc(
    proc_root: Path,
    pid: int,
    *,
    ppid: int = 1,
    uid: int = 0,
    exe: str | None = None,
    cmdline: str = "agy",
    tty_atime: float | None = None,
) -> None:
    pdir = proc_root / str(pid)
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / "status").write_text(f"Name:\tagy\nPPid:\t{ppid}\nUid:\t{uid}\t{uid}\t{uid}\t{uid}\n", encoding="utf-8")
    (pdir / "cmdline").write_text(cmdline.replace(" ", "\x00"), encoding="utf-8")
    if exe is not None:
        (pdir / "exe").symlink_to(exe)
    if tty_atime is not None:
        fd_dir = pdir / "fd"
        fd_dir.mkdir(parents=True, exist_ok=True)
        tty_file = proc_root / f"pts_{pid}"
        tty_file.write_text("", encoding="utf-8")
        import os as _os

        _os.utime(tty_file, (tty_atime, tty_atime))
        (fd_dir / "0").symlink_to(tty_file)


def test_reap_stale_agy_processes_respects_all_safety_guards(tmp_path):
    """验证自动清理仅杀闲置超时的 agy.<ts>.old 残留进程，绝不误杀：
    - 正常最新二进制 (/root/.local/bin/agy)
    - 当前调用链祖先进程
    - 终端在 30 分钟内仍有输入的 .old 会话
    - 存在非 statusline 活跃子进程的 .old 会话
    """
    from cproxy.services.ai_watch import reap_stale_agy_processes

    proc_root = tmp_path / "proc"
    now = 1_800_000_000.0
    old_exe = "/root/.local/bin/agy.1792306061959817128.old (deleted)"
    fresh_exe = "/root/.local/bin/agy"

    # 100: 闲置 2 小时的 .old 进程，仅挂着 statusline 子进程(101) -> 应清理
    _make_fake_proc(proc_root, 100, ppid=1, uid=0, exe=old_exe, tty_atime=now - 7200)
    _make_fake_proc(
        proc_root, 101, ppid=100, uid=0, exe="/usr/bin/node", cmdline="node /root/.gemini/agy-hud.js statusline"
    )
    # 200: 正常最新 agy（即便闲置 2 小时） -> 不清理
    _make_fake_proc(proc_root, 200, ppid=1, uid=0, exe=fresh_exe, tty_atime=now - 7200)
    # 300: 刚在 5 分钟前有终端输入的 .old 进程 -> 不清理
    _make_fake_proc(proc_root, 300, ppid=1, uid=0, exe=old_exe, tty_atime=now - 300)
    # 400: 闲置 2 小时但有正在执行 bash 子任务(401)的 .old 进程 -> 不清理
    _make_fake_proc(proc_root, 400, ppid=1, uid=0, exe=old_exe, tty_atime=now - 7200)
    _make_fake_proc(proc_root, 401, ppid=400, uid=0, exe="/usr/bin/bash", cmdline="bash -c pytest")
    # 500: 调用链自身祖先进程(500 -> 501) -> 绝不清理
    _make_fake_proc(proc_root, 500, ppid=1, uid=0, exe=old_exe, tty_atime=now - 7200)
    _make_fake_proc(proc_root, 501, ppid=500, uid=0, exe="/usr/bin/python3", cmdline="python3 cproxy")

    killed: list[tuple[int, int]] = []
    reaped = reap_stale_agy_processes(
        proc_root,
        now=now,
        idle_seconds=1800,
        dry_run=False,
        kill_fn=lambda pid, sig: killed.append((pid, sig)),
        current_uid=0,
        self_pid=501,
    )
    assert reaped == [100]
    assert len(killed) == 1 and killed[0][0] == 100


def test_normalize_agy_old_process_label():
    from cproxy.services.traffic import _normalize_process_label

    assert _normalize_process_label("/root/.local/bin/agy.1792306061959817128.old (deleted)") == "/root/.local/bin/agy"
    assert _normalize_process_label("/root/.local/bin/agy.1792306061959817128.old") == "/root/.local/bin/agy"
    assert _normalize_process_label("agy.1792306061959817128.old (deleted)") == "agy"
    assert (
        _normalize_process_label(r"C:\Users\14767\AppData\Local\agy\bin\agy.exe.1793269750355348300.old")
        == r"C:\Users\14767\AppData\Local\agy\bin\agy.exe"
    )
    assert _normalize_process_label("agy.exe.1793269750355348300.old") == "agy.exe"
    assert _normalize_process_label("/usr/bin/curl") == "/usr/bin/curl"


def test_watch_dual_endpoint_catches_cloudcode_failure_and_passive_fallback_drops_old_leaf(tmp_path, monkeypatch):
    """验证：
    1. AI-GEMINI 同时探测 generativelanguage 与 daily-cloudcode-pa，任一不通即累计失败并在第 2 轮切换；
    2. 当 mihomo fallback 被动改变了活跃叶子（last_active != current），自动清理滞留在旧叶子的连接。
    """
    paths = _paths(tmp_path)
    groups = _groups()
    dropped_calls: list[tuple[str, str | None]] = []

    class _TrackingQuery(_FakeQuery):
        def _drop_stale_ai_connections(self, group: str, *, keep_leaf: str | None = None) -> int:
            dropped_calls.append((group, keep_leaf))
            return 1

    def _verdict(p, url: str) -> str:
        if "daily-cloudcode-pa.googleapis.com" in url:
            return PROBE_DEAD
        return PROBE_OK

    monkeypatch.setattr(ai_watch_mod, "ai_target_verdict", _verdict)

    svc1 = AIWatchService(paths)
    svc1.query = _TrackingQuery(groups)
    r1 = svc1.watch()
    gemini1 = next(r for r in r1.results if r.group == "AI-GEMINI")
    assert gemini1.verdict == PROBE_DEAD
    assert not gemini1.switched
    assert dropped_calls == []

    # 模拟 mihomo fallback 在两轮巡检之间被动把 AI-MANUAL 从 US-01 切到了 JP-01
    groups["AI-MANUAL"] = _group("AI-MANUAL", ["US-01", "JP-01", "AI-AUTO"], "JP-01")
    svc2 = AIWatchService(paths)
    q2 = _TrackingQuery(groups)
    svc2.query = q2
    r2 = svc2.watch()
    gemini2 = next(r for r in r2.results if r.group == "AI-GEMINI")
    assert gemini2.switched and gemini2.target == "GB-01"
    assert ("AI-GEMINI", "GB-01") in q2.switches
    # 被动切换的 AI-MANUAL 触发了旧叶子连接清理（keep_leaf="JP-01"）
    assert dropped_calls == [("AI-MANUAL", "JP-01")]


def test_watch_passive_fallback_from_dead_node_updates_fixed_and_cools_down(tmp_path, monkeypatch):
    """当被 fixed 钉住的旧节点被 mihomo 健康检查判死（alive=False）而被动回退到新节点时，
    看门狗必须把旧节点记入 cooldown 并调 api.switch_group 把 fixed 更新到新节点，
    防止旧节点 5 分钟后偶发回包即被 mihomo fallback 瞬间弹回。"""
    from cproxy.config import ai_watch_state_file

    paths = _paths(tmp_path)
    groups = {
        "AI-MANUAL": _group("AI-MANUAL", ["US-01", "SG-01", "AI-AUTO"], "SG-01"),
        "AI-GEMINI": _group("AI-GEMINI", ["KR-01", "DE-01"], "KR-01"),
        "US-01": ProxyGroup(name="US-01", type="Shadowsocks", current="-", candidates=[], delay=120, alive=True),
        "SG-01": ProxyGroup(name="SG-01", type="Shadowsocks", current="-", candidates=[], delay=90, alive=True),
        "KR-01": ProxyGroup(name="KR-01", type="Shadowsocks", current="-", candidates=[], delay=80, alive=True),
        "DE-01": ProxyGroup(name="DE-01", type="Shadowsocks", current="-", candidates=[], delay=150, alive=True),
    }
    api_switches: list[tuple[str, str]] = []

    class _PinTrackingQuery(_FakeQuery):
        def __init__(self, g):
            super().__init__(g)
            self.api = type(
                "_API",
                (),
                {
                    "get_groups": lambda _self: g,
                    "switch_group": lambda _self, grp, target: api_switches.append((grp, target)),
                },
            )()

    monkeypatch.setattr(ai_watch_mod, "ai_target_verdict", lambda p, url: PROBE_OK)

    svc1 = AIWatchService(paths)
    svc1.query = _PinTrackingQuery(groups)
    svc1.watch()  # 记录 last_active = SG-01

    # 模拟 SG-01 彻底断流 (alive=False, delay=0)，mihomo fallback 被动回退到候选首项 US-01
    groups["SG-01"] = ProxyGroup(name="SG-01", type="Shadowsocks", current="-", candidates=[], delay=0, alive=False)
    groups["AI-MANUAL"] = _group("AI-MANUAL", ["US-01", "SG-01", "AI-AUTO"], "US-01")

    svc2 = AIWatchService(paths)
    svc2.query = _PinTrackingQuery(groups)
    svc2.watch()

    assert api_switches == [("AI-MANUAL", "US-01")]
    state = json.loads(ai_watch_state_file(paths).read_text(encoding="utf-8"))
    assert "SG-01" in state["AI-MANUAL"]["cooldown"]


def test_probe_target_catches_google_gws_hk_cookie_redirect_and_gzip_403():
    """验证主动探测识别三类无 API Key 下的隐性地区门禁：
    1. Google GWS 将出口 IP 判为 CN（送中）/ HK 时在 HTTP 200 头下发 `domain=.google.com.hk` Cookie；
    2. 重定向最终 URL 落入 `available-regions`；
    3. gzip 压缩的 HTTP 403 错误响应体包含 `LOCATION_NOT_ELIGIBLE`。
    """
    import gzip
    from email.message import Message

    class _HeaderResponse:
        def __init__(self, status: int, cookies: list[str], url: str = "https://www.google.com/?hl=en"):
            self.status = status
            self.url = url
            msg = Message()
            for ck in cookies:
                msg["Set-Cookie"] = ck
            self.headers = msg

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def getcode(self):
            return self.status

    class _GzipHTTPError(HTTPError):
        def __init__(self, code: int, plain_body: str):
            self.code = code
            self._compressed = gzip.compress(plain_body.encode("utf-8"))
            msg = Message()
            msg["Content-Encoding"] = "gzip"
            self.hdrs = msg

        def read(self, amt=None):
            return self._compressed if amt is None else self._compressed[:amt]

    class _ScenarioOpener:
        def __init__(self, fn):
            self._fn = fn

        def open(self, request, timeout=None):
            return self._fn(request)

    # 1. 送中台湾节点：HTTP 200 但 GWS 下发 .google.com.hk Cookie
    verdict_hk, _ = _probe_target(
        _ScenarioOpener(
            lambda _r: _HeaderResponse(
                200,
                ["__Secure-STRP=AD6Dogt; expires=Wed, 11-Nov-2026 07:05:00 GMT; path=/; domain=.google.com.hk; Secure"],
            )
        ),
        "https://www.google.com/?hl=en",
        5,
        0,
    )
    assert verdict_hk == PROBE_GEO_BLOCKED

    # 2. 干净德国/日本节点：HTTP 200 且 Cookie 为 .google.com
    verdict_clean, _ = _probe_target(
        _ScenarioOpener(
            lambda _r: _HeaderResponse(
                200,
                ["AEC=AaJma5; expires=Mon, 10-May-2027 07:00:00 GMT; path=/; domain=.google.com; Secure; HttpOnly"],
            )
        ),
        "https://www.google.com/?hl=en",
        5,
        0,
    )
    assert verdict_clean == PROBE_OK

    # 3. 200/302 落入 available-regions 页面
    verdict_redir, _ = _probe_target(
        _ScenarioOpener(lambda _r: _HeaderResponse(200, [], url="https://ai.google.dev/gemini-api/docs/available-regions")),
        "https://aistudio.google.com/",
        5,
        0,
    )
    assert verdict_redir == PROBE_GEO_BLOCKED

    # 4. gzip 压缩的 403 LOCATION_NOT_ELIGIBLE 错误体
    def _raise_gzip_403(_r):
        raise _GzipHTTPError(
            403,
            (
                '{"error":{"status":"PERMISSION_DENIED",'
                '"message":"Your location is not eligible for this service.",'
                '"details":[{"reason":"LOCATION_NOT_ELIGIBLE"}]}}'
            ),
        )

    verdict_403, _ = _probe_target(_ScenarioOpener(_raise_gzip_403), "https://aicode.googleapis.com", 5, 0)
    assert verdict_403 == PROBE_GEO_BLOCKED


def test_detect_recent_agy_geo_block_matches_rpc_errors_and_ignores_user_input(tmp_path):
    """验证 agy 日志地区门禁扫描：
    1. 捕获 errorreport.go 的 400 User location is not supported；
    2. 捕获 code_assist_client.go 后台 60s FetchConfig 的 LOCATION_NOT_ELIGIBLE 并提取 detected_location=CN；
    3. 绝不把 input_loop.go 中用户粘贴报错文本误判为地区门禁（防自激）；
    4. 若后续已有更新的 streamGenerateContent ResponseID 成功记录，忽略旧报错。
    """
    from cproxy.services.ai_watch import detect_recent_agy_geo_block

    log_dir = tmp_path / ".gemini" / "antigravity-cli" / "log"
    log_dir.mkdir(parents=True)
    now = 1_794_387_200.0  # 固定时间戳
    lt = time.localtime(now)
    prefix_10s_ago = time.strftime("%m%d %H:%M:%S", time.localtime(now - 10.0))
    prefix_5s_ago = time.strftime("%m%d %H:%M:%S", time.localtime(now - 5.0))
    prefix_2s_ago = time.strftime("%m%d %H:%M:%S", time.localtime(now - 2.0))
    _ = lt

    log_file = log_dir / "cli-20261111_140000.log"

    # 仅含用户输入粘贴的报错文本 -> 绝不触发
    log_file.write_text(
        f'I{prefix_5s_ago}.100000 907 input_loop.go:104] HandleUserInput called with text: '
        f'"刚刚agy又报错了⚠ FAILED_PRECONDITION (code 400): User location is not supported for the API use."\n',
        encoding="utf-8",
    )
    ok_input, _, _ = detect_recent_agy_geo_block(log_dir, now=now)
    assert ok_input is False

    # 后台 FetchConfig LOCATION_NOT_ELIGIBLE (detected_location:CN) -> 触发并提取 CN
    log_file.write_text(
        f"W{prefix_5s_ago}.250000 907 code_assist_client.go:339] AICode FetchConfig failed: "
        f"rpc error: code = PermissionDenied desc = Your location is not eligible for this service. "
        f"error details: reason = LOCATION_NOT_ELIGIBLE domain = aicode.googleapis.com metadata = map[detected_location:CN]\n",
        encoding="utf-8",
    )
    hit_cn, ts_cn, reason_cn = detect_recent_agy_geo_block(log_dir, now=now)
    assert hit_cn is True
    assert ts_cn > 0
    assert "LOCATION_NOT_ELIGIBLE" in reason_cn and "CN" in reason_cn

    # errorreport.go 400 -> 触发；但若之后已有 streamGenerateContent ResponseID 成功，则不重复触发
    log_file.write_text(
        f"E{prefix_10s_ago}.100000 907 errorreport.go:273] ⚠ FAILED_PRECONDITION (code 400): "
        f"User location is not supported for the API use.\n",
        encoding="utf-8",
    )
    hit_400, _, reason_400 = detect_recent_agy_geo_block(log_dir, now=now)
    assert hit_400 is True
    assert "400 User location is not supported" in reason_400

    log_file.write_text(
        f"E{prefix_10s_ago}.100000 907 errorreport.go:273] ⚠ FAILED_PRECONDITION (code 400): "
        f"User location is not supported for the API use.\n"
        f"I{prefix_2s_ago}.300000 907 client.go:210] streamGenerateContent succeeded ResponseID: resp_123\n",
        encoding="utf-8",
    )
    hit_recovered, _, _ = detect_recent_agy_geo_block(log_dir, now=now)
    assert hit_recovered is False


def test_watch_agy_geo_block_log_forces_immediate_switch_off_1x_and_quarantines_node(tmp_path, monkeypatch):
    """复现台湾送中 1X 节点事故：
    即便当前节点名为 `1X` 安全节点且通用 API 探测返回 200/403，只要 agy 日志出现真实地区门禁，
    ai-watch 单轮立即强切（force_drop=True），跳过已隔离的送中节点，直达干净德国节点，
    并将该送中节点写入 12h `gemini_geo_blocked` 隔离池且沉底到 `ai-gemini-order.json` 末尾。
    """
    from cproxy.config import ai_gemini_order_file
    from cproxy.services.probe import is_gemini_geo_blocked, record_gemini_geo_blocked_node

    paths = _paths(tmp_path)
    log_dir = tmp_path / "agy_logs"
    log_dir.mkdir(parents=True)
    now = time.time()
    ts_str = time.strftime("%m%d %H:%M:%S", time.localtime(now - 3.0))
    (log_dir / "cli-20261111_141200.log").write_text(
        f"E{ts_str}.500000 1234 errorreport.go:273] ⚠ FAILED_PRECONDITION (code 400): "
        f"User location is not supported for the API use.\n",
        encoding="utf-8",
    )

    order_path = ai_gemini_order_file(paths)
    order_path.parent.mkdir(parents=True, exist_ok=True)
    order_path.write_text(
        json.dumps(["🇹🇼台湾 01 | 1X", "🇩🇪德国 01 | 1X", "🇬🇧英国 01 | IEPL 0.8X | CM"], ensure_ascii=False),
        encoding="utf-8",
    )
    # 预先标记同出口 IP 的另一节点已隔离，验证 _pick_next 自动跳过它
    record_gemini_geo_blocked_node(paths, "🇹🇼台湾 01 | IEPL 0.8X | CM", now=now)

    groups = {
        "AI-MANUAL": _group("AI-MANUAL", ["US-01", "JP-01", "AI-AUTO"], "US-01"),
        "AI-GEMINI": _group(
            "AI-GEMINI",
            ["🇹🇼台湾 01 | 1X", "🇹🇼台湾 01 | IEPL 0.8X | CM", "🇩🇪德国 01 | 1X", "🇬🇧英国 01 | IEPL 0.8X | CM"],
            "🇹🇼台湾 01 | 1X",
        ),
        "AI-AUTO": _group("AI-AUTO", ["US-01", "JP-01"], "US-01"),
        "🇹🇼台湾 01 | 1X": ProxyGroup(name="🇹🇼台湾 01 | 1X", type="VLESS", current="-", candidates=[], delay=55, alive=True),
        "🇹🇼台湾 01 | IEPL 0.8X | CM": ProxyGroup(
            name="🇹🇼台湾 01 | IEPL 0.8X | CM", type="VLESS", current="-", candidates=[], delay=48, alive=True
        ),
        "🇩🇪德国 01 | 1X": ProxyGroup(name="🇩🇪德国 01 | 1X", type="VLESS", current="-", candidates=[], delay=165, alive=True),
        "🇬🇧英国 01 | IEPL 0.8X | CM": ProxyGroup(
            name="🇬🇧英国 01 | IEPL 0.8X | CM", type="VLESS", current="-", candidates=[], delay=170, alive=True
        ),
    }

    switch_calls: list[tuple[str, str, bool, bool]] = []

    class _ForceTrackingQuery(_FakeQuery):
        def switch_group(self, group: str, target: str, *, drop_connections: bool = True, force_drop: bool = False):
            switch_calls.append((group, target, drop_connections, force_drop))

    monkeypatch.setattr(ai_watch_mod, "ai_target_verdict", lambda _p, _u: PROBE_OK)
    svc = AIWatchService(paths, agy_log_dir=log_dir)
    svc.query = _ForceTrackingQuery(groups)

    report = svc.watch()
    gemini_res = next(r for r in report.results if r.group == "AI-GEMINI")
    assert gemini_res.verdict == PROBE_GEO_BLOCKED
    assert gemini_res.switched is True
    assert gemini_res.target == "🇩🇪德国 01 | 1X"
    assert switch_calls == [("AI-GEMINI", "🇩🇪德国 01 | 1X", True, True)]
    assert is_gemini_geo_blocked("🇹🇼台湾 01 | 1X", paths) is True
    updated_order = json.loads(order_path.read_text(encoding="utf-8"))
    assert updated_order[0] == "🇩🇪德国 01 | 1X"
    assert updated_order[-1] == "🇹🇼台湾 01 | 1X"


def test_drop_stale_ai_connections_force_drops_poisoned_h2_keepalive_socket(tmp_path, monkeypatch):
    """验证地区门禁强切（force=True）必须强杀滞留在旧送中叶子上的 HTTP/2 保活连接：
    常规切换（force=False）会保护 `upload>=18KB, download<4KB, idle=2s` 的疑似思考中流，
    而 `force=True` 必须立即掐断该连接，防止 H2KeepaliveProxy 复用送中 TCP 套接字继续报 400。
    """
    from cproxy.services import ai_watch as aw_mod
    from cproxy.services.query import QueryService

    paths = _paths(tmp_path)
    closed_ids: list[str] = []
    fake_conns = {
        "connections": [
            {
                "id": "conn-poisoned-tw",
                "upload": 38_000,
                "download": 820,
                "chains": ["🇹🇼台湾 01 | 1X", "AI-GEMINI"],
                "metadata": {
                    "host": "daily-cloudcode-pa.googleapis.com",
                    "sourcePort": "54321",
                    "process": "cproxy",
                },
            },
            {
                "id": "conn-clean-de",
                "upload": 25_000,
                "download": 500,
                "chains": ["🇩🇪德国 01 | 1X", "AI-GEMINI"],
                "metadata": {
                    "host": "daily-cloudcode-pa.googleapis.com",
                    "sourcePort": "54322",
                    "process": "cproxy",
                },
            },
        ]
    }

    monkeypatch.setattr(aw_mod, "get_local_proxy_socket_idle_ms", lambda _port=7890: {"54321": (1500, 1200)})
    q = QueryService(paths)
    q.api = type(
        "_ConnAPI",
        (),
        {
            "get_connections": lambda _self: fake_conns,
            "close_connection": lambda _self, cid: closed_ids.append(cid),
        },
    )()

    # 常规非强制清理：保护疑似长思考连接
    assert q._drop_stale_ai_connections("AI-GEMINI", keep_leaf="🇩🇪德国 01 | 1X", force=False) == 0
    assert closed_ids == []

    # 地区门禁强制清理：立即关闭旧送中叶子的中毒连接，但保留新叶子连接
    assert q._drop_stale_ai_connections("AI-GEMINI", keep_leaf="🇩🇪德国 01 | 1X", force=True) == 1
    assert closed_ids == ["conn-poisoned-tw"]


