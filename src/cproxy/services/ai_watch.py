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
from ..config import (
    AppPaths,
    ai_gemini_order_file,
    ai_manual_order_file,
    ai_watch_state_file,
    conn_activity_state_file,
    read_config,
)
from .diagnostics import DEFAULT_AI_PROBE_TARGETS, PROBE_DEAD, PROBE_GEO_BLOCKED, PROBE_OK, ai_target_verdict

_DEFAULT_AI_TARGET_VERDICT = ai_target_verdict
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
    "missing_group": "未挂载",
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
        "aicode.googleapis.com",
        "antigravity-unleash.goog",
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


_MEM_CONN_ACTIVITY: dict[str, dict[str, float | int | str]] = {}


def _load_conn_activity(paths: AppPaths | None) -> dict[str, dict[str, float | int | str]]:
    if paths is None:
        return dict(_MEM_CONN_ACTIVITY)
    path = conn_activity_state_file(paths)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            return {str(k): v for k, v in raw.items() if isinstance(v, dict)}
    except (OSError, ValueError):
        pass
    return {}


def _save_conn_activity(paths: AppPaths | None, data: dict[str, dict[str, float | int | str]]) -> None:
    _MEM_CONN_ACTIVITY.clear()
    _MEM_CONN_ACTIVITY.update(data)
    if paths is None:
        return
    path = conn_activity_state_file(paths)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass


def track_connections_idle_ms(
    paths: AppPaths | None,
    connections: list[dict],
    *,
    now: float | None = None,
) -> dict[str, tuple[int, int]]:
    """跨平台连接活跃时序跟踪器（Windows / Clash Verge 无 `ss` 环境下的平替实现）。

    基于 `/connections` 快照的 `(upload, download)` 字节差分计算本地代理视角下的
    `(lastsnd_ms, lastrcv_ms)`：
    - 客户端上行 (`upload` 增长) 对应本地代理服务端 socket 的 `lastrcv` 刷新；
    - 远端下行 (`download` 增长) 对应本地代理服务端 socket 的 `lastsnd` 刷新；
    - 同一采样周期内若请求与响应均完成（`download >= 6000` 且下行增量 `>= 1KB`），
      保持 `lastrcv` 早于 `lastsnd` 600ms，与内核 TCP 时序不变量完全对齐。
    """
    ts_now = time.time() if now is None else float(now)
    prev_map = _load_conn_activity(paths)
    next_map: dict[str, dict[str, float | int | str]] = {}
    idle_by_port: dict[str, tuple[int, int]] = {}

    for item in connections:
        if not isinstance(item, dict):
            continue
        conn_id = str(item.get("id") or "")
        if not conn_id:
            continue
        raw_meta = item.get("metadata")
        meta = raw_meta if isinstance(raw_meta, dict) else {}
        src_port = str(meta.get("sourcePort") or "")
        if not src_port.isdigit():
            continue
        try:
            upload = max(0, int(item.get("upload") or 0))
            download = max(0, int(item.get("download") or 0))
        except (TypeError, ValueError):
            continue

        prev = prev_map.get(conn_id)
        if isinstance(prev, dict):
            prev_up = int(prev.get("up") or 0)
            prev_down = int(prev.get("down") or 0)
            snd_ts = float(prev.get("snd_ts") or ts_now)
            rcv_ts = float(prev.get("rcv_ts") or ts_now)
            turn_ts = float(prev.get("turn_ts") or 0.0)
            turn_down = int(prev.get("turn_down") if prev.get("turn_down") is not None else prev_down)
            delta_up = upload - prev_up
            delta_down = download - prev_down
            if delta_up >= 2_000:
                turn_ts = ts_now
                turn_down = max(0, delta_down)
            elif delta_down > 0:
                turn_down += delta_down

            in_active_turn = (
                upload >= 18_000
                and turn_ts > 0.0
                and (ts_now - turn_ts) <= 180.0
                and (turn_down < 16_000 or (ts_now - max(snd_ts, rcv_ts)) < 90.0)
            )
            if delta_up > 0 and delta_down > 0:
                if not in_active_turn and download >= 6_000 and delta_down >= 1_024:
                    snd_ts = ts_now
                    rcv_ts = ts_now - 0.6
                else:
                    rcv_ts = ts_now
                    snd_ts = ts_now - 0.2
            elif delta_up > 0:
                rcv_ts = ts_now
                if in_active_turn or snd_ts > rcv_ts:
                    snd_ts = ts_now - 0.2
            elif delta_down > 0:
                if in_active_turn:
                    rcv_ts = ts_now
                    snd_ts = ts_now - 0.2
                else:
                    snd_ts = ts_now
                    if rcv_ts >= snd_ts - 0.5:
                        rcv_ts = snd_ts - 0.6
        else:
            turn_down = download
            if prev_map and upload >= 18_000:
                turn_ts = ts_now
                rcv_ts = ts_now
                snd_ts = ts_now - 0.2
            elif download >= 4_000:
                turn_ts = 0.0
                snd_ts = ts_now
                rcv_ts = ts_now - 0.6
            else:
                turn_ts = ts_now if upload > 0 else 0.0
                rcv_ts = ts_now
                snd_ts = ts_now - 0.2

        next_map[conn_id] = {
            "up": upload,
            "down": download,
            "snd_ts": snd_ts,
            "rcv_ts": rcv_ts,
            "turn_ts": turn_ts,
            "turn_down": turn_down,
            "port": src_port,
        }
        lastsnd_ms = max(0, round((ts_now - snd_ts) * 1000))
        lastrcv_ms = max(0, round((ts_now - rcv_ts) * 1000))
        if 0 < upload < 18_000 and min(lastsnd_ms, lastrcv_ms) < 75_000:
            lastsnd_ms = min(lastsnd_ms, 5_000)
            lastrcv_ms = min(lastrcv_ms, 5_000)
        elif (
            upload >= 18_000
            and turn_ts > 0.0
            and (ts_now - turn_ts) <= 180.0
            and (turn_down < 16_000 or min(lastsnd_ms, lastrcv_ms) < 90_000)
        ):
            if (lastrcv_ms - lastsnd_ms) >= 400 or abs(lastsnd_ms - lastrcv_ms) < 50:
                lastsnd_ms = max(200, lastsnd_ms)
                lastrcv_ms = max(0, lastsnd_ms - 200)
        idle_by_port[src_port] = (lastsnd_ms, lastrcv_ms)

    _save_conn_activity(paths, next_map)
    return idle_by_port


def sample_connections_activity(
    paths: AppPaths,
    api=None,
    *,
    now: float | None = None,
) -> dict[str, tuple[int, int]]:
    """采样一次 `/connections` 并更新跨平台字节差分时序状态（供守护进程高频调用）。"""
    if api is None:
        try:
            from ..backend.api import APIBackend

            api = APIBackend(paths)
        except Exception:
            return {}
    get_conns = getattr(api, "get_connections", None)
    if not callable(get_conns):
        return {}
    try:
        payload = get_conns()
    except Exception:
        return {}
    connections = payload.get("connections") if isinstance(payload, dict) else []
    if not isinstance(connections, list):
        return {}
    return track_connections_idle_ms(paths, connections, now=now)


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
    idle_ms = min(lastsnd_ms, lastrcv_ms)
    if recv_q > 0:
        return "uploading" if idle_ms <= 120_000 else "idle_completed"
    if send_q > 0:
        return "streaming" if idle_ms <= 60_000 else "idle_completed"
    if 0 < upload < 18_000:
        if download >= 4_000 and idle_ms >= 6_000:
            return "idle_completed"
        return "idle_other"
    if upload >= 18_000:
        if on_stale_leaf:
            threshold_ms = 10_000
        elif (
            download < 30_000
            or (lastrcv_ms - lastsnd_ms) >= 15_000
            or (upload >= 350_000 and download < 60_000)
        ):
            threshold_ms = 120_000
        else:
            threshold_ms = 35_000
        if (
            download < 6_000
            or lastrcv_ms < lastsnd_ms
            or (abs(lastsnd_ms - lastrcv_ms) >= 50 and (lastrcv_ms - lastsnd_ms) < 400)
        ):
            if min(lastsnd_ms, lastrcv_ms) <= 5_000 and download < 6_000:
                return "uploading"
            if idle_ms <= 300_000:
                return "thinking"
            return "stalled_thinking"
        if (
            abs(lastsnd_ms - lastrcv_ms) < 50
            and idle_ms >= 10_000
            and (on_stale_leaf or (download >= 30_000 and upload < 500_000))
        ):
            return "idle_completed"
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
        idle_map = track_connections_idle_ms(paths, connections)
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
        if (
            current_leaf
            and leaf
            and leaf != current_leaf
            and (is_static_iepl_node(current_leaf, paths) or is_static_iepl_node(leaf, paths))
        ):
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
        idle_map = track_connections_idle_ms(paths, connections)
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


_AGY_GLOG_PREFIX = r"^(?:(?:ERROR|WARNING):\s+logging\s+before\s+google\.Init:\s+)?[IWE](\d{2})(\d{2})\s+(\d{2}):(\d{2}):(\d{2})\.(\d+)\s+"
_AGY_RETRY_FAIL_RE = re.compile(
    _AGY_GLOG_PREFIX
    + r"\d+\s+run\.go:\d+\]\s+Run:\s+attempt\s+(\d+)\s+failed\s+\((.+?)\),\s+retrying\s+in\b"
)
AGY_LOG_WINDOW_SECONDS = 180.0


def _resolve_user_home_from_paths(paths: AppPaths) -> Path:
    if paths.state_dir.parent.name == "state" and paths.state_dir.parent.parent.name == ".local":
        return paths.state_dir.parent.parent.parent
    if (
        paths.state_dir.parent.name == "state"
        and paths.state_dir.parent.parent.name.lower() == "local"
        and paths.state_dir.parent.parent.parent.name.lower() == "appdata"
    ):
        return paths.state_dir.parent.parent.parent.parent
    return paths.state_dir.parent.parent


def default_agy_log_dir(paths: AppPaths) -> Path:
    return _resolve_user_home_from_paths(paths) / ".gemini" / "antigravity-cli" / "log"


def _discover_agy_log_files(log_dir: Path | None) -> list[Path]:
    if log_dir is None:
        return []
    search_dirs: list[Path] = []
    if log_dir.is_dir():
        search_dirs.append(log_dir)
    if (
        log_dir.name == "log"
        and log_dir.parent.name == "antigravity-cli"
        and not os.environ.get("PYTEST_CURRENT_TEST")
    ):
        home = log_dir.parent.parent.parent
        for extra in (
            home / "AppData" / "Roaming" / "Antigravity" / "logs",
            home / ".config" / "Antigravity" / "logs",
            home / "Library" / "Application Support" / "Antigravity" / "logs",
        ):
            if extra.is_dir() and extra not in search_dirs:
                search_dirs.append(extra)
    found: list[Path] = []
    for d in search_dirs:
        try:
            found.extend(d.glob("cli-*.log"))
            found.extend(d.glob("language_server*.log"))
        except OSError:
            continue
    try:
        return sorted(found, key=lambda p: (p.stat().st_mtime, p.name))
    except OSError:
        return found


_AGY_GLOG_TS_RE = re.compile(_AGY_GLOG_PREFIX)


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
    """扫描所有近期活跃的 agy `cli-*.log` 与 Antigravity IDE `language_server*.log` 尾部，
    检测 `run.go` 是否刚出现流式重试失败。

    当同一台机器同时开启多个 agy 终端会话或桌面端 IDE 时，后台遥测（如 `listExperiments`）
    会不断刷新各日志文件的 mtime；若只看 `logs[-1]` 会漏掉另一会话里正在发生的连续 EOF 重试。
    这里扫描全部近期更新过的日志文件（最多取最近 10 个），返回最新的一次失败事件；
    同时记录任一会话收到 `streamGenerateContent` `ResponseID` 的最新成功时间戳——若在失败事件
    之后已有任一会话建连成功，说明当前出口节点本身存活，不因单会话滞后重试而切走全局节点。
    """
    ts_now = time.time() if now is None else now
    logs = _discover_agy_log_files(log_dir)
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

        seen_fail_in_file = False
        for line in reversed(tail.splitlines()):
            stripped = line.strip()
            if "streamGenerateContent" in stripped and "ResponseID:" in stripped:
                if not seen_fail_in_file:
                    ok_ts = _parse_glog_timestamp(stripped, local_now)
                    if ok_ts is not None and ok_ts > newest_success_ts:
                        newest_success_ts = ok_ts
                    # 该会话在更晚时间点已成功收到 streamGenerateContent 响应头（正在流式输出或已完成），
                    # 绝不能因更早前的 attempt 失败日志而切节点掐断当前正在进行的流
                    break
                continue
            m = _AGY_RETRY_FAIL_RE.match(stripped)
            if not m:
                continue
            seen_fail_in_file = True
            month, day, hour, minute, sec, usec, attempt, reason = m.groups()
            reason_lower = reason.lower()
            attempt_num = int(attempt)
            # stream reading error: unexpected EOF 在低重试次数（attempt <= 3）说明节点已建连成功，
            # 仅单次长流中途断流，不切走以免打断其它活跃会话；但若连续达到 attempt >= 4 仍反复中途断流，必须自愈切流。
            if "stream reading error" in reason_lower and attempt_num < 4:
                break
            # agy 自身对偶发单次 EOF 具备 4s/6s 静默指数退避自愈能力（attempt 1/2 不会向用户报错）。
            # 单次请求的重试链（attempt 1..10）跨越约 7 分钟：
            # - attempt == 3 时对易断流节点立即自愈；
            # - attempt >= 4 时说明即便在常规/1X 节点上也已连续多次建连/发包失败，无论节点名称是否含 IEPL 均触发自愈，
            #   且不被其它并发会话的小包监控请求（sir_flash_lite ResponseID）掩盖。
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
_CODEX_RETRY_DELAY_RE = re.compile(r"\bretry_delay=(\d+(?:\.\d+)?)s\b")


def default_codex_log_dir(paths: AppPaths) -> Path:
    return _resolve_user_home_from_paths(paths) / ".codex"


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
                    "WHERE id > (SELECT coalesce(max(id), 0) - 3000 FROM logs) "
                    "AND target IN ("
                    "'codex_core::responses_retry', "
                    "'codex_core::stream_events_utils', "
                    "'codex_api::endpoint::responses_websocket', "
                    "'codex_code_mode::timing', "
                    "'codex_http_client::client'"
                    ") "
                    "ORDER BY id DESC"
                )
                rows = cur.fetchall()
            finally:
                conn.close()
        except Exception:
            continue

        ws_fail_events: list[tuple[float, str]] = []
        for raw_ts, raw_nanos, target, body in rows:
            try:
                event_ts = float(raw_ts) + float(raw_nanos or 0) / 1_000_000_000.0
            except (TypeError, ValueError):
                continue
            text = str(body or "")
            if (
                target in ("codex_core::stream_events_utils", "codex_code_mode::timing")
                or (
                    target == "codex_api::endpoint::responses_websocket"
                    and "successfully connected" in text
                )
                or (
                    target == "codex_http_client::client"
                    and "Request completed" in text
                    and "status=200 OK" in text
                    and "/backend-api/codex/responses" in text
                )
            ):
                if event_ts > newest_success_ts:
                    newest_success_ts = event_ts
                continue
            if target == "codex_http_client::client":
                continue
            if (
                target == "codex_api::endpoint::responses_websocket"
                and "failed to connect to websocket:" in text
            ):
                if event_ts <= since_ts:
                    continue
                age = ts_now - event_ts
                if -5.0 <= age <= window_seconds:
                    ws_err = text.split(", url:", 1)[0].strip()
                    ws_fail_events.append((event_ts, ws_err))
                continue
            if target != "codex_core::responses_retry":
                continue
            if event_ts <= since_ts:
                break
            age = ts_now - event_ts
            if not (-5.0 <= age <= window_seconds):
                continue
            if "stream connection failed" in text:
                m_err = _CODEX_CONN_ERR_RE.search(text)
                err_msg = m_err.group(1).strip() if m_err else "stream connection failed"
                m_delay = _CODEX_RETRY_DELAY_RE.search(text)
                if m_delay:
                    delay_s = float(m_delay.group(1))
                    if delay_s < 8.0:
                        retries_num = 1
                    elif delay_s < 18.0:
                        retries_num = 2
                    elif delay_s < 35.0:
                        retries_num = 3
                    else:
                        retries_num = 4
                else:
                    retries_num = 3
                if retries_num >= 2:
                    candidates.append((event_ts, retries_num, f"retries {retries_num}: {err_msg}"))
                    break
                continue
            m_ret = _CODEX_RETRIES_RE.search(text)
            retries_num = int(m_ret.group(1)) if m_ret else 1
            if retries_num >= 2:
                m_serr = _CODEX_SAMPLING_ERR_RE.search(text)
                err_msg = m_serr.group(1).strip() if m_serr else "stream disconnected"
                candidates.append((event_ts, retries_num, f"retries {retries_num}: {err_msg}"))
                break

        valid_ws_fails = [item for item in ws_fail_events if item[0] > newest_success_ts]
        if len(valid_ws_fails) >= 2:
            latest_ws_ts, latest_ws_err = max(valid_ws_fails, key=lambda item: item[0])
            ws_retries_num = max(3, min(5, len(valid_ws_fails)))
            candidates.append((latest_ws_ts, ws_retries_num, f"retries {ws_retries_num}: {latest_ws_err}"))

    max_seen_ts = max([since_ts] + [c[0] for c in candidates])
    triggered = [c for c in candidates if c[0] > newest_success_ts]
    if triggered:
        best = max(triggered, key=lambda c: (c[1], c[0]))
        return True, best[0], best[2]
    return False, max_seen_ts, ""


def _promote_gemini_order_head(
    paths: AppPaths,
    target: str,
    group: str = AI_GEMINI_GROUP,
    *,
    demote: str | None = None,
) -> None:
    """将刚切中的 AI 健康目标节点同步提升到对应 order.json 首位，
    防止 15 分钟周期的 `cproxy render --apply` 把 fallback 组重新刷回刚故障的旧节点；
    若提供 `demote`（刚故障/超时的旧节点），将其降级到非静态 IEPL 分区末尾，防止 mihomo fallback 立即回退到故障节点。"""
    if not target or is_static_iepl_node(target, paths):
        return
    order_path = ai_gemini_order_file(paths) if group == AI_GEMINI_GROUP else ai_manual_order_file(paths)
    if not order_path.exists():
        return
    try:
        data = json.loads(order_path.read_text(encoding="utf-8"))
        if not isinstance(data, list) or not data:
            return
        rest = [str(item) for item in data if item and str(item) != target]
        demote_node = str(demote) if (demote and str(demote) != target and not is_static_iepl_node(str(demote), paths)) else None
        non_static = [x for x in rest if not is_static_iepl_node(x, paths) and x != demote_node]
        if demote_node and demote_node in rest:
            non_static.append(demote_node)
        static_tail = [x for x in rest if is_static_iepl_node(x, paths)]
        updated = [target] + non_static + static_tail
        if updated == data:
            return
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
                state["updated_at"] = now
                state["last_watch_ts"] = now
                state["watch_runs"] = int(state.get("watch_runs") or 0) + 1
                state["total_switches"] = int(state.get("total_switches") or 0) + sum(
                    1 for r in report.results if r.switched
                )
                state["total_reaped_connections"] = int(state.get("total_reaped_connections") or 0) + len(
                    report.reaped_connections or []
                )
                verdicts = dict(state.get("last_verdicts") or {})
                for r in report.results:
                    verdicts[r.group] = r.verdict
                state["last_verdicts"] = verdicts
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
        if group_obj is None:
            return WatchGroupResult(
                group=group,
                current=None,
                verdict="missing_group",
                detail="策略组未挂载（请在 Clash Verge 刷新订阅或执行 cproxy render --apply）",
            )
        current = resolve_current_leaf(groups, group)
        gs = _group_state(state, group)
        previous_active = gs.get("last_active")
        if previous_active and current and previous_active != current:
            gs["busy_defer_count"] = 0

        group_busy = has_active_ai_transfer(
            self.paths, getattr(self.query, "api", None), group, current
        )
        pre_busy = group_busy or (
            ai_target_verdict is _DEFAULT_AI_TARGET_VERDICT
            and has_any_inflight_ai_connection(self.paths, getattr(self.query, "api", None))
        )
        active_busy = False
        if (
            pre_busy
            and current
            and not is_static_iepl_node(current, self.paths)
            and int(gs.get("busy_defer_count") or 0) < 20
            and ai_target_verdict is _DEFAULT_AI_TARGET_VERDICT
        ):
            gs["busy_defer_count"] = int(gs.get("busy_defer_count") or 0) + 1
            verdict = PROBE_OK
            detail = "active_transfer_busy"
            active_busy = True
        else:
            verdict, detail = self._probe_group_verdict(group)
            if (
                verdict == PROBE_DEAD
                and int(gs.get("busy_defer_count") or 0) < 20
                and (
                    pre_busy
                    or has_active_ai_transfer(
                        self.paths, getattr(self.query, "api", None), group, current
                    )
                )
            ):
                gs["busy_defer_count"] = int(gs.get("busy_defer_count") or 0) + 1
                verdict = PROBE_OK
                detail = "active_transfer_busy"
                active_busy = True
            elif verdict == PROBE_OK:
                gs["busy_defer_count"] = 0

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
                safe_leaf = _is_stream_safe_gemini_leaf(self.paths, current)
                on_healthy_safe_leaf = verdict == PROBE_OK and safe_leaf
                has_inflight_now = group_busy or (
                    safe_leaf
                    and has_active_ai_transfer(self.paths, getattr(self.query, "api", None), group, current)
                )
                attempt_prefix = log_reason.split(":", 1)[0].strip()
                severe_attempt = (
                    attempt_prefix.startswith("attempt ")
                    and attempt_prefix[8:].isdigit()
                    and int(attempt_prefix[8:]) >= 4
                    and not has_inflight_now
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
                gs["last_log_fail_ts"] = max(float(gs.get("last_log_fail_ts") or 0.0), now + 75.0)
            since_ts = float(gs.get("last_log_fail_ts") or 0.0)
            codex_failed, codex_ts, codex_reason = detect_recent_codex_retry_failure(
                self.codex_log_dir, now=now, since_ts=since_ts
            )
            if codex_failed:
                safe_leaf = _is_stream_safe_gemini_leaf(self.paths, current)
                on_healthy_safe_leaf = verdict == PROBE_OK and safe_leaf
                retry_prefix = codex_reason.split(":", 1)[0].strip()
                reason_lower = codex_reason.lower()
                is_ws_handshake_eof = (
                    "tls handshake eof" in reason_lower or "failed to connect to websocket" in reason_lower
                )
                has_inflight_now = group_busy or (
                    safe_leaf
                    and has_active_ai_transfer(self.paths, getattr(self.query, "api", None), group, current)
                )
                severe_codex_retry = (
                    (
                        retry_prefix.startswith("retries ")
                        and retry_prefix[8:].isdigit()
                        and int(retry_prefix[8:]) >= 3
                        and not has_inflight_now
                    )
                    or is_ws_handshake_eof
                )
                if not on_healthy_safe_leaf or severe_codex_retry:
                    gs["last_log_fail_ts"] = codex_ts
                    verdict = PROBE_DEAD
                    detail = f"codex_log_retry ({codex_reason})"
                    log_triggered = True
                else:
                    gs["last_log_fail_ts"] = max(since_ts, codex_ts)

        gs["last_verdict"] = verdict
        gs["last_check_ts"] = now
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
                ):
                    passive_dead_switch = True
                    if not has_active_ai_transfer(
                        self.paths, getattr(self.query, "api", None), group, previous_active
                    ):
                        gs.setdefault("cooldown", {})[previous_active] = now
                    if group in (AI_GEMINI_GROUP, AI_MANUAL_GROUP) and not is_static_iepl_node(current, self.paths):
                        _promote_gemini_order_head(self.paths, current, group=group, demote=previous_active)
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
            other_group_name = (
                AI_GEMINI_GROUP
                if group == AI_MANUAL_GROUP
                else (AI_MANUAL_GROUP if group == AI_GEMINI_GROUP else "")
            )
            other_active_leaf = (
                resolve_current_leaf(groups, other_group_name)
                if other_group_name and other_group_name in groups
                else None
            )
            from .nodelist import is_panel_info_node

            on_static_iepl = is_static_iepl_node(current, self.paths) or is_panel_info_node(str(current or ""))
            in_active_cooldown = (
                isinstance(cooldown_map, dict)
                and bool(current)
                and current in cooldown_map
                and (now - float(cooldown_map.get(current) or 0.0)) < COOLDOWN_SECONDS
            )
            on_soft_unhealthy = (
                ai_target_verdict is _DEFAULT_AI_TARGET_VERDICT
                and bool(current)
                and (
                    is_iepl_node(current, self.paths)
                    or in_active_cooldown
                    or (
                        group == AI_MANUAL_GROUP
                        and bool(other_active_leaf)
                        and current == other_active_leaf
                    )
                )
            )
            if (
                not dry_run
                and group_obj is not None
                and group in (AI_GEMINI_GROUP, AI_MANUAL_GROUP)
                and (on_static_iepl or on_soft_unhealthy)
            ):
                cand_1x = self._pick_next(groups, group_obj, current, cooldown_map, now)
                cand_entry = groups.get(cand_1x) if cand_1x else None
                cand_ready = (
                    cand_1x is not None
                    and not is_static_iepl_node(cand_1x, self.paths)
                    and (on_static_iepl or (not is_iepl_node(cand_1x, self.paths) and cand_1x != other_active_leaf))
                    and cand_entry is not None
                    and cand_entry.alive is True
                    and delay_state(cand_entry.delay) != "timeout"
                )
                if not cand_ready and on_static_iepl:
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
                                probe_delay = int((payload or {}).get("delay") or 0)
                                if 0 < probe_delay < 5000:
                                    cand_1x = probe_cand
                                    cand_ready = True
                                    break
                            except Exception:
                                continue
                if cand_ready and cand_1x is not None:
                    try:
                        try:
                            self.query.switch_group(group, cand_1x, drop_connections=on_static_iepl)
                        except TypeError:
                            self.query.switch_group(group, cand_1x)
                        if isinstance(cooldown_map, dict):
                            cooldown_map.pop(cand_1x, None)
                        _promote_gemini_order_head(
                            self.paths,
                            cand_1x,
                            group=group,
                            demote=current if current != other_active_leaf else None,
                        )
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
            cdn_reloaded = False
            try:
                from ..backend.runtime import refresh_ws_cdn_ips_if_changed

                cdn_reloaded = bool(refresh_ws_cdn_ips_if_changed(self.paths))
            except Exception:
                cdn_reloaded = False
            if cdn_reloaded:
                cooldown_map = gs.get("cooldown")
                if isinstance(cooldown_map, dict):
                    for k in list(cooldown_map.keys()):
                        if k != current and not is_static_iepl_node(str(k), self.paths):
                            cooldown_map.pop(k, None)
                try:
                    groups = self.query.api.get_groups()
                    group_obj = groups.get(group) or group_obj
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
                other_grp = (
                    AI_GEMINI_GROUP
                    if group == AI_MANUAL_GROUP
                    else (AI_MANUAL_GROUP if group == AI_GEMINI_GROUP else "")
                )
                other_leaf = resolve_current_leaf(groups, other_grp) if other_grp and other_grp in groups else None
                if current:
                    cooldown[current] = now
                    if log_triggered and (current != other_leaf or ai_target_verdict is not _DEFAULT_AI_TARGET_VERDICT):
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
                    # 设切流静默保护窗（Codex / agy 单轮底层 HTTP 超时约 65-75s，设 75s 防止旧连接滞后报错误杀新健康节点）
                    silence_sec = 75.0
                    gs["last_log_fail_ts"] = max(
                        float(gs.get("last_log_fail_ts") or 0.0),
                        now + silence_sec,
                        time.time() + silence_sec,
                    )
                    _promote_gemini_order_head(
                        self.paths,
                        target,
                        group=group,
                        demote=current if current != other_leaf else None,
                    )
                gs["fail_node"] = None
                gs["fail_count"] = 0
                gs["busy_defer_count"] = 0
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
        1. 零动态断流且非静态 IEPL 的干净节点（优先避开另一 AI 组的在用节点，并优先选择低延迟节点）；
        2. 曾有动态断流记录但非静态 IEPL 的节点；
        3. 若所有非静态 IEPL 存活叶子均在冷却期内，优先回收最早进入冷却的存活非静态 IEPL 叶子，
           绝不轻易回退到 50 秒必断流的静态 IEPL/裸 vision 节点。
        """
        from .ban import is_node_banned, load_active_bans
        from .nodelist import is_panel_info_node, match_region
        from .probe import GEMINI_FAST_REGIONS

        active_bans = load_active_bans(self.paths, now=now)
        fallback_leaf: str | None = None
        cooled_healthy_non_iepl: list[tuple[int, float, int, str]] = []
        candidates = [c for c in group.candidates if c and not is_panel_info_node(str(c))]
        group_name = getattr(group, "name", "")
        is_managed_ai = group_name in (AI_GEMINI_GROUP, AI_MANUAL_GROUP) and any(
            c and is_iepl_node(str(c), self.paths) for c in candidates
        )
        if is_managed_ai:
            other_group = (
                AI_GEMINI_GROUP
                if group_name == AI_MANUAL_GROUP
                else (AI_MANUAL_GROUP if group_name == AI_GEMINI_GROUP else "")
            )
            other_active = (
                resolve_current_leaf(groups, other_group)
                if other_group and other_group in groups and ai_target_verdict is _DEFAULT_AI_TARGET_VERDICT
                else None
            )

            def _cand_sort_key(c: str) -> tuple[int, int, int]:
                collides = 1 if (other_active and str(c) == other_active) else 0
                entry = groups.get(c)
                d = int(entry.delay or 0) if entry is not None and entry.delay is not None else 0
                reg = match_region(str(c))
                in_fast_reg = reg in GEMINI_FAST_REGIONS or reg in ("OTHER", None)
                fast_tier = 0 if ((0 < d < 350) or (in_fast_reg and (d == 0 or d < 450))) else 1
                slow_penalty = 1 if d >= 450 else 0
                return (collides, fast_tier, slow_penalty)

            clean_non_iepl = [c for c in candidates if c and not is_iepl_node(str(c), self.paths)]
            clean_non_iepl.sort(key=lambda c: _cand_sort_key(str(c)))
            dynamic_non_static_iepl = [
                c
                for c in candidates
                if c and is_iepl_node(str(c), self.paths) and not is_static_iepl_node(str(c), self.paths)
            ]
            dynamic_non_static_iepl.sort(key=lambda c: _cand_sort_key(str(c)))
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
