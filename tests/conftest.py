import pytest


@pytest.fixture(autouse=True)
def isolate_xdg_environment(monkeypatch):
    for name in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME"):
        monkeypatch.delenv(name, raising=False)
