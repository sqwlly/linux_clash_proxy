from __future__ import annotations

import sys
import ssl
from types import SimpleNamespace
from urllib.error import URLError

sys.path.insert(0, "/root/clash_proxy/src")

from cproxy.backend.api import APIBackend, APIUnavailableError
from cproxy.config import default_paths


def test_api_backend_uses_short_default_timeout(tmp_path, monkeypatch):
    config_dir = tmp_path / ".config" / "cproxy"
    config_dir.mkdir(parents=True)
    (config_dir / "config.yaml").write_text(
        "external-controller: 127.0.0.1:9\n",
        encoding="utf-8",
    )

    captured: dict[str, object] = {}

    def fake_urlopen(request, timeout):
        captured["timeout"] = timeout
        raise URLError("boom")

    monkeypatch.setattr("cproxy.backend.api.urlopen", fake_urlopen)

    backend = APIBackend(default_paths(tmp_path))

    try:
        backend.request("GET", "/proxies")
    except APIUnavailableError:
        pass

    assert captured["timeout"] == 2


def test_api_backend_allows_empty_success_response(tmp_path, monkeypatch):
    config_dir = tmp_path / ".config" / "cproxy"
    config_dir.mkdir(parents=True)
    (config_dir / "config.yaml").write_text(
        "external-controller: 127.0.0.1:9090\n",
        encoding="utf-8",
    )

    captured: dict[str, object] = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return b""

    def fake_urlopen(request, timeout):
        captured["method"] = request.get_method()
        captured["url"] = request.full_url
        return FakeResponse()

    monkeypatch.setattr("cproxy.backend.api.urlopen", fake_urlopen)

    backend = APIBackend(default_paths(tmp_path))
    backend.close_connection("conn/1")

    assert captured["method"] == "DELETE"
    assert captured["url"].endswith("/connections/conn%2F1")


def test_api_backend_prefers_tls_controller(tmp_path):
    config_dir = tmp_path / ".config" / "cproxy"
    config_dir.mkdir(parents=True)
    (config_dir / "config.yaml").write_text(
        "external-controller: 127.0.0.1:9090\n"
        "external-controller-tls: 127.0.0.1:9443\n",
        encoding="utf-8",
    )

    backend = APIBackend(default_paths(tmp_path))

    assert backend.controller_url() == "https://127.0.0.1:9443"


def test_api_backend_allows_self_signed_loopback_tls_controller(tmp_path, monkeypatch):
    config_dir = tmp_path / ".config" / "cproxy"
    config_dir.mkdir(parents=True)
    (config_dir / "config.yaml").write_text(
        "external-controller-tls: 127.0.0.1:9443\n",
        encoding="utf-8",
    )
    captured: dict[str, object] = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return b'{"version":"test"}'

    def fake_urlopen(request, timeout, context=None):
        captured["url"] = request.full_url
        captured["context"] = context
        return FakeResponse()

    monkeypatch.setattr("cproxy.backend.api.urlopen", fake_urlopen)

    backend = APIBackend(default_paths(tmp_path))

    assert backend.version() == {"version": "test"}
    assert captured["url"] == "https://127.0.0.1:9443/version"
    assert isinstance(captured["context"], ssl.SSLContext)
    assert captured["context"].verify_mode == ssl.CERT_NONE


def test_api_backend_rejects_unix_controller(tmp_path):
    config_dir = tmp_path / ".config" / "cproxy"
    config_dir.mkdir(parents=True)
    (config_dir / "config.yaml").write_text(
        "external-controller-unix: /run/mihomo.sock\n",
        encoding="utf-8",
    )

    backend = APIBackend(default_paths(tmp_path))

    try:
        backend.controller_url()
    except APIUnavailableError as exc:
        assert "external-controller-unix" in str(exc)
    else:
        raise AssertionError("expected APIUnavailableError")


def test_api_backend_reads_secret_file(tmp_path):
    config_dir = tmp_path / ".config" / "cproxy"
    config_dir.mkdir(parents=True)
    secret_path = tmp_path / "controller.secret"
    secret_path.write_text("file-secret\n", encoding="utf-8")
    (config_dir / "config.yaml").write_text(
        f"secret-file: {secret_path}\nsecret: inline-secret\n",
        encoding="utf-8",
    )

    backend = APIBackend(default_paths(tmp_path))

    assert backend.api_secret() == "file-secret"


def test_api_backend_reads_systemd_credential(tmp_path, monkeypatch):
    config_dir = tmp_path / ".config" / "cproxy"
    config_dir.mkdir(parents=True)
    credentials_dir = tmp_path / "credentials"
    credentials_dir.mkdir()
    (credentials_dir / "controller-secret").write_text("credential-secret\n", encoding="utf-8")
    (config_dir / "config.yaml").write_text(
        "secret-systemd-credential: controller-secret\nsecret: inline-secret\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(credentials_dir))

    backend = APIBackend(default_paths(tmp_path))

    assert backend.api_secret() == "credential-secret"


def test_api_backend_reads_keyring_secret(tmp_path, monkeypatch):
    config_dir = tmp_path / ".config" / "cproxy"
    config_dir.mkdir(parents=True)
    (config_dir / "config.yaml").write_text(
        "secret-keyring-service: cproxy\nsecret-keyring-username: controller\n",
        encoding="utf-8",
    )
    fake_keyring = SimpleNamespace(get_password=lambda service, username: f"{service}:{username}:secret")
    monkeypatch.setitem(sys.modules, "keyring", fake_keyring)

    backend = APIBackend(default_paths(tmp_path))

    assert backend.api_secret() == "cproxy:controller:secret"
