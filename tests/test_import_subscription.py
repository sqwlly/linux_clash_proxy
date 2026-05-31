import importlib.util
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace


def _load_import_module():
    script = Path(__file__).resolve().parents[1] / "scripts" / "import_subscription.py"
    spec = importlib.util.spec_from_file_location("import_subscription_under_test", script)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class _FakeResponse:
    status = 200
    headers = {"Content-Type": "text/html; charset=utf-8"}

    def __init__(self, body: bytes):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self, size: int):
        return self.body[:size]


def test_downloaded_full_yaml_subscription_runs_update_script(monkeypatch, tmp_path, capsys):
    module = _load_import_module()
    update_script = tmp_path / "update_config.sh"
    update_script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    subscription = b"""
proxies:
  - name: Node
    type: direct
proxy-groups:
  - name: PROXY
    type: select
    proxies: [Node]
rules:
  - MATCH,PROXY
"""
    commands = []

    def fake_urlopen(request, timeout):
        assert timeout == 20
        return _FakeResponse(subscription)

    def fake_run(command, check):
        commands.append(command)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(module, "urlopen", fake_urlopen)
    monkeypatch.setattr(module.subprocess, "run", fake_run)
    monkeypatch.setattr(
        sys,
        "argv",
        ["import_subscription.py", "https://example.test/sub", "--update-script", str(update_script)],
    )

    assert module.main() == 0
    stdout = capsys.readouterr().out
    assert "HTTP 200" in stdout
    assert "proxies=1, groups=1, rules=1" in stdout
    assert len(commands) == 1
    assert commands[0][0:2] == [str(update_script), "--dry-run"]
    assert not Path(commands[0][2]).exists()


def test_rejects_node_uri_list_without_full_yaml(monkeypatch, tmp_path, capsys):
    module = _load_import_module()
    update_script = tmp_path / "update_config.sh"
    update_script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")

    monkeypatch.setattr(module, "urlopen", lambda request, timeout: _FakeResponse(b"ss://example"))
    monkeypatch.setattr(
        sys,
        "argv",
        ["import_subscription.py", "https://example.test/sub", "--update-script", str(update_script)],
    )

    assert module.main() == 1
    stderr = capsys.readouterr().err
    assert "订阅导入校验失败" in stderr
    assert "top-level YAML must be a mapping" in stderr
