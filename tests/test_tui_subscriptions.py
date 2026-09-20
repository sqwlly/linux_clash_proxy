from cproxy.tui.screens.subscriptions import (
    redact_subscription_url,
    subscription_group_rows,
)


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


def test_redact_subscription_url_hides_query_token():
    assert redact_subscription_url("https://example.test/api/v1/client/subscribe?token=secret") == (
        "https://example.test/api/v1/client/subscribe?..."
    )
