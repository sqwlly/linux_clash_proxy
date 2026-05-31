from pathlib import Path

from cproxy.config import AppPaths
from cproxy.tui.screens.subscriptions import (
    build_import_subscription_command,
    build_import_update_env,
    format_subscription_result,
    redact_subscription_url,
    subscription_group_rows,
    write_user_refresh_script,
)


def test_build_import_subscription_command_includes_attach_target():
    assert build_import_subscription_command(
        "/usr/local/bin/clash-proxy",
        "https://example.test/sub",
        dry_run=True,
        group="CyberGuard",
        attach_to="AI-MANUAL",
        config_path=Path("/home/me/.config/cproxy/config.yaml"),
        update_script="/usr/local/bin/clash-proxy-update",
    ) == [
        "/usr/local/bin/clash-proxy",
        "import-subscription",
        "https://example.test/sub",
        "--dry-run",
        "--config-file",
        "/home/me/.config/cproxy/config.yaml",
        "--update-script",
        "/usr/local/bin/clash-proxy-update",
        "--group",
        "CyberGuard",
        "--attach-to",
        "AI-MANUAL",
    ]


def test_build_import_update_env_points_to_user_paths(tmp_path):
    paths = AppPaths(
        config_dir=tmp_path / ".config" / "cproxy",
        data_dir=tmp_path / ".local" / "share" / "cproxy",
        state_dir=tmp_path / ".local" / "state" / "cproxy",
    )
    env = build_import_update_env(paths, tmp_path / "refresh.sh", "/usr/local/bin/clash-proxy")

    assert env["CONFIG_FILE"] == str(paths.config_dir / "config.yaml")
    assert env["RUNTIME_CONFIG"] == str(paths.data_dir / "runtime.yaml")
    assert env["REFRESH_SCRIPT"] == str(tmp_path / "refresh.sh")
    assert env["PROXY_SH"] == "/usr/local/bin/clash-proxy"
    assert env["CPROXY_CONFIG_DIR"] == str(paths.config_dir)
    assert env["CPROXY_DATA_DIR"] == str(paths.data_dir)
    assert env["CPROXY_STATE_DIR"] == str(paths.state_dir)


def test_write_user_refresh_script_renders_with_cproxy_paths():
    refresh_script = write_user_refresh_script()
    try:
        text = refresh_script.read_text(encoding="utf-8")
        assert "from cproxy.runtime import render_runtime" in text
        assert 'os.environ["CPROXY_CONFIG_DIR"]' in text
        assert refresh_script.stat().st_mode & 0o700 == 0o700
    finally:
        refresh_script.unlink(missing_ok=True)


def test_subscription_group_rows_show_attach_relationships():
    rows = subscription_group_rows(
        {
            "proxy-groups": [
                {"name": "AI-MANUAL", "type": "select", "proxies": ["AI-AUTO", "CyberGuard"]},
                {"name": "CyberGuard", "type": "select", "proxies": ["CyberGuard-Auto", "Node A", "DIRECT"]},
                {"name": "CyberGuard-Auto", "type": "fallback", "proxies": ["Node A", "Node B"]},
            ]
        }
    )

    assert ("AI-MANUAL", "select", "2", "─") in rows
    assert ("CyberGuard", "select", "3", "AI-MANUAL") in rows
    assert ("CyberGuard-Auto", "fallback", "2", "CyberGuard") in rows


def test_format_subscription_result_summarizes_preview_output():
    text = format_subscription_result(
        "订阅挂载完成: HTTP 200, 100 bytes, source=yaml, proxies=3, group=CyberGuard, attach_to=AI-MANUAL\nok",
        "",
        0,
        dry_run=True,
        group="CyberGuard",
        attach_to="AI-MANUAL",
    )

    assert text.startswith("Preview: OK")
    assert "Group: CyberGuard" in text
    assert "Attach to: AI-MANUAL" in text
    assert "Summary: 订阅挂载完成:" in text
    assert "stdout:" in text


def test_redact_subscription_url_hides_query_token():
    assert redact_subscription_url("https://example.test/api/v1/client/subscribe?token=secret") == (
        "https://example.test/api/v1/client/subscribe?..."
    )
