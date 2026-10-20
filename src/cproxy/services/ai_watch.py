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
from ..config import AppPaths, ai_gemini_order_file, ai_manual_order_file, ai_watch_state_file, read_config
from .diagnostics import DEFAULT_AI_PROBE_TARGETS, PROBE_DEAD, PROBE_GEO_BLOCKED, PROBE_OK, ai_target_verdict
from .probe import (
    STREAM_FRAGILE_TTL_SECONDS,
    is_dynamic_stream_fragile,
    is_iepl_node,
    is_static_iepl_node,
    probe_paths_context,
    resolve_current_leaf,
)
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
    reaped_connections: list[str] = field(default_factory=list)


_SS_LASTSND_RE = re.compile(r"\blastsnd:(\d+)\b")
_SS_LASTRCV_RE = re.compile(r"\blastrcv:(\d+)\b")

GEMINI_STREAM_HOSTS = frozenset(
    {
        "daily-cloudcode-pa.googleapis.com",
    }
)

AI_STREAM_HOSTS = frozenset(
    {
        "daily-cloudcode-pa.googleapis.com",
        "chatgpt.com",
        "api.openai.com",
        "api.anthropic.com",
        "claude.ai",
    }
)


_LAST_SOCKET_QUEUE_MAP: dict[str, tuple[int, int]] = {}


def get_local_proxy_socket_idle_ms(mixed_port: int = 7890) -> dict[str, tuple[int, int]]:
    """通过 `ss -tnpi` 获取本地代理端口（默认 :7890）各客户端源端口的 `(lastsnd_ms, lastrcv_ms)`，
    并同步记录各套接字的内核队列深度 `(recv_q_bytes, send_q_bytes)`。"""
    import subprocess

    _LAST_SOCKET_QUEUE_MAP.clear()
    try:
        proc = subprocess.run(
            ["ss", "-tnpi", f"( sport = :{int(mixed_port)} )"],
            capture_output=True,
            text=True,
            timeout=2.0,
            check=False,
        )
        if proc.returncode != 0 or not proc.stdout:
            return {}
    except Exception:
        return {}

    idle_map: dict[str, tuple[int, int]] = {}
    pending_port: str | None = None
    pending_queues: tuple[int, int] = (0, 0)
    for raw_line in proc.stdout.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("ESTAB"):
            parts = line.split()
            pending_port = None
            pending_queues = (0, 0)
            if len(parts) >= 5:
                peer_port = parts[4].rsplit(":", 1)[-1]
                if peer_port.isdigit():
                    pending_port = peer_port
                    try:
                        pending_queues = (max(0, int(parts[1])), max(0, int(parts[2])))
                    except ValueError:
                        pending_queues = (0, 0)
        elif pending_port is not None:
            m_snd = _SS_LASTSND_RE.search(line)
            m_rcv = _SS_LASTRCV_RE.search(line)
            if m_snd is not None and m_rcv is not None:
                idle_map[pending_port] = (int(m_snd.group(1)), int(m_rcv.group(1)))
                _LAST_SOCKET_QUEUE_MAP[pending_port] = pending_queues
            pending_port = None
    return idle_map


def _unpack_socket_timings(
    timings: tuple[int, ...] | None,
    src_port: str = "",
) -> tuple[int, int, int, int] | None:
    if timings is None or len(timings) < 2:
        return None
    lastsnd_ms = int(timings[0])
    lastrcv_ms = int(timings[1])
    if len(timings) >= 4:
        return lastsnd_ms, lastrcv_ms, max(0, int(timings[2])), max(0, int(timings[3]))
    q = _LAST_SOCKET_QUEUE_MAP.get(src_port, (0, 0))
    return lastsnd_ms, lastrcv_ms, q[0], q[1]


def classify_ai_connection_lifecycle(
    upload: int,
    download: int,
    lastsnd_ms: int,
    lastrcv_ms: int,
    *,
    on_stale_leaf: bool = False,
    recv_q: int = 0,
    send_q: int = 0,
) -> str:
    """基于内核 TCP 队列深度 (Recv-Q/Send-Q)、双方向字节数与收发时序的确定性 AI 连接生命周期状态机。

    返回值：
    - `"uploading"`: 客户端已将请求写入本地代理但内核 Recv-Q 尚未排空，或正在持续发送请求体；
    - `"thinking"`: 已上传完整上下文 (`upload >= 18KB`)，正在等待远端模型返回本轮 HTTP 200 响应体；
    - `"streaming"`: 正在流式接收 SSE / WebSocket 响应（`Send-Q > 0` 或近期有下行数据包）；
    - `"idle_completed"`: 请求响应已完整结束且双方向静默达到连接池老化回收阈值；
    - `"idle_other"`: 其它未承载活跃大包轮次的轻量化/初建连接。
    """
    if recv_q > 0:
        return "uploading"
    if send_q > 0:
        return "streaming"
    idle_ms = min(lastsnd_ms, lastrcv_ms)
    if 0 < upload < 18_000:
        if download >= 4_000 and idle_ms >= 6_000:
            return "idle_completed"
        return "idle_other"
    if upload >= 18_000:
        threshold_ms = 10_000 if on_stale_leaf else 35_000
        if download < 6_000 or (lastrcv_ms - lastsnd_ms) < 400:
            if min(lastsnd_ms, lastrcv_ms) <= 5_000 and download < 6_000:
                return "uploading"
            if idle_ms <= 300_000:
                return "thinking"
            return "stalled_thinking"
        if idle_ms < threshold_ms:
            return "streaming"
        return "idle_completed"
    return "idle_other"


def is_inflight_ai_connection(
    upload: int,
    download: int,
    lastsnd_ms: int,
    lastrcv_ms: int,
    *,
    on_stale_leaf: bool = False,
    recv_q: int = 0,
    send_q: int = 0,
) -> bool:
    """判定连接是否正处于上传、模型思考或流式输出阶段（严禁在切流或回收时关闭）。"""
    return classify_ai_connection_lifecycle(
        upload,
        download,
        lastsnd_ms,
        lastrcv_ms,
        on_stale_leaf=on_stale_leaf,
        recv_q=recv_q,
        send_q=send_q,
    ) in ("uploading", "thinking", "streaming")


def is_idle_completed_gemini_connection(
    upload: int,
    download: int,
    lastsnd_ms: int,
    lastrcv_ms: int,
    *,
    on_stale_leaf: bool = False,
    recv_q: int = 0,
    send_q: int = 0,
) -> bool:
    """判定 AI HTTP/2 连接是否已完成请求响应并处于连接池闲置状态。"""
    return (
        classify_ai_connection_lifecycle(
            upload,
            download,
            lastsnd_ms,
            lastrcv_ms,
            on_stale_leaf=on_stale_leaf,
            recv_q=recv_q,
            send_q=send_q,
        )
        == "idle_completed"
    )


def reap_idle_gemini_connections(
    paths: AppPaths,
    api,
    *,
    current_leaf: str | None = None,
    manual_leaf: str | None = None,
    dry_run: bool = False,
) -> list[str]:
    """主动清理 agy / codex / claude 连接池中闲置的 AI HTTP/2 连接，
    防止下一轮长思考或流式请求复用半衰期连接撞上 Cloudflare 94s 空闲超时。"""
    get_conns = getattr(api, "get_connections", None)
    close_conn = getattr(api, "close_connection", None)
    if not callable(get_conns) or not callable(close_conn):
        return []
    try:
        payload = get_conns()
    except Exception:
        return []
    connections = payload.get("connections") if isinstance(payload, dict) else []
    if not isinstance(connections, list) or not connections:
        return []

    candidates: list[tuple[str, int, int, str, bool]] = []
    for item in connections:
        if not isinstance(item, dict):
            continue
        conn_id = str(item.get("id") or "")
        if not conn_id:
            continue
        raw_meta = item.get("metadata")
        meta = raw_meta if isinstance(raw_meta, dict) else {}
        host = str(meta.get("host") or "").lower()
        if host not in AI_STREAM_HOSTS:
            continue
        src_port = str(meta.get("sourcePort") or "")
        if not src_port.isdigit():
            continue
        try:
            upload = int(item.get("upload") or 0)
            download = int(item.get("download") or 0)
        except (TypeError, ValueError):
            continue
        chains = item.get("chains") or []
        chain_set = {str(part) for part in chains} if isinstance(chains, list) else set()
        leaf = str(chains[0]) if isinstance(chains, list) and chains else ""
        active_leaf = manual_leaf if AI_MANUAL_GROUP in chain_set and manual_leaf else current_leaf
        on_stale_leaf = bool(active_leaf and leaf and leaf != active_leaf)
        # 在当前活跃叶子上，仅主动回收 Gemini HTTP/2 连接池与轻量化轮询（upload < 18KB），
        # 绝不主动强杀活跃叶子上 codex / claude 的大包长连接（防止多步推理/工具调用间隙断流）
        if not on_stale_leaf and host not in GEMINI_STREAM_HOSTS and upload >= 18_000:
            continue
        candidates.append((conn_id, upload, download, src_port, on_stale_leaf))

    if not candidates:
        return []

    mixed_port = 7890
    try:
        cfg = read_config(paths)
        if isinstance(cfg, dict) and cfg.get("mixed-port"):
            mixed_port = int(cfg["mixed-port"])
    except Exception:
        mixed_port = 7890

    idle_map = get_local_proxy_socket_idle_ms(mixed_port)
    if not idle_map:
        return []

    reaped: list[str] = []
    for conn_id, upload, download, src_port, on_stale_leaf in candidates:
        unpacked = _unpack_socket_timings(idle_map.get(src_port), src_port)
        if unpacked is None:
            continue
        lastsnd_ms, lastrcv_ms, recv_q, send_q = unpacked
        if is_idle_completed_gemini_connection(
            upload,
            download,
            lastsnd_ms,
            lastrcv_ms,
            on_stale_leaf=on_stale_leaf,
            recv_q=recv_q,
            send_q=send_q,
        ):
            if not dry_run:
                try:
                    close_conn(conn_id)
                except Exception:
                    continue
            reaped.append(conn_id)
    return reaped


def has_active_ai_transfer(
    paths: AppPaths,
    api,
    group: str,
    current_leaf: str | None = None,
) -> bool:
    """检测指定 AI 出口组当前是否正有大包上下文上传或活跃流式响应在进行中。

    当 codex 多线程并发上传 3~9MB 上下文或 agy 上传 1~4MB 轨迹时，5Mbps 公网上行会被持续打满
    20~45s（内核 Send-Q 堆积 1.5MB+），新发起的 5s 看门狗探针会排队超时而误判 `PROBE_DEAD`。
    若当前叶子上的 AI 连接已上传 `>= 50KB` 且内核 TCP 套接字在最近 `15s` 内仍有真实数据收发
    （`min(lastsnd_ms, lastrcv_ms) <= 15_000`），说明链路正在全力传输数据而非死节点，暂缓切流。
    """
    get_conns = getattr(api, "get_connections", None)
    if not callable(get_conns):
        return False
    try:
        payload = get_conns()
    except Exception:
        return False
    connections = payload.get("connections") if isinstance(payload, dict) else []
    if not isinstance(connections, list) or not connections:
        return False

    ports_to_check: list[tuple[str, int, int]] = []
    for item in connections:
        if not isinstance(item, dict) or not QueryService._connection_follows_ai_switch(item, group):
            continue
        chains = item.get("chains") or []
        leaf = str(chains[0]) if isinstance(chains, list) and chains else ""
        if current_leaf and leaf and leaf != current_leaf:
            continue
        raw_meta = item.get("metadata")
        meta = raw_meta if isinstance(raw_meta, dict) else {}
        host = str(meta.get("host") or "").lower()
        if host not in AI_STREAM_HOSTS:
            continue
        src_port = str(meta.get("sourcePort") or "")
        if not src_port.isdigit():
            continue
        try:
            upload = int(item.get("upload") or 0)
            download = int(item.get("download") or 0)
        except (TypeError, ValueError):
            continue
        if upload >= 50_000:
            ports_to_check.append((src_port, upload, download))

    if not ports_to_check:
        return False

    mixed_port = 7890
    try:
        cfg = read_config(paths)
        if isinstance(cfg, dict) and cfg.get("mixed-port"):
            mixed_port = int(cfg["mixed-port"])
    except Exception:
        mixed_port = 7890

    idle_map = get_local_proxy_socket_idle_ms(mixed_port)
    if not idle_map:
        return False

    for src_port, upload, download in ports_to_check:
        unpacked = _unpack_socket_timings(idle_map.get(src_port), src_port)
        if unpacked is None:
            continue
        lastsnd_ms, lastrcv_ms, recv_q, send_q = unpacked
        if is_inflight_ai_connection(
            upload,
            download,
            lastsnd_ms,
            lastrcv_ms,
            recv_q=recv_q,
            send_q=send_q,
        ):
            return True
    return False


def has_any_inflight_ai_connection(paths: AppPaths, api=None) -> bool:
    """检测任意 AI 策略组（AI-MANUAL / AI-GEMINI）是否存在正在上传、模型思考或流式输出中的活跃连接。
    用于拦截周期性 `cproxy render --apply` 与 `rebalance` 的破坏性 `PUT /configs?force=true` 热重载。
    """
    if api is None:
        try:
            from ..backend.api import APIBackend

            api = APIBackend(paths)
        except Exception:
            return False
    return has_active_ai_transfer(paths, api, AI_MANUAL_GROUP) or has_active_ai_transfer(
        paths, api, AI_GEMINI_GROUP
    )


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
    if paths.state_dir.parent.name == "state" and paths.state_dir.parent.parent.name == ".local":
        base = paths.state_dir.parent.parent.parent
    else:
        base = paths.state_dir.parent.parent
    return base / ".gemini" / "antigravity-cli" / "log"


_AGY_GLOG_TS_RE = re.compile(r"^I(\d{2})(\d{2})\s+(\d{2}):(\d{2}):(\d{2})\.(\d+)\s+")


def _parse_glog_timestamp(line: str, local_now: time.struct_time) -> float | None:
    m = _AGY_GLOG_TS_RE.match(line)
    if not m:
        return None
    month, day, hour, minute, sec, usec = m.groups()
    year = local_now.tm_year
    if int(month) == 12 and local_now.tm_mon == 1:
        year -= 1
    try:
        struct_t = time.strptime(
            f"{year:04d}-{int(month):02d}-{int(day):02d} {int(hour):02d}:{int(minute):02d}:{int(sec):02d}",
            "%Y-%m-%d %H:%M:%S",
        )
        return time.mktime(struct_t) + int(usec[:6].ljust(6, "0")) / 1_000_000.0
    except ValueError:
        return None


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
    这里扫描全部近期更新过的 `cli-*.log`（最多取最近 10 个），返回最新的一次失败事件；
    同时记录任一会话收到 `streamGenerateContent` `ResponseID` 的最新成功时间戳——若在失败事件
    之后已有任一会话建连成功，说明当前出口节点本身存活，不因单会话滞后重试而切走全局节点。
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
    newest_success_ts = 0.0
    candidates: list[tuple[float, int, str]] = []

    for log_path in reversed(logs[-10:]):
        try:
            with log_path.open("rb") as fh:
                fh.seek(0, os.SEEK_END)
                size = fh.tell()
                fh.seek(max(0, size - 262144), os.SEEK_SET)
                tail = fh.read().decode("utf-8", errors="replace")
        except OSError:
            continue

        for line in reversed(tail.splitlines()):
            stripped = line.strip()
            if "streamGenerateContent" in stripped and "ResponseID:" in stripped:
                ok_ts = _parse_glog_timestamp(stripped, local_now)
                if ok_ts is not None and ok_ts > newest_success_ts:
                    newest_success_ts = ok_ts
                # 该会话在更晚时间点已成功收到 streamGenerateContent 响应头（正在流式输出或已完成），
                # 绝不能因更早前的 attempt 失败日志而切节点掐断当前正在进行的流
                break
            m = _AGY_RETRY_FAIL_RE.match(stripped)
            if not m:
                continue
            month, day, hour, minute, sec, usec, attempt, reason = m.groups()
            reason_lower = reason.lower()
            # stream reading error: unexpected EOF 说明节点已成功建连、上传完 Prompt 并拿到 HTTP 200 ResponseID，
            # 仅在超长思考/生成流式读取中途断流；此时节点本身可达，切走反而会落入高延迟兜底节点并打断其它活跃会话。
            if "stream reading error" in reason_lower:
                break
            # agy 自身对偶发单次 EOF 具备 4s/6s 静默指数退避自愈能力（attempt 1/2 不会向用户报错）。
            # 单次请求的重试链（attempt 1..10）跨越约 7 分钟：
            # - attempt == 3 时对易断流节点立即自愈；
            # - attempt >= 4 时说明即便在常规/1X 节点上也已连续多次建连/发包失败，无论节点名称是否含 IEPL 均触发自愈，
            #   且不被其它并发会话的小包监控请求（sir_flash_lite ResponseID）掩盖。
            attempt_num = int(attempt)
            if attempt_num not in (3, 4) and "bad record mac" not in reason_lower:
                continue
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
            if event_ts <= since_ts:
                break
            age = ts_now - event_ts
            if -5.0 <= age <= window_seconds:
                candidates.append((event_ts, attempt_num, f"attempt {attempt_num}: {reason}"))
            break

    max_seen_ts = max([since_ts] + [c[0] for c in candidates])
    triggered = [
        c
        for c in candidates
        if c[1] >= 4 or "bad record mac" in c[2].lower() or c[0] > newest_success_ts
    ]
    if triggered:
        best = max(
            triggered,
            key=lambda c: (1 if (c[1] >= 4 or "bad record mac" in c[2].lower()) else 0, c[0]),
        )
        return True, best[0], best[2]
    return False, max_seen_ts, ""


_CODEX_RETRIES_RE = re.compile(r"\bretries=(\d+)\b")
_CODEX_SAMPLING_ERR_RE = re.compile(r"\bsampling_error=(.+)$")
_CODEX_CONN_ERR_RE = re.compile(r"\berror=(.+?)(?:\s+retry_delay=|$)")


def default_codex_log_dir(paths: AppPaths) -> Path:
    if paths.state_dir.parent.name == "state" and paths.state_dir.parent.parent.name == ".local":
        base = paths.state_dir.parent.parent.parent
    else:
        base = paths.state_dir.parent.parent
    return base / ".codex"


def detect_recent_codex_retry_failure(
    codex_dir: Path | None,
    *,
    now: float | None = None,
    window_seconds: float = AGY_LOG_WINDOW_SECONDS,
    since_ts: float = 0.0,
) -> tuple[bool, float, str]:
    """扫描近期活跃的 `~/.codex/logs_*.sqlite` 尾部，检测 Codex 是否刚出现流式断连或发包重试失败。

    使用只读 URI 模式（`mode=ro`）与主键尾部窗口（`id > max(id) - 500`）做亚毫秒级查询，
    绝不阻塞或争用正在运行的 codex 进程写锁；若在失败事件之后已有新的 `stream_events_utils`
    成功事件，说明当前出口已恢复推流，不误切节点。
    """
    if codex_dir is None or not codex_dir.is_dir():
        return False, since_ts, ""
    import sqlite3

    ts_now = time.time() if now is None else now
    try:
        dbs = sorted(codex_dir.glob("logs_*.sqlite"), key=lambda p: (p.stat().st_mtime, p.name))
    except OSError:
        return False, since_ts, ""
    if not dbs:
        return False, since_ts, ""

    newest_success_ts = 0.0
    candidates: list[tuple[float, int, str]] = []

    for db_path in reversed(dbs[-2:]):
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=0.5)
            try:
                cur = conn.cursor()
                cur.execute(
                    "SELECT ts, ts_nanos, target, feedback_log_body FROM logs "
                    "WHERE id > (SELECT coalesce(max(id), 0) - 500 FROM logs) "
                    "AND target IN ('codex_core::responses_retry', 'codex_core::stream_events_utils') "
                    "ORDER BY id DESC"
                )
                rows = cur.fetchall()
            finally:
                conn.close()
        except Exception:
            continue

        for raw_ts, raw_nanos, target, body in rows:
            try:
                event_ts = float(raw_ts) + float(raw_nanos or 0) / 1_000_000_000.0
            except (TypeError, ValueError):
                continue
            if target == "codex_core::stream_events_utils":
                if event_ts > newest_success_ts:
                    newest_success_ts = event_ts
                continue
            if target != "codex_core::responses_retry":
                continue
            if event_ts <= since_ts:
                break
            age = ts_now - event_ts
            if not (-5.0 <= age <= window_seconds):
                continue
            text = str(body or "")
            if "stream connection failed" in text:
                m_err = _CODEX_CONN_ERR_RE.search(text)
                err_msg = m_err.group(1).strip() if m_err else "stream connection failed"
                candidates.append((event_ts, 5, f"retries 5: {err_msg}"))
                break
            m_ret = _CODEX_RETRIES_RE.search(text)
            retries_num = int(m_ret.group(1)) if m_ret else 1
            if retries_num >= 2:
                m_serr = _CODEX_SAMPLING_ERR_RE.search(text)
                err_msg = m_serr.group(1).strip() if m_serr else "stream disconnected"
                candidates.append((event_ts, retries_num, f"retries {retries_num}: {err_msg}"))
                break

    max_seen_ts = max([since_ts] + [c[0] for c in candidates])
    triggered = [c for c in candidates if c[0] > newest_success_ts]
    if triggered:
        best = max(triggered, key=lambda c: (c[1], c[0]))
        return True, best[0], best[2]
    return False, max_seen_ts, ""


def _promote_gemini_order_head(paths: AppPaths, target: str, group: str = AI_GEMINI_GROUP) -> None:
    """将刚切中的 AI 健康目标节点同步提升到对应 order.json 首位，
    防止 15 分钟周期的 `cproxy render --apply` 把 fallback 组重新刷回刚故障的旧节点。"""
    if not target or is_static_iepl_node(target, paths):
        return
    order_path = ai_gemini_order_file(paths) if group == AI_GEMINI_GROUP else ai_manual_order_file(paths)
    if not order_path.exists():
        return
    try:
        data = json.loads(order_path.read_text(encoding="utf-8"))
        if not isinstance(data, list) or not data or data[0] == target:
            return
        if is_static_iepl_node(target, paths) and any(
            item and not is_static_iepl_node(str(item), paths) for item in data
        ):
            return
        updated = [target] + [str(item) for item in data if item and str(item) != target]
        order_path.write_text(json.dumps(updated, ensure_ascii=False), encoding="utf-8")
    except (OSError, ValueError):
        pass


def _is_stream_safe_gemini_leaf(paths: AppPaths, node_name: str | None) -> bool:
    """判断 AI-GEMINI 当前叶子是否为抗断流安全节点（1X / WebSocket / Hysteria2 / TUIC / SS 等非 IEPL/裸vision 节点）。

    兼容不同机场命名（包括完全无 IEPL 节点、或全部为普通命名的机场）：
    1. 若名称含 `IEPL` / `IPLC` / `内网专线`、或协议为裸 TCP `xtls-rprx-vision`，直接判为非安全；
    2. 查阅本地 `config.yaml` 的 `proxies` 协议字典：若能查到该节点且 `not is_stream_fragile_proxy(proxy_dict, node_name)`，视为安全节点；
    3. 若名称显式含 `1X`，判为安全。
    """
    if not node_name or is_static_iepl_node(node_name, paths):
        return False
    try:
        from ..backend.runtime import is_stream_fragile_proxy

        cfg = read_config(paths)
        for proxy in cfg.get("proxies") or []:
            if isinstance(proxy, dict) and str(proxy.get("name") or "") == node_name:
                return not is_stream_fragile_proxy(proxy, node_name)
    except Exception:
        pass
    if "1X" in str(node_name).upper():
        return True
    return False


class AIWatchService:
    def __init__(
        self,
        paths: AppPaths,
        proc_root: Path = Path("/proc"),
        agy_log_dir: Path | None = None,
        codex_log_dir: Path | None = None,
    ):
        self.paths = paths
        self.query = QueryService(paths)
        self.proc_root = proc_root
        self.agy_log_dir = default_agy_log_dir(paths) if agy_log_dir is None else agy_log_dir
        self.codex_log_dir = default_codex_log_dir(paths) if codex_log_dir is None else codex_log_dir

    def watch(self, dry_run: bool = False) -> WatchReport:
        report = WatchReport()
        try:
            groups = self.query.api.get_groups()
        except Exception:
            # 代理未运行：不探测、不写状态，返回空报告
            return report

        with probe_paths_context(self.paths):
            state = _load_state(self.paths)
            now = time.time()
            for group in (AI_MANUAL_GROUP, AI_GEMINI_GROUP):
                report.results.append(self._watch_group(groups, group, state, now, dry_run))
            gemini_res = next((r for r in report.results if r.group == AI_GEMINI_GROUP), None)
            gemini_leaf = (gemini_res.target or gemini_res.current) if gemini_res else None
            manual_res = next((r for r in report.results if r.group == AI_MANUAL_GROUP), None)
            manual_leaf = (manual_res.target or manual_res.current) if manual_res else None
            report.reaped_connections = reap_idle_gemini_connections(
                self.paths,
                getattr(self.query, "api", None),
                current_leaf=gemini_leaf,
                manual_leaf=manual_leaf,
                dry_run=dry_run,
            )
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
        previous_active = gs.get("last_active")
        log_triggered = False
        if group == AI_GEMINI_GROUP:
            # 若节点已由外部切换或自动优选变更，设 30s 静默保护窗，旧节点上的滞后重试报错不归咎于新节点
            if previous_active and current and previous_active != current:
                gs["last_log_fail_ts"] = max(float(gs.get("last_log_fail_ts") or 0.0), now + 30.0)
            since_ts = float(gs.get("last_log_fail_ts") or 0.0)
            log_failed, log_ts, log_reason = detect_recent_agy_retry_failure(
                self.agy_log_dir, now=now, since_ts=since_ts
            )
            if log_failed:
                on_healthy_safe_leaf = (
                    verdict == PROBE_OK
                    and _is_stream_safe_gemini_leaf(self.paths, current)
                )
                attempt_prefix = log_reason.split(":", 1)[0].strip()
                severe_attempt = (
                    attempt_prefix.startswith("attempt ")
                    and attempt_prefix[8:].isdigit()
                    and int(attempt_prefix[8:]) >= 4
                )
                is_severe_retry = "bad record mac" in log_reason.lower() or severe_attempt
                if not on_healthy_safe_leaf or is_severe_retry:
                    gs["last_log_fail_ts"] = log_ts
                    verdict = PROBE_DEAD
                    detail = f"agy_log_retry ({log_reason})"
                    log_triggered = True
                else:
                    gs["last_log_fail_ts"] = max(since_ts, log_ts)
        elif group == AI_MANUAL_GROUP:
            if previous_active and current and previous_active != current:
                gs["last_log_fail_ts"] = max(float(gs.get("last_log_fail_ts") or 0.0), now + 30.0)
            since_ts = float(gs.get("last_log_fail_ts") or 0.0)
            codex_failed, codex_ts, codex_reason = detect_recent_codex_retry_failure(
                self.codex_log_dir, now=now, since_ts=since_ts
            )
            if codex_failed:
                gs["last_log_fail_ts"] = codex_ts
                verdict = PROBE_DEAD
                detail = f"codex_log_retry ({codex_reason})"
                log_triggered = True

        if (
            verdict == PROBE_DEAD
            and not log_triggered
            and int(gs.get("busy_defer_count") or 0) < 4
            and has_active_ai_transfer(self.paths, getattr(self.query, "api", None), group, current)
        ):
            gs["busy_defer_count"] = int(gs.get("busy_defer_count") or 0) + 1
            verdict = PROBE_OK
            detail = "active_transfer_busy"
        elif verdict == PROBE_OK:
            gs["busy_defer_count"] = 0

        result = WatchGroupResult(group=group, current=current, verdict=verdict, detail=detail)

        # 若 mihomo fallback 被动切换了出口叶子，清理仍滞留在旧叶子上的陈旧长连接，
        # 避免同一会话混用新旧出口 IP；若旧叶子已被健康检查标死/超时，同步写入冷却期并把
        # fallback 的 `fixed` 指针更新到新叶子，防止旧叶子短暂回包后 mihomo 瞬间跳回造成乒乓抖动。
        passive_dead_switch = False
        if current:
            if not dry_run and previous_active and previous_active != current:
                prev_entry = groups.get(previous_active)
                if (
                    prev_entry is not None
                    and not prev_entry.candidates
                    and (prev_entry.alive is False or delay_state(prev_entry.delay) == "timeout")
                    and not has_active_ai_transfer(
                        self.paths, getattr(self.query, "api", None), group, previous_active
                    )
                ):
                    gs.setdefault("cooldown", {})[previous_active] = now
                    passive_dead_switch = True
                if group != AI_GEMINI_GROUP or is_static_iepl_node(previous_active, self.paths):
                    drop_fn = getattr(self.query, "_drop_stale_ai_connections", None)
                    if callable(drop_fn):
                        try:
                            drop_fn(group, keep_leaf=current)
                        except TypeError:
                            drop_fn(group)
            gs["last_active"] = current

        # 自动模式（AI-MANUAL 当前是 AI-AUTO）由自动池自身 fallback 兜底，看门狗不手动切，
        # 否则会把用户手动选的自动模式静默踢成固定叶子且无返回路径
        if group == AI_MANUAL_GROUP and group_obj is not None and group_obj.current == AI_AUTO_GROUP:
            return result

        if verdict == PROBE_OK:
            cooldown_map = gs.get("cooldown", {})
            current_already_cooled = (
                current is not None
                and current in cooldown_map
                and now - float(cooldown_map[current]) < COOLDOWN_SECONDS
            )
            if (
                not dry_run
                and group_obj is not None
                and group in (AI_GEMINI_GROUP, AI_MANUAL_GROUP)
                and is_static_iepl_node(current, self.paths)
                and not current_already_cooled
                and not has_active_ai_transfer(
                    self.paths, getattr(self.query, "api", None), group, current
                )
            ):
                cand_1x = self._pick_next(groups, group_obj, current, cooldown_map, now)
                cand_entry = groups.get(cand_1x) if cand_1x else None
                cand_ready = (
                    cand_1x is not None
                    and not is_static_iepl_node(cand_1x, self.paths)
                    and cand_entry is not None
                    and cand_entry.alive is True
                    and delay_state(cand_entry.delay) != "timeout"
                )
                if not cand_ready:
                    try:
                        from ..backend.runtime import TEST_URL, refresh_ws_cdn_ips_if_changed

                        refresh_ws_cdn_ips_if_changed(self.paths)
                    except Exception:
                        TEST_URL = "https://www.gstatic.com/generate_204"
                    delay_fn = getattr(getattr(self.query, "api", None), "delay_test", None)
                    if callable(delay_fn):
                        from .ban import is_node_banned, load_active_bans

                        active_bans = load_active_bans(self.paths, now=now)
                        non_iepl_cands = [
                            str(c)
                            for c in group_obj.candidates
                            if c
                            and c != current
                            and not is_static_iepl_node(str(c), self.paths)
                            and not (groups.get(c) is not None and groups[c].candidates)
                            and not (active_bans and is_node_banned(str(c), active_bans))
                        ]
                        non_iepl_cands.sort(key=lambda c: (1 if c in cooldown_map else 0, float(cooldown_map.get(c, 0.0))))
                        for probe_cand in non_iepl_cands[:2]:
                            try:
                                payload = delay_fn(probe_cand, TEST_URL, 5000, request_timeout=6)
                                if int((payload or {}).get("delay") or 0) > 0:
                                    cand_1x = probe_cand
                                    cand_ready = True
                                    break
                            except Exception:
                                continue
                if cand_ready and cand_1x is not None:
                    try:
                        try:
                            self.query.switch_group(group, cand_1x, drop_connections=True)
                        except TypeError:
                            self.query.switch_group(group, cand_1x)
                        if isinstance(cooldown_map, dict):
                            cooldown_map.pop(cand_1x, None)
                        _promote_gemini_order_head(self.paths, cand_1x, group=group)
                        gs["fail_node"] = None
                        gs["fail_count"] = 0
                        gs["last_active"] = cand_1x
                        result.switched = True
                        result.target = cand_1x
                        result.detail = "migrated_off_iepl"
                        return result
                    except Exception:
                        pass
            if (
                passive_dead_switch
                and not dry_run
                and group_obj is not None
                and current in group_obj.candidates
                and not is_static_iepl_node(current, self.paths)
            ):
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
            try:
                from ..backend.runtime import refresh_ws_cdn_ips_if_changed

                refresh_ws_cdn_ips_if_changed(self.paths)
            except Exception:
                pass
            target = self._pick_next(groups, group_obj, current, gs.get("cooldown", {}), now)
            if target is not None:
                # 仅在 AI-MANUAL、地区封锁、客户端真实重试报错或易断流节点切换时关闭旧连接；
                # 当 AI-GEMINI 已在健康节点上被动切换时，不强杀活跃 TCP 连接，防止掐断并发 agy 会话的流式输出。
                should_drop = (
                    group != AI_GEMINI_GROUP
                    or verdict == PROBE_GEO_BLOCKED
                    or log_triggered
                    or is_static_iepl_node(current, self.paths)
                )
                try:
                    try:
                        self.query.switch_group(group, target, drop_connections=should_drop)
                    except TypeError:
                        self.query.switch_group(group, target)
                except Exception:
                    # 切换失败（目标在 fallback 中 alive=False 或 API 抖动）：将目标记入冷却期避免下轮死循环重试同一节点
                    gs.setdefault("cooldown", {})[target] = now
                    return result
                cooldown = gs.setdefault("cooldown", {})
                if current:
                    cooldown[current] = now
                    if log_triggered:
                        sf = state.get("stream_fragile")
                        if not isinstance(sf, dict):
                            sf = {}
                        sf = {
                            str(k): float(v)
                            for k, v in sf.items()
                            if k and isinstance(v, (int, float)) and (now - float(v)) < STREAM_FRAGILE_TTL_SECONDS
                        }
                        sf[current] = now
                        state["stream_fragile"] = sf
                if group in (AI_GEMINI_GROUP, AI_MANUAL_GROUP):
                    # 设 30s 切流静默保护窗
                    gs["last_log_fail_ts"] = max(
                        float(gs.get("last_log_fail_ts") or 0.0),
                        now + 30.0,
                        time.time() + 30.0,
                    )
                    _promote_gemini_order_head(self.paths, target, group=group)
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
        对于 `AI-GEMINI` 与 `AI-MANUAL`，按三层优先级挑选：
        1. 零动态断流且非静态 IEPL 的干净节点（亚太/英国低延迟区优先）；
        2. 曾有动态断流记录但非静态 IEPL 的节点（亚太/英国优先）；
        3. 若所有非静态 IEPL 存活叶子均在冷却期内，优先回收最早进入冷却的存活非静态 IEPL 叶子，
           绝不轻易回退到 50 秒必断流的静态 IEPL/裸 vision 节点。
        """
        from .ban import is_node_banned, load_active_bans
        from .nodelist import match_region
        from .probe import GEMINI_FAST_REGIONS

        active_bans = load_active_bans(self.paths, now=now)
        fallback_leaf: str | None = None
        cooled_healthy_non_iepl: list[tuple[int, float, int, str]] = []
        candidates = list(group.candidates)
        is_managed_ai = getattr(group, "name", "") in (AI_GEMINI_GROUP, AI_MANUAL_GROUP) and any(
            c and is_iepl_node(str(c), self.paths) for c in candidates
        )
        if is_managed_ai:
            clean_non_iepl = [c for c in candidates if c and not is_iepl_node(str(c), self.paths)]
            clean_non_iepl.sort(
                key=lambda c: 0 if (match_region(str(c)) in GEMINI_FAST_REGIONS or match_region(str(c)) in ("OTHER", None)) else 1
            )
            dynamic_non_static_iepl = [
                c
                for c in candidates
                if c and is_iepl_node(str(c), self.paths) and not is_static_iepl_node(str(c), self.paths)
            ]
            dynamic_non_static_iepl.sort(
                key=lambda c: 0 if (match_region(str(c)) in GEMINI_FAST_REGIONS or match_region(str(c)) in ("OTHER", None)) else 1
            )
            static_iepl = [c for c in candidates if c and is_static_iepl_node(str(c), self.paths)]
            candidates = clean_non_iepl + dynamic_non_static_iepl + static_iepl
        for idx, cand in enumerate(candidates):
            if not cand or cand == current:
                continue
            if active_bans and is_node_banned(str(cand), active_bans):
                continue
            entry = groups.get(cand)
            if entry is not None and entry.candidates:
                continue
            is_dead = entry is not None and (entry.alive is False or delay_state(entry.delay) == "timeout")
            ts = cooldown.get(cand)
            if ts is not None and now - float(ts) < COOLDOWN_SECONDS:
                if is_managed_ai and not is_static_iepl_node(str(cand), self.paths) and not is_dead:
                    dyn_tier = 0 if not is_iepl_node(str(cand), self.paths) else 1
                    cooled_healthy_non_iepl.append((dyn_tier, float(ts), idx, str(cand)))
                continue
            if is_dead:
                if fallback_leaf is None:
                    fallback_leaf = str(cand)
                continue
            if is_managed_ai and is_static_iepl_node(str(cand), self.paths) and cooled_healthy_non_iepl:
                cooled_healthy_non_iepl.sort()
                return cooled_healthy_non_iepl[0][3]
            return str(cand)
        if cooled_healthy_non_iepl:
            cooled_healthy_non_iepl.sort()
            return cooled_healthy_non_iepl[0][3]
        return fallback_leaf
