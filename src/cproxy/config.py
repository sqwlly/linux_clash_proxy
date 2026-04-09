from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass(frozen=True)
class AppPaths:
    config_dir: Path
    data_dir: Path
    state_dir: Path


def default_paths(home: Path | None = None) -> AppPaths:
    base_home = home or Path.home()
    config_home = Path(os.environ.get("XDG_CONFIG_HOME", base_home / ".config"))
    data_home = Path(os.environ.get("XDG_DATA_HOME", base_home / ".local" / "share"))
    state_home = Path(os.environ.get("XDG_STATE_HOME", base_home / ".local" / "state"))
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


def log_file(paths: AppPaths) -> Path:
    return paths.state_dir / "cproxy.log"


def read_config(paths: AppPaths) -> dict:
    path = config_file(paths)
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}
