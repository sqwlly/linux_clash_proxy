from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
import yaml

from cproxy import cli
from cproxy.backend.api import APIUnavailableError
from cproxy.completion import _candidates
from cproxy.config import config_file, default_paths, read_config, runtime_file
from cproxy.services import tun
from cproxy.services.refresh import update_source_from_subscription
from cproxy.snapshots import list_snapshots


@pytest.fixture
def configured(tmp_path, monkeypatch):
    paths = default_paths(tmp_path)
    paths.config_dir.mkdir(parents=True)
    config_file(paths).write_text(
        """mixed-port: 7890
proxies:
  - {name: '🇺🇸 US 01', type: ss, server: 127.0.0.1, port: 1, cipher: aes-128-gcm, password: test}
  - {name: '🇸🇬 SG 01', type: ss, server: 127.0.0.1, port: 1, cipher: aes-128-gcm, password: test}
proxy-groups:
  - {name: PROXY, type: select, proxies: ['🇺🇸 US 01', '🇸🇬 SG 01']}
rules: ['MATCH,PROXY']
""",
        encoding="utf-8",
    )
    monkeypatch.setattr(tun.ProcessBackend, "status", lambda self: SimpleNamespace(running=False, pid=None))
    monkeypatch.setattr(tun, "_prerequisites", lambda pid: [])
    return paths


def test_enable_renders_snapshots_and_disable_preserves_custom_settings(configured):
    paths = configured
    original = config_file(paths).read_bytes()
    report = tun.TunService(paths).configure(True)

    config = read_config(paths)
    assert config["tun"]["enable"] is True
    assert config["tun"]["auto-route"] is True
    assert config["dns"]["enable"] is True
    assert yaml.safe_load(runtime_file(paths).read_text())["tun"] == config["tun"]
    assert list_snapshots(paths, "config")[0].read_bytes() == original
    assert config_file(paths).stat().st_mode & 0o777 == 0o600
    assert report.kernel_enabled is None
    assert report.configured_enabled is True

    config["tun"]["route-exclude-address"] = ["10.0.0.0/8"]
    config["tun"]["stack"] = "gvisor"
    config_file(paths).write_text(yaml.safe_dump(config))
    tun.TunService(paths).configure(False)
    tun.TunService(paths).configure(True)
    current = read_config(paths)
    assert current["tun"]["stack"] == "gvisor"
    assert current["tun"]["route-exclude-address"] == ["10.0.0.0/8"]
    assert current["dns"] == config["dns"]


def test_enable_preserves_existing_dns_settings(configured):
    config = read_config(configured)
    config["dns"] = {"enable": False, "nameserver": ["https://dns.example/dns-query"], "enhanced-mode": "redir-host"}
    config_file(configured).write_text(yaml.safe_dump(config))
    tun.TunService(configured).configure(True)
    assert read_config(configured)["dns"] == {**config["dns"], "enable": True}


def test_disable_does_not_create_dns_config(configured):
    tun.TunService(configured).configure(False)
    assert "dns" not in read_config(configured)
    assert read_config(configured)["tun"] == {"enable": False}


def test_subscription_refresh_preserves_local_tun_and_dns(configured, monkeypatch):
    tun.TunService(configured).configure(True)
    before = read_config(configured)
    remote = {**before, "tun": {"enable": False}, "dns": {"enable": False}}
    remote["proxies"] = [{**proxy, "port": 2} for proxy in before["proxies"]]
    monkeypatch.setattr("cproxy.services.refresh._download_subscription", lambda *args: (yaml.safe_dump(remote).encode(), None))

    update_source_from_subscription(configured, "https://example.invalid/sub")
    after = read_config(configured)
    assert after["tun"] == before["tun"]
    assert after["dns"] == before["dns"]
    assert after["proxies"] == remote["proxies"]


@pytest.mark.parametrize("error", [ValueError("无法生成 AI 出口"), APIUnavailableError("无法读取 controller 凭据")])
def test_render_failure_restores_exact_source(configured, monkeypatch, error):
    original = config_file(configured).read_bytes()

    def fail(self):
        raise error

    monkeypatch.setattr(tun.RuntimeBackend, "render_runtime", fail)
    with pytest.raises(type(error)):
        tun.TunService(configured).configure(True)
    assert config_file(configured).read_bytes() == original
    assert not runtime_file(configured).exists()
    assert not list(configured.config_dir.glob(".config.yaml.*"))


@pytest.mark.parametrize("bad", ["tun: false", "dns: []", "[]"])
def test_invalid_config_is_not_overwritten(configured, bad):
    config_file(configured).write_text(bad)
    with pytest.raises(ValueError):
        tun.TunService(configured).configure(True)
    assert config_file(configured).read_text() == bad


@pytest.mark.parametrize("running,issues", [(False, []), (True, ["缺少 CAP_NET_ADMIN"])])
def test_apply_preflight_fails_without_mutating_config(configured, monkeypatch, running, issues):
    original = config_file(configured).read_bytes()
    monkeypatch.setattr(tun.ProcessBackend, "status", lambda self: SimpleNamespace(running=running, pid=123))
    monkeypatch.setattr(tun, "_prerequisites", lambda pid: issues)
    with pytest.raises(RuntimeError, match="未修改配置"):
        tun.TunService(configured).configure(True, apply=True)
    assert config_file(configured).read_bytes() == original
    assert not list_snapshots(configured)


def test_apply_requires_api_before_saving(configured, monkeypatch):
    original = config_file(configured).read_bytes()
    monkeypatch.setattr(tun.ProcessBackend, "status", lambda self: SimpleNamespace(running=True, pid=123))

    def fail(self):
        raise APIUnavailableError("API 不可访问")

    monkeypatch.setattr(tun.APIBackend, "get_config", fail)
    with pytest.raises(APIUnavailableError):
        tun.TunService(configured).configure(True, apply=True)
    assert config_file(configured).read_bytes() == original


@pytest.mark.parametrize("enabled", [True, False])
def test_apply_reloads_and_checks_live_tun(configured, monkeypatch, enabled):
    calls = []
    live = {"tun": {"enable": not enabled, "device": "lo"}}
    monkeypatch.setattr(tun.ProcessBackend, "status", lambda self: SimpleNamespace(running=True, pid=123))
    monkeypatch.setattr(tun.APIBackend, "get_config", lambda self: live)
    # lo 保证测试无需创建 TUN 网卡；API 的开关变化才是验证对象。
    config = read_config(configured)
    config["tun"] = {"device": "lo"}
    config_file(configured).write_text(yaml.safe_dump(config))

    def reload(self, path):
        calls.append(path)
        live["tun"]["enable"] = enabled

    monkeypatch.setattr(tun.APIBackend, "reload_config", reload)
    report = tun.TunService(configured).configure(enabled, apply=True)
    assert calls == [str(runtime_file(configured))]
    assert report.kernel_enabled is enabled
    assert report.configured_enabled is enabled


def test_failed_reload_does_not_report_success(configured, monkeypatch):
    monkeypatch.setattr(tun.ProcessBackend, "status", lambda self: SimpleNamespace(running=True, pid=123))
    monkeypatch.setattr(tun.APIBackend, "get_config", lambda self: {"tun": {"enable": False}})

    def fail(self, path):
        raise APIUnavailableError("failed")

    monkeypatch.setattr(tun.APIBackend, "reload_config", fail)
    with pytest.raises(RuntimeError, match="已保存.*热重载失败"):
        tun.TunService(configured).configure(True, apply=True)
    assert read_config(configured)["tun"]["enable"] is True


def test_api_acceptance_without_enabled_tun_is_failure(configured, monkeypatch):
    monkeypatch.setattr(tun.ProcessBackend, "status", lambda self: SimpleNamespace(running=True, pid=123))
    monkeypatch.setattr(tun.APIBackend, "get_config", lambda self: {"tun": {"enable": False}})
    monkeypatch.setattr(tun.APIBackend, "reload_config", lambda *args: {})
    with pytest.raises(RuntimeError, match="未确认生效"):
        tun.TunService(configured).configure(True, apply=True)


@pytest.mark.parametrize("payload", [{}, {"tun": {}}, {"tun": {"enable": "false"}}])
def test_missing_or_invalid_api_status_stays_unknown(configured, monkeypatch, payload):
    monkeypatch.setattr(tun.ProcessBackend, "status", lambda self: SimpleNamespace(running=True, pid=123))
    monkeypatch.setattr(tun.APIBackend, "get_config", lambda self: payload)
    assert tun.TunService(configured).status().kernel_enabled is None


def test_api_enabled_without_interface_warns(configured, monkeypatch):
    monkeypatch.setattr(tun.ProcessBackend, "status", lambda self: SimpleNamespace(running=True, pid=123))
    monkeypatch.setattr(tun.APIBackend, "get_config", lambda self: {"tun": {"enable": True, "device": "cproxy-missing"}})
    report = tun.TunService(configured).status()
    assert report.interface_present is False
    assert any("未发现对应网卡" in warning for warning in report.warnings)


def test_interface_lookup_uses_current_namespace_and_preserves_unknown(configured, monkeypatch):
    monkeypatch.setattr(tun.ProcessBackend, "status", lambda self: SimpleNamespace(running=True, pid=123))
    monkeypatch.setattr(tun.APIBackend, "get_config", lambda self: {"tun": {"enable": True, "device": "cproxy-tun"}})
    monkeypatch.setattr(tun.socket, "if_nameindex", lambda: [(1, "lo"), (2, "cproxy-tun")])
    assert tun.TunService(configured).status().interface_present is True

    def fail():
        raise OSError("接口列表不可读")

    monkeypatch.setattr(tun.socket, "if_nameindex", fail)
    assert tun.TunService(configured).status().interface_present is None


def test_capability_and_device_checks(monkeypatch):
    monkeypatch.setattr(tun, "_net_admin", lambda pid: False)
    monkeypatch.setattr(tun.sys, "platform", "linux")
    monkeypatch.setattr(tun.Path, "stat", lambda self: (_ for _ in ()).throw(FileNotFoundError()))
    issues = tun._prerequisites(123)
    assert any("/dev/net/tun" in issue for issue in issues)
    assert any("CAP_NET_ADMIN" in issue for issue in issues)


def test_cli_status_json_and_completion(configured, monkeypatch, capsys):
    monkeypatch.setattr(cli, "default_paths", lambda: configured)
    assert cli.run(["tun", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema_version"] == 1
    assert payload["command"] == "tun"
    assert payload["data"]["configured_enabled"] is False
    assert payload["data"]["kernel_enabled"] is None
    assert _candidates(configured, ["cproxy", "tun", ""], 2) == ["status", "on", "off"]
    assert "--apply" in _candidates(configured, ["cproxy", "tun", "--"], 2)


def test_cli_rejects_status_apply(configured, monkeypatch, capsys):
    monkeypatch.setattr(cli, "default_paths", lambda: configured)
    assert cli.run(["tun", "status", "--apply"]) == 1
    assert "只能与 tun on/off" in capsys.readouterr().err
