from __future__ import annotations

import copy
import hashlib
import os
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

try:
    from yaml import CSafeLoader as _YamlLoader
except ImportError:  # pragma: no cover
    from yaml import SafeLoader as _YamlLoader  # type: ignore[assignment]

# 路径 → ((mtime_ns, size, sha256), 解析结果)。config.yaml / runtime.yaml 有 50–60KB 代理列表，
# 纯 Python SafeLoader 每次约 120ms；一次 CLI 里 request() 会读配置三遍。
# 读文件本身只要 0ms 量级，用元数据 + SHA-256 判断是否需要再 parse，并返回深拷贝避免调用方污染缓存。
_YAML_CACHE_LOCK = threading.Lock()
_YAML_CACHE: dict[str, tuple[tuple[int, int, str], Any]] = {}


def atomic_write_bytes(path: Path, data: bytes, mode: int = 0o600) -> None:
    """原子写入二进制文件，POSIX 下强制权限（默认 0o600），失败自动清理临时文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    tmp_path = Path(tmp_name)
    fd_closed = False
    replaced = False
    try:
        with os.fdopen(fd, "wb") as fh:
            fd_closed = True
            fh.write(data)
            fh.flush()
        if os.name != "nt":
            os.chmod(tmp_path, mode)
        for attempt in range(5):
            try:
                os.replace(tmp_path, path)
                replaced = True
                break
            except PermissionError:
                if os.name != "nt" or attempt == 4:
                    raise
                time.sleep(0.02 * (attempt + 1))
    finally:
        if not fd_closed:
            try:
                os.close(fd)
            except OSError:
                pass
        if not replaced:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass


def atomic_write_text(
    path: Path,
    content: str,
    mode: int = 0o600,
    encoding: str = "utf-8",
) -> None:
    """原子写入文本文件，POSIX 下强制权限（默认 0o600）。"""
    atomic_write_bytes(path, content.encode(encoding), mode=mode)


@dataclass(frozen=True)
class AppPaths:
    config_dir: Path
    data_dir: Path
    state_dir: Path


def default_paths(home: Path | None = None) -> AppPaths:
    base_home = home or Path.home()
    if home is None:
        if os.name == "nt" and not (base_home / ".config" / "cproxy").exists():
            default_cfg_base = Path(os.environ.get("APPDATA", base_home / ".config"))
            default_local_base = Path(os.environ.get("LOCALAPPDATA", base_home / ".local"))
            config_home = Path(os.environ.get("XDG_CONFIG_HOME", default_cfg_base))
            data_home = Path(os.environ.get("XDG_DATA_HOME", default_local_base / "share"))
            state_home = Path(os.environ.get("XDG_STATE_HOME", default_local_base / "state"))
        else:
            config_home = Path(os.environ.get("XDG_CONFIG_HOME", base_home / ".config"))
            data_home = Path(os.environ.get("XDG_DATA_HOME", base_home / ".local" / "share"))
            state_home = Path(os.environ.get("XDG_STATE_HOME", base_home / ".local" / "state"))
        config_dir = Path(os.environ.get("CPROXY_CONFIG_DIR", config_home / "cproxy"))
        data_dir = Path(os.environ.get("CPROXY_DATA_DIR", data_home / "cproxy"))
        state_dir = Path(os.environ.get("CPROXY_STATE_DIR", state_home / "cproxy"))
        return AppPaths(config_dir=config_dir, data_dir=data_dir, state_dir=state_dir)

    config_home = base_home / ".config"
    data_home = base_home / ".local" / "share"
    state_home = base_home / ".local" / "state"
    return AppPaths(
        config_dir=config_home / "cproxy",
        data_dir=data_home / "cproxy",
        state_dir=state_home / "cproxy",
    )


def config_file(paths: AppPaths) -> Path:
    return paths.config_dir / "config.yaml"


def runtime_file(paths: AppPaths) -> Path:
    return paths.data_dir / "runtime.yaml"


def pid_file(paths: AppPaths) -> Path:
    return paths.state_dir / "cproxy.pid"


def process_meta_file(paths: AppPaths) -> Path:
    return paths.state_dir / "cproxy-process.json"


def log_file(paths: AppPaths) -> Path:
    return paths.state_dir / "cproxy.log"


def traffic_db_file(paths: AppPaths) -> Path:
    return paths.state_dir / "traffic.db"


def subscription_info_file(paths: AppPaths) -> Path:
    return paths.state_dir / "subscription-info.json"


def ai_manual_order_file(paths: AppPaths) -> Path:
    """cproxy ai-rebalance 持久化的 AI-MANUAL 稳定度排序（state 文件，非用户配置）。"""
    return paths.state_dir / "ai-manual-order.json"


def ai_gemini_order_file(paths: AppPaths) -> Path:
    """cproxy ai-rebalance 持久化的 AI-GEMINI 稳定度排序（state 文件，非用户配置）。"""
    return paths.state_dir / "ai-gemini-order.json"


def ai_claude_order_file(paths: AppPaths) -> Path:
    """cproxy ai-rebalance 持久化的 AI-CLAUDE 稳定度排序（state 文件，非用户配置）。"""
    return paths.state_dir / "ai-claude-order.json"


def ai_watch_state_file(paths: AppPaths) -> Path:
    """cproxy ai-watch 的看护状态（连续失败计数与冷却期，state 文件）。"""
    return paths.state_dir / "ai-watch.json"


def node_bans_file(paths: AppPaths) -> Path:
    """cproxy ban 持久化的节点屏蔽列表（state 文件）。"""
    return paths.state_dir / "node-bans.json"


def custom_rules_file(paths: AppPaths) -> Path:
    """cproxy rule 持久化的用户自定义分流规则（state 文件）。"""
    return paths.state_dir / "custom-rules.json"


def conn_activity_state_file(paths: AppPaths) -> Path:
    """跨平台（Windows/无 ss 环境）通过 /connections 字节差分跟踪连接活跃时序的状态文件。"""
    return paths.state_dir / "conn-activity.json"


def verge_daemon_state_file(paths: AppPaths) -> Path:
    """cproxy verge daemon 后台心跳与调度遥测状态文件。"""
    return paths.state_dir / "verge-daemon.json"


def load_yaml_file(path: Path) -> Any:
    """读 YAML。文件不存在返回 None。按路径 + (mtime_ns, size, sha256) 缓存，写入后自动失效。"""
    try:
        st = path.stat()
        raw = path.read_bytes()
    except OSError:
        return None
    digest = hashlib.sha256(raw).hexdigest()
    fingerprint = (st.st_mtime_ns, len(raw), digest)
    cache_key = str(path)
    with _YAML_CACHE_LOCK:
        hit = _YAML_CACHE.get(cache_key)
        if hit is not None and hit[0] == fingerprint:
            return copy.deepcopy(hit[1])
    data = yaml.load(raw.decode("utf-8"), Loader=_YamlLoader)
    with _YAML_CACHE_LOCK:
        _YAML_CACHE[cache_key] = (fingerprint, copy.deepcopy(data))
    return data


def read_config(paths: AppPaths) -> dict:
    data = load_yaml_file(config_file(paths))
    return data if isinstance(data, dict) else {}
