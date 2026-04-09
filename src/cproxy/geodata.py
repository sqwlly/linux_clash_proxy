from __future__ import annotations

from pathlib import Path

from .backend.models import ConnectivityCheckResult
from .config import AppPaths

COUNTRY_MMDB_NAME = "country.mmdb"


def country_mmdb_path(paths: AppPaths) -> Path:
    return paths.data_dir / COUNTRY_MMDB_NAME


def check_country_mmdb(paths: AppPaths) -> ConnectivityCheckResult:
    path = country_mmdb_path(paths)
    if path.is_file():
        return ConnectivityCheckResult(name="GeoIP 数据", ok=True, detail=f"已找到 {path}")
    return ConnectivityCheckResult(
        name="GeoIP 数据",
        ok=False,
        detail=f"缺少 {COUNTRY_MMDB_NAME}，请放置到 {path}",
    )
