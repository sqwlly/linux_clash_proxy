import sys
from pathlib import Path

# 优先使用仓库内 src/ 的 cproxy，避免命中 site-packages 中的过期安装快照
SRC_DIR = Path(__file__).resolve().parents[1] / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

import pytest


@pytest.fixture(autouse=True)
def isolate_xdg_environment(monkeypatch):
    for name in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME"):
        monkeypatch.delenv(name, raising=False)
    for name in ("NO_PROXY", "no_proxy"):
        monkeypatch.setenv(name, "127.0.0.1,localhost")
    monkeypatch.setenv("CPROXY_NO_SYSTEMD", "1")
