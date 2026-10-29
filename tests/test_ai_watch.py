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

