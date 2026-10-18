"""AI 出口看门狗：对当前活跃出口做语义探测，连续失败即切到下一个好节点。

与 mihomo 的 fallback（测通用连通性 generate_204）互补：fallback 兜「节点彻底挂」，
本看门狗兜「节点通但 AI 用不了」（Gemini 地区门禁等）。语义判定由
`diagnostics.ai_target_verdict` 提供；连续失败阈值 + 冷却期抗抖，状态持久化到
`ai-watch.json`（oneshot timer 跨轮恢复）。
"""
from __future__ import annotations

import json
import os
import re
import signal
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from ..backend.runtime import AI_AUTO_GROUP, AI_GEMINI_GROUP, AI_MANUAL_GROUP, delay_state
from ..config import AppPaths, ai_watch_state_file, read_config
from .diagnostics import DEFAULT_AI_PROBE_TARGETS, PROBE_DEAD, PROBE_GEO_BLOCKED, PROBE_OK, ai_target_verdict
from .probe import resolve_current_leaf
from .query import QueryService

CONSECUTIVE_FAILURE_THRESHOLD = 2
COOLDOWN_SECONDS = 10 * 60
# agy 自更新会把旧二进制重命名为 `agy.<ns>.old`（或进一步 unlink 成 `(deleted)`）。
# 闲置超过 30 分钟的 `.old` 进程仍会每 ~15s 轮询 daily-cloudcode-pa.googleapis.com，
# 抢占同账号流式配额导致活跃会话报 "There was a network issue connecting to the server"。
STALE_AGY_IDLE_SECONDS = 30 * 60
_STALE_AGY_EXE_RE = re.compile(r"(?:^|/)agy\.\d+\.old(?:\s+\(deleted\))?$")

VERDICT_LABELS = {
    PROBE_OK: "ok",
    PROBE_GEO_BLOCKED: "geo_blocked",
    PROBE_DEAD: "dead",
}


@dataclass
class WatchGroupResult:
    group: str
    current: str | None
    verdict: str
    detail: str = ""
    switched: bool = False
    target: str | None = None


@dataclass
class WatchReport:
    results: list[WatchGroupResult] = field(default_factory=list)
    reaped_pids: list[int] = field(default_factory=list)


def _read_ppid_and_uid(proc_dir: Path) -> tuple[int | None, int | None]:
    ppid: int | None = None
    uid: int | None = None
    try:
        text = (proc_dir / "status").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None, None
    for line in text.splitlines():
        if line.startswith("PPid:"):
            parts = line.split()
            if len(parts) >= 2 and parts[1].isdigit():
                ppid = int(parts[1])
        elif line.startswith("Uid:"):
            parts = line.split()
            if len(parts) >= 2 and parts[1].isdigit():
                uid = int(parts[1])
        if ppid is not None and uid is not None:
            break
    return ppid, uid


def _ancestor_pids(proc_root: Path, start_pid: int | None = None) -> set[int]:
    pid = os.getpid() if start_pid is None else start_pid
    ancestors: set[int] = set()
    while pid and pid > 1 and pid not in ancestors:
        ancestors.add(pid)
        ppid, _ = _read_ppid_and_uid(proc_root / str(pid))
        if ppid is None:
            break
        pid = ppid
    return ancestors


def reap_stale_agy_processes(
    proc_root: Path = Path("/proc"),
    *,
    now: float | None = None,
    idle_seconds: int = STALE_AGY_IDLE_SECONDS,
    dry_run: bool = False,
    kill_fn: Callable[[int, int], None] = os.kill,
    current_uid: int | None = None,
    self_pid: int | None = None,
) -> list[int]:
    """清理已被热更新替换且终端长期闲置的 `agy.<timestamp>.old` 残留进程。

    三重安全保护（防止误杀正在使用的会话）：
    1. 绝不触碰当前进程及任何祖先进程；
    2. 若存在非 `statusline` 的活跃子进程（正在跑命令/子任务），跳过；
    3. 若控制终端（`fd/0`）存在且最近输入时间距今不足 `idle_seconds`（默认 30 分钟），跳过。
    """
    if not proc_root.is_dir():
        return []
    ts_now = time.time() if now is None else now
    uid_target = os.getuid() if current_uid is None else current_uid
    protected = _ancestor_pids(proc_root, start_pid=self_pid)

    children_by_ppid: dict[int, list[tuple[int, str]]] = {}
    candidates: list[tuple[int, Path]] = []

    try:
        entries = list(proc_root.iterdir())
    except OSError:
        return []

    for entry in entries:
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        ppid, uid = _read_ppid_and_uid(entry)
        try:
            cmdline = (entry / "cmdline").read_text(encoding="utf-8", errors="replace").replace("\x00", " ").strip()
        except OSError:
            cmdline = ""
        if ppid is not None:
            children_by_ppid.setdefault(ppid, []).append((pid, cmdline))

        if uid is not None and uid != uid_target:
            continue
        try:
            exe_target = os.readlink(entry / "exe")
        except OSError:
            continue
        if _STALE_AGY_EXE_RE.search(exe_target):
            candidates.append((pid, entry))

    reaped: list[int] = []
    for pid, entry in sorted(candidates, key=lambda item: item[0]):
        if pid in protected:
            continue
        active_children = [
            child_pid
            for child_pid, child_cmd in children_by_ppid.get(pid, [])
            if "statusline" not in child_cmd
        ]
        if active_children:
            continue
        try:
            tty_stat = os.stat(entry / "fd" / "0")
        except OSError:
            tty_stat = None
        if tty_stat is not None and (ts_now - float(tty_stat.st_atime)) < idle_seconds:
            continue
        if not dry_run:
            try:
                kill_fn(pid, signal.SIGTERM)
            except OSError:
                continue
        reaped.append(pid)
    return reaped


def _watch_url(paths: AppPaths, group: str) -> str:
    """AI-MANUAL 用 ChatGPT Web URL，AI-GEMINI 用 Gemini API URL（均可被 config 覆盖）。

    codex 无干净可测的语义失败信号，改用 chatgpt.com 首页（返回 200）做可达性判定，
    而非 codex 的 POST 端点（GET 必返 4xx，会被语义分类永远判 ok，看门狗形同虚设）。
    """
    config = read_config(paths)
    if group == AI_GEMINI_GROUP:
        return str(config.get("ai-gemini-api-url") or DEFAULT_AI_PROBE_TARGETS[2][1])
    return str(config.get("ai-chatgpt-url") or DEFAULT_AI_PROBE_TARGETS[0][1])


def _load_state(paths: AppPaths) -> dict:
    try:
        data = json.loads(ai_watch_state_file(paths).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_state(paths: AppPaths, state: dict) -> None:
    path = ai_watch_state_file(paths)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def _group_state(state: dict, group: str) -> dict:
    item = state.get(group)
    if not isinstance(item, dict):
        item = {}
        state[group] = item
    item.setdefault("fail_node", None)
    item.setdefault("fail_count", 0)
    item.setdefault("cooldown", {})
    return item


class AIWatchService:
    def __init__(self, paths: AppPaths, proc_root: Path = Path("/proc")):
        self.paths = paths
        self.query = QueryService(paths)
        self.proc_root = proc_root

    def watch(self, dry_run: bool = False) -> WatchReport:
        report = WatchReport()
        try:
            groups = self.query.api.get_groups()
        except Exception:
            # 代理未运行：不探测、不写状态，返回空报告
            return report

        state = _load_state(self.paths)
        now = time.time()
        for group in (AI_MANUAL_GROUP, AI_GEMINI_GROUP):
            report.results.append(self._watch_group(groups, group, state, now, dry_run))
        report.reaped_pids = reap_stale_agy_processes(self.proc_root, now=now, dry_run=dry_run)
        if not dry_run:
            _save_state(self.paths, state)
        return report

    def _watch_group(
        self,
        groups: dict,
        group: str,
        state: dict,
        now: float,
        dry_run: bool,
    ) -> WatchGroupResult:
        group_obj = groups.get(group)
        current = resolve_current_leaf(groups, group) if group_obj else None
        try:
            verdict = ai_target_verdict(self.paths, _watch_url(self.paths, group))
        except Exception as exc:
            verdict, detail = PROBE_DEAD, str(exc)
        else:
            detail = VERDICT_LABELS.get(verdict, verdict)

        result = WatchGroupResult(group=group, current=current, verdict=verdict, detail=detail)
        gs = _group_state(state, group)

        # 自动模式（AI-MANUAL 当前是 AI-AUTO）由自动池自身 fallback 兜底，看门狗不手动切，
        # 否则会把用户手动选的自动模式静默踢成固定叶子且无返回路径
        if group == AI_MANUAL_GROUP and group_obj is not None and group_obj.current == AI_AUTO_GROUP:
            return result

        if verdict == PROBE_OK:
            gs["fail_node"] = None
            gs["fail_count"] = 0
            return result

        # 语义失败：记录连续失败计数
        if gs.get("fail_node") == current:
            gs["fail_count"] = int(gs.get("fail_count", 0)) + 1
        else:
            gs["fail_node"] = current
            gs["fail_count"] = 1

        if gs["fail_count"] >= CONSECUTIVE_FAILURE_THRESHOLD and group_obj is not None and not dry_run:
            target = self._pick_next(groups, group_obj, current, gs.get("cooldown", {}), now)
            if target is not None:
                try:
                    self.query.switch_group(group, target)
                except Exception:
                    # 切换失败（API 抖动等）：保留失败计数，下一轮重试，不让异常中断循环、丢状态
                    return result
                cooldown = gs.setdefault("cooldown", {})
                if current:
                    cooldown[current] = now
                gs["fail_node"] = None
                gs["fail_count"] = 0
                result.switched = True
                result.target = target
        return result

    def _pick_next(self, groups: dict, group, current: str | None, cooldown: dict, now: float) -> str | None:
        """从组候选里挑下一个可切叶子：跳过当前节点、冷却期节点与嵌套组（AI-AUTO 等）。

        mihomo `/proxies` 对每个叶子节点也返回一条 `candidates=[]` 的条目，因此必须用
        `bool(entry.candidates)` 判嵌套组（与 `resolve_ai_leaf` 一致），而不能用
        `cand in groups`。同时优先挑选健康检查存活（非 `alive=False`/超时）的叶子，
        仅当所有非冷却叶子都被标死时才回退到首个可用候选。
        """
        fallback_leaf: str | None = None
        for cand in group.candidates:
            if not cand or cand == current:
                continue
            entry = groups.get(cand)
            if entry is not None and entry.candidates:
                continue
            ts = cooldown.get(cand)
            if ts is not None and now - float(ts) < COOLDOWN_SECONDS:
                continue
            if entry is not None and (entry.alive is False or delay_state(entry.delay) == "timeout"):
                if fallback_leaf is None:
                    fallback_leaf = str(cand)
                continue
            return str(cand)
        return fallback_leaf
