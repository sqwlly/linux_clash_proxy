from __future__ import annotations

import shutil
from pathlib import Path

from .config import AppPaths


DEFAULT_CONFIG = """mixed-port: 7890
external-controller: 127.0.0.1:9090
mode: rule
log-level: info
proxies: []
proxy-groups: []
rules: []
"""


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
