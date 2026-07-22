from __future__ import annotations

import json
import ssl
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from types import SimpleNamespace
from urllib.error import URLError
from urllib.request import HTTPSHandler, ProxyHandler

ROOT_DIR = Path(__file__).resolve().parents[1]
SRC_DIR = ROOT_DIR / "src"

sys.path.insert(0, str(SRC_DIR))

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

    def fake_open(request, timeout):
        captured["timeout"] = timeout
        raise URLError("boom")

    monkeypatch.setattr("cproxy.backend.api.build_opener", lambda *_handlers: SimpleNamespace(open=fake_open))

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

    def fake_open(request, timeout):
        captured["method"] = request.get_method()
        captured["url"] = request.full_url
        return FakeResponse()

    monkeypatch.setattr("cproxy.backend.api.build_opener", lambda *_handlers: SimpleNamespace(open=fake_open))

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

    def fake_open(request, timeout):
        captured["url"] = request.full_url
        return FakeResponse()

    def fake_build_opener(*handlers):
        captured["handlers"] = handlers
        return SimpleNamespace(open=fake_open)

    monkeypatch.setattr("cproxy.backend.api.build_opener", fake_build_opener)

    backend = APIBackend(default_paths(tmp_path))

    assert backend.version() == {"version": "test"}
    assert captured["url"] == "https://127.0.0.1:9443/version"
    handlers = captured["handlers"]
    assert isinstance(handlers, tuple)
    assert any(isinstance(handler, ProxyHandler) and handler.proxies == {} for handler in handlers)
    https_handler = next(handler for handler in handlers if isinstance(handler, HTTPSHandler))
    assert isinstance(https_handler._context, ssl.SSLContext)
    assert https_handler._context.verify_mode == ssl.CERT_NONE


def test_api_backend_bypasses_environment_proxy(tmp_path, monkeypatch):
    class ControllerHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path != "/version":
                self.send_error(502, "request must not use environment proxy")
                return
            body = json.dumps({"version": "test"}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format, *_args):
            pass

    controller = ThreadingHTTPServer(("127.0.0.1", 0), ControllerHandler)
    thread = Thread(target=controller.serve_forever, daemon=True)
    thread.start()
    try:
        controller_url = f"http://127.0.0.1:{controller.server_port}"
        for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
            monkeypatch.setenv(key, controller_url)
        for key in ("NO_PROXY", "no_proxy"):
            monkeypatch.delenv(key, raising=False)

        config_dir = tmp_path / ".config" / "cproxy"
        config_dir.mkdir(parents=True)
        (config_dir / "config.yaml").write_text(
            f"external-controller: {controller_url}\n",
            encoding="utf-8",
        )

        backend = APIBackend(default_paths(tmp_path))
        assert backend.version() == {"version": "test"}
    finally:
        controller.shutdown()
        controller.server_close()
        thread.join(timeout=1)


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
