from __future__ import annotations

import sys
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
