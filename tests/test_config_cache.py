"""config.yaml / runtime.yaml 解析缓存。"""

from __future__ import annotations

from pathlib import Path

from cproxy.config import default_paths, load_yaml_file, read_config


def test_read_config_reuses_parse_until_file_changes(tmp_path: Path, monkeypatch):
    loads: list[object] = []
    real_load = __import__("yaml").load

    def counting_load(stream, Loader=None):
        loads.append(1)
        return real_load(stream, Loader=Loader)

    monkeypatch.setattr("cproxy.config.yaml.load", counting_load)

    config_dir = tmp_path / ".config" / "cproxy"
    config_dir.mkdir(parents=True)
    path = config_dir / "config.yaml"
    path.write_text("mixed-port: 7890\n", encoding="utf-8")
    paths = default_paths(tmp_path)

    assert read_config(paths)["mixed-port"] == 7890
    assert read_config(paths)["mixed-port"] == 7890
    assert len(loads) == 1

    path.write_text("mixed-port: 7891\n", encoding="utf-8")
    assert read_config(paths)["mixed-port"] == 7891
    assert len(loads) == 2


def test_load_yaml_file_missing_returns_none(tmp_path: Path):
    assert load_yaml_file(tmp_path / "nope.yaml") is None
