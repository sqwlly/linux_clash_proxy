from __future__ import annotations

import os
import shutil
from pathlib import Path

from .config import AppPaths, read_config


DEFAULT_CONFIG = """mixed-port: 7890
external-controller: 127.0.0.1:9090
mode: rule
log-level: info
proxies: []
proxy-groups: []
rules: []
"""
DEFAULT_LEGACY_ROOT = Path("/root/clash_proxy")


def init_user_layout(paths: AppPaths) -> Path:
    paths.config_dir.mkdir(parents=True, exist_ok=True)
    paths.data_dir.mkdir(parents=True, exist_ok=True)
    paths.state_dir.mkdir(parents=True, exist_ok=True)

    config_file = paths.config_dir / "config.yaml"
    if not config_file.exists():
        config_file.write_text(DEFAULT_CONFIG, encoding="utf-8")

    return config_file


def migrate_from_legacy(paths: AppPaths, legacy_root: Path) -> Path:
    legacy_config = legacy_root / "config.yaml"
    if not legacy_config.exists():
        raise FileNotFoundError(f"legacy config not found: {legacy_config}")

    paths.config_dir.mkdir(parents=True, exist_ok=True)
    paths.data_dir.mkdir(parents=True, exist_ok=True)
    paths.state_dir.mkdir(parents=True, exist_ok=True)

    target = paths.config_dir / "config.yaml"
    shutil.copyfile(legacy_config, target)
    return target


def default_legacy_root() -> Path:
    return Path(os.environ.get("CPROXY_LEGACY_ROOT", str(DEFAULT_LEGACY_ROOT)))


def is_placeholder_config(paths: AppPaths) -> bool:
    config = read_config(paths)
    proxies = config.get("proxies") or []
    groups = config.get("proxy-groups") or []
    rules = config.get("rules") or []
    return not proxies and not groups and not rules


def auto_migrate_from_default_legacy(paths: AppPaths) -> Path | None:
    legacy_root = default_legacy_root()
    legacy_config = legacy_root / "config.yaml"
    if not legacy_config.exists():
        return None
    return migrate_from_legacy(paths, legacy_root)
