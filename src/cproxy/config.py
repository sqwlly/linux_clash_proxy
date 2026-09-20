from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

try:
    from yaml import CSafeLoader as _YamlLoader
except ImportError:  # pragma: no cover
    from yaml import SafeLoader as _YamlLoader  # type: ignore[assignment]

# 路径 → (content hash, 解析结果)。config.yaml / runtime.yaml 有 50–60KB 代理列表，
# 纯 Python SafeLoader 每次约 120ms；一次 CLI 里 request() 会读配置三遍。
# 读文件本身只要 0ms 量级，用内容哈希判断是否需要再 parse。
_YAML_CACHE: dict[str, tuple[int, Any]] = {}


@dataclass(frozen=True)
class AppPaths:
    config_dir: Path
    data_dir: Path
    state_dir: Path


def default_paths(home: Path | None = None) -> AppPaths:
    base_home = home or Path.home()
    if home is None:
        config_home = Path(os.environ.get("XDG_CONFIG_HOME", base_home / ".config"))
        data_home = Path(os.environ.get("XDG_DATA_HOME", base_home / ".local" / "share"))
        state_home = Path(os.environ.get("XDG_STATE_HOME", base_home / ".local" / "state"))
    else:
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


def load_yaml_file(path: Path) -> Any:
    """读 YAML。文件不存在返回 None。按路径 + 内容哈希缓存，写入后自动失效。"""
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    digest = hash(raw)
    cache_key = str(path)
    hit = _YAML_CACHE.get(cache_key)
    if hit is not None and hit[0] == digest:
        return hit[1]
    data = yaml.load(raw.decode("utf-8"), Loader=_YamlLoader)
    _YAML_CACHE[cache_key] = (digest, data)
    return data


def read_config(paths: AppPaths) -> dict:
    data = load_yaml_file(config_file(paths))
    return data if isinstance(data, dict) else {}
