import importlib.util
import base64
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import yaml


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
    monkeypatch.setattr(
        module,
        "download_subscription_with_curl",
        lambda url, max_bytes, timeout: module.SubscriptionContent(
            text=subscription.decode("utf-8"), status=200, content_type="", byte_count=len(subscription)
        ),
    )
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
    monkeypatch.setattr(module, "download_subscription_with_curl", lambda url, max_bytes, timeout: (_ for _ in ()).throw(RuntimeError("no fallback")))
    monkeypatch.setattr(
        sys,
        "argv",
        ["import_subscription.py", "https://example.test/sub", "--update-script", str(update_script)],
    )

    assert module.main() == 1
    stderr = capsys.readouterr().err
    assert "订阅导入校验失败" in stderr
    assert "subscription is not full YAML or Base64 node list" in stderr


def test_base64_vless_subscription_converts_to_minimal_yaml(monkeypatch, tmp_path, capsys):
    module = _load_import_module()
    update_script = tmp_path / "update_config.sh"
    update_script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    decoded = "\n".join(
        [
            "vless://uuid@example.test:443?type=ws&security=tls&sni=edge.example.test&fp=chrome&host=edge.example.test&path=%2Fws#Node%201",
            "vless://uuid@example.test:443?type=ws&security=tls&sni=edge.example.test&fp=chrome&host=edge.example.test&path=%2Fws#剩余流量：100 GB",
            "vless://uuid2@reality.example.test:8443?type=tcp&security=reality&sni=updates.example.test&fp=chrome&flow=xtls-rprx-vision&pbk=public-key&sid=abcd#Reality%201",
        ]
    )
    body = base64.b64encode(decoded.encode("utf-8"))
    captured_configs = []

    def fake_run(command, check):
        captured_configs.append(Path(command[2]).read_text(encoding="utf-8"))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(module, "urlopen", lambda request, timeout: _FakeResponse(body))
    monkeypatch.setattr(
        module,
        "download_subscription_with_curl",
        lambda url, max_bytes, timeout: module.SubscriptionContent(
            text=body.decode("utf-8"), status=200, content_type="", byte_count=len(body)
        ),
    )
    monkeypatch.setattr(module.subprocess, "run", fake_run)
    monkeypatch.setattr(
        sys,
        "argv",
        ["import_subscription.py", "https://example.test/sub", "--update-script", str(update_script)],
    )

    assert module.main() == 0
    stdout = capsys.readouterr().out
    assert "source=base64-uri-list" in stdout
    assert "proxies=2, groups=2, rules=1" in stdout
    config = yaml.safe_load(captured_configs[0])
    assert [proxy["name"] for proxy in config["proxies"]] == ["Node 1", "Reality 1"]
    assert config["proxies"][0]["ws-opts"] == {"path": "/ws", "headers": {"Host": "edge.example.test"}}
    assert config["proxies"][1]["reality-opts"] == {"public-key": "public-key", "short-id": "abcd"}
    assert config["proxy-groups"][0]["name"] == "PROXY"
    assert config["rules"] == ["MATCH,PROXY"]


def test_base64_vless_subscription_uses_requested_group_name(monkeypatch, tmp_path):
    module = _load_import_module()
    update_script = tmp_path / "update_config.sh"
    update_script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    decoded = "vless://uuid@example.test:443?type=ws&security=tls&sni=edge.example.test#Node%201"
    body = base64.b64encode(decoded.encode("utf-8"))
    captured_configs = []

    def fake_run(command, check):
        captured_configs.append(yaml.safe_load(Path(command[2]).read_text(encoding="utf-8")))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(module, "urlopen", lambda request, timeout: _FakeResponse(body))
    monkeypatch.setattr(
        module,
        "download_subscription_with_curl",
        lambda url, max_bytes, timeout: module.SubscriptionContent(
            text=body.decode("utf-8"), status=200, content_type="", byte_count=len(body)
        ),
    )
    monkeypatch.setattr(module.subprocess, "run", fake_run)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "import_subscription.py",
            "https://example.test/sub",
            "--update-script",
            str(update_script),
            "--group",
            "CyberGuard",
        ],
    )

    assert module.main() == 0
    config = captured_configs[0]
    assert [group["name"] for group in config["proxy-groups"]] == ["CyberGuard", "CyberGuard-Auto"]
    assert config["proxy-groups"][0]["proxies"] == ["CyberGuard-Auto", "Node 1", "DIRECT"]
    assert config["rules"] == ["MATCH,CyberGuard"]
