import sys
from pathlib import Path

sys.path.insert(0, "/root/clash_proxy/src")

from cproxy.config import AppPaths
from cproxy.geodata import check_country_mmdb, country_mmdb_path


def test_country_mmdb_path_uses_data_dir(tmp_path: Path):
    paths = AppPaths(
        config_dir=tmp_path / ".config" / "cproxy",
        data_dir=tmp_path / ".local" / "share" / "cproxy",
        state_dir=tmp_path / ".local" / "state" / "cproxy",
    )

    assert country_mmdb_path(paths) == tmp_path / ".local" / "share" / "cproxy" / "country.mmdb"


def test_check_country_mmdb_reports_missing_file(tmp_path: Path):
    paths = AppPaths(
        config_dir=tmp_path / ".config" / "cproxy",
        data_dir=tmp_path / ".local" / "share" / "cproxy",
        state_dir=tmp_path / ".local" / "state" / "cproxy",
    )

    result = check_country_mmdb(paths)

    assert result.ok is False
    assert result.name == "GeoIP 数据"
    assert "country.mmdb" in result.detail
    assert str(paths.data_dir / "country.mmdb") in result.detail


def test_check_country_mmdb_reports_present_file(tmp_path: Path):
    paths = AppPaths(
        config_dir=tmp_path / ".config" / "cproxy",
        data_dir=tmp_path / ".local" / "share" / "cproxy",
        state_dir=tmp_path / ".local" / "state" / "cproxy",
    )
    paths.data_dir.mkdir(parents=True)
    (paths.data_dir / "country.mmdb").write_bytes(b"test")

    result = check_country_mmdb(paths)

    assert result.ok is True
    assert str(paths.data_dir / "country.mmdb") in result.detail
