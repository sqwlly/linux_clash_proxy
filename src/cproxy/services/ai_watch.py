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


DEFAULT_CLOUDCODE_URL = "https://daily-cloudcode-pa.googleapis.com"


def _watch_urls(paths: AppPaths, group: str) -> tuple[str, ...]:
    """AI-MANUAL 测 ChatGPT Web + OpenAI API，AI-GEMINI 测 Gemini API + Cloud Code。

    双端点看护：
    - AI-GEMINI 同时覆盖 Gemini API（识别地区门禁）与 agy 实际连接的
      `daily-cloudcode-pa.googleapis.com`（识别 Cloud Code 链路断流）；
    - AI-MANUAL 同时覆盖 `chatgpt.com` 与 `api.openai.com/v1/models`。
    """
    config = read_config(paths)
    if group == AI_GEMINI_GROUP:
        primary = str(config.get("ai-gemini-api-url") or DEFAULT_AI_PROBE_TARGETS[2][1])
        secondary = str(config.get("ai-cloudcode-url") or DEFAULT_CLOUDCODE_URL)
        return (primary, secondary) if primary != secondary else (primary,)
    primary = str(config.get("ai-chatgpt-url") or DEFAULT_AI_PROBE_TARGETS[0][1])
    secondary = str(config.get("ai-openai-api-url") or DEFAULT_AI_PROBE_TARGETS[1][1])
    return (primary, secondary) if primary != secondary else (primary,)


def _watch_url(paths: AppPaths, group: str) -> str:
    return _watch_urls(paths, group)[0]


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


_AGY_RETRY_FAIL_RE = re.compile(
    r"^I(\d{2})(\d{2})\s+(\d{2}):(\d{2}):(\d{2})\.(\d+)\s+\d+\s+run\.go:\d+\]\s+"
    r"Run:\s+attempt\s+(\d+)\s+failed\s+\((.+?)\),\s+retrying\s+in\b"
)
AGY_LOG_WINDOW_SECONDS = 180.0


def default_agy_log_dir(paths: AppPaths) -> Path:
    return paths.state_dir.parent.parent / ".gemini" / "antigravity-cli" / "log"


def detect_recent_agy_retry_failure(
    log_dir: Path | None,
    *,
    now: float | None = None,
    window_seconds: float = AGY_LOG_WINDOW_SECONDS,
    since_ts: float = 0.0,
) -> tuple[bool, float, str]:
    """扫描所有近期活跃的 agy `cli-*.log` 尾部，检测 `run.go` 是否刚出现流式重试失败。

    当同一台机器同时开启多个 agy 终端会话时，后台遥测（如 `listExperiments`）会不断刷新各
    日志文件的 mtime；若只看 `logs[-1]` 会漏掉另一会话里正在发生的连续 EOF 重试。
    这里扫描全部近期更新过的 `cli-*.log`（最多取最近 10 个），返回最新的一次失败事件。
    """
    if log_dir is None or not log_dir.is_dir():
        return False, since_ts, ""
    ts_now = time.time() if now is None else now
    try:
        logs = sorted(log_dir.glob("cli-*.log"), key=lambda p: (p.stat().st_mtime, p.name))
    except OSError:
        return False, since_ts, ""
    if not logs:
        return False, since_ts, ""

    local_now = time.localtime(ts_now)
    newest_ts = since_ts
    newest_reason = ""
    found = False

    for log_path in reversed(logs[-10:]):
        try:
            with log_path.open("rb") as fh:
                fh.seek(0, os.SEEK_END)
                size = fh.tell()
                fh.seek(max(0, size - 65536), os.SEEK_SET)
                tail = fh.read().decode("utf-8", errors="replace")
        except OSError:
            continue

        for line in reversed(tail.splitlines()):
            m = _AGY_RETRY_FAIL_RE.match(line.strip())
            if not m:
                continue
            month, day, hour, minute, sec, usec, attempt, reason = m.groups()
            year = local_now.tm_year
            if int(month) == 12 and local_now.tm_mon == 1:
                year -= 1
            try:
                struct_t = time.strptime(
                    f"{year:04d}-{int(month):02d}-{int(day):02d} {int(hour):02d}:{int(minute):02d}:{int(sec):02d}",
                    "%Y-%m-%d %H:%M:%S",
                )
                event_ts = time.mktime(struct_t) + int(usec[:6].ljust(6, "0")) / 1_000_000.0
            except ValueError:
                continue
            if event_ts <= newest_ts:
                break
            age = ts_now - event_ts
            if -5.0 <= age <= window_seconds:
                newest_ts = event_ts
                newest_reason = f"attempt {attempt}: {reason}"
                found = True
            break

    if found:
        return True, newest_ts, newest_reason
    return False, since_ts, ""


class AIWatchService:
    def __init__(
        self,
        paths: AppPaths,
        proc_root: Path = Path("/proc"),
        agy_log_dir: Path | None = None,
    ):
        self.paths = paths
        self.query = QueryService(paths)
        self.proc_root = proc_root
        self.agy_log_dir = default_agy_log_dir(paths) if agy_log_dir is None else agy_log_dir

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

    def _probe_group_verdict(self, group: str) -> tuple[str, str]:
        for url in _watch_urls(self.paths, group):
            try:
                verdict = ai_target_verdict(self.paths, url)
            except Exception as exc:
                return PROBE_DEAD, str(exc)
            if verdict != PROBE_OK:
                return verdict, VERDICT_LABELS.get(verdict, verdict)
        return PROBE_OK, VERDICT_LABELS[PROBE_OK]

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
        verdict, detail = self._probe_group_verdict(group)

        gs = _group_state(state, group)
        log_triggered = False
        if group == AI_GEMINI_GROUP:
            since_ts = float(gs.get("last_log_fail_ts") or 0.0)
            log_failed, log_ts, log_reason = detect_recent_agy_retry_failure(
                self.agy_log_dir, now=now, since_ts=since_ts
            )
            if log_failed:
                gs["last_log_fail_ts"] = log_ts
                verdict = PROBE_DEAD
                detail = f"agy_log_retry ({log_reason})"
                log_triggered = True

        result = WatchGroupResult(group=group, current=current, verdict=verdict, detail=detail)

        # 若 mihomo fallback 被动切换了出口叶子，清理仍滞留在旧叶子上的陈旧长连接，
        # 避免同一会话混用新旧出口 IP；若旧叶子已被健康检查标死/超时，同步写入冷却期并把
        # fallback 的 `fixed` 指针更新到新叶子，防止旧叶子短暂回包后 mihomo 瞬间跳回造成乒乓抖动。
        previous_active = gs.get("last_active")
        passive_dead_switch = False
        if current:
            if not dry_run and previous_active and previous_active != current:
                drop_fn = getattr(self.query, "_drop_stale_ai_connections", None)
                if callable(drop_fn):
                    try:
                        drop_fn(group, keep_leaf=current)
                    except TypeError:
                        drop_fn(group)
                prev_entry = groups.get(previous_active)
                if (
                    prev_entry is not None
                    and not prev_entry.candidates
                    and (prev_entry.alive is False or delay_state(prev_entry.delay) == "timeout")
                ):
                    gs.setdefault("cooldown", {})[previous_active] = now
                    passive_dead_switch = True
            gs["last_active"] = current

        # 自动模式（AI-MANUAL 当前是 AI-AUTO）由自动池自身 fallback 兜底，看门狗不手动切，
        # 否则会把用户手动选的自动模式静默踢成固定叶子且无返回路径
        if group == AI_MANUAL_GROUP and group_obj is not None and group_obj.current == AI_AUTO_GROUP:
            return result

        if verdict == PROBE_OK:
            if passive_dead_switch and not dry_run and group_obj is not None and current in group_obj.candidates:
                api_switch = getattr(self.query.api, "switch_group", None)
                if callable(api_switch):
                    try:
                        api_switch(group, current)
                    except Exception:
                        pass
            gs["fail_node"] = None
            gs["fail_count"] = 0
            return result

        # 语义失败：记录连续失败计数；若由客户端真实重试日志触发，则直接达到切换阈值立即自愈
        if log_triggered:
            gs["fail_node"] = current
            gs["fail_count"] = max(int(gs.get("fail_count", 0)) + 1, CONSECUTIVE_FAILURE_THRESHOLD)
        elif gs.get("fail_node") == current:
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
                gs["last_active"] = target
                result.switched = True
                result.target = target
                try:
                    from .notify import send_notification

                    send_notification(
                        self.paths,
                        "cproxy AI 出口自动切换",
                        f"{group}: {current or '-'} → {target}（原因: {detail}）",
                        event="ai_watch_switch",
                    )
                except Exception:
                    pass
        return result

    def _pick_next(self, groups: dict, group, current: str | None, cooldown: dict, now: float) -> str | None:
        """从组候选里挑下一个可切叶子：跳过当前节点、屏蔽节点、冷却期节点与嵌套组（AI-AUTO 等）。

        mihomo `/proxies` 对每个叶子节点也返回一条 `candidates=[]` 的条目，因此必须用
        `bool(entry.candidates)` 判嵌套组（与 `resolve_ai_leaf` 一致），而不能用
        `cand in groups`。同时优先挑选健康检查存活（非 `alive=False`/超时）的叶子，
        仅当所有非冷却叶子都被标死时才回退到首个可用候选。
        """
        from .ban import is_node_banned, load_active_bans

        active_bans = load_active_bans(self.paths, now=now)
        fallback_leaf: str | None = None
        for cand in group.candidates:
            if not cand or cand == current:
                continue
            if active_bans and is_node_banned(str(cand), active_bans):
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
