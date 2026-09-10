from __future__ import annotations

from pathlib import Path

import pytest

from cproxy.config import AppPaths
from cproxy.services.ipcheck import (
    IpCheckError,
    IpCheckService,
    IpPurityReport,
    parse_report,
    risk_verdict,
)


class FakeGroup:
    def __init__(self, current: str, candidates: list[str]):
        self.current = current
        self.candidates = candidates


class FakeSwitchAPI:
    def __init__(self, groups: dict[str, FakeGroup]):
        self.groups = groups
        self.switch_calls: list[tuple[str, str]] = []

    def get_groups(self) -> dict[str, FakeGroup]:
        return self.groups

    def switch_group(self, group: str, node: str) -> None:
        self.switch_calls.append((group, node))
        self.groups[group].current = node


def make_paths() -> AppPaths:
    base = Path("/tmp/cproxy-ipcheck-test")
    return AppPaths(config_dir=base / "config", data_dir=base / "data", state_dir=base / "state")


def make_service(
    groups: dict[str, FakeGroup],
    payload: dict | None = None,
    fetch_error: Exception | None = None,
) -> tuple[IpCheckService, FakeSwitchAPI]:
    api = FakeSwitchAPI(groups)
    service = IpCheckService(make_paths())
    service.api = api

    def fake_fetch(ip: str | None, timeout: int) -> dict:
        if fetch_error is not None:
            raise fetch_error
        return payload if payload is not None else {}

    service.fetch_report = fake_fetch
    return service, api


def make_recording_service(
    groups: dict[str, FakeGroup],
    payload: dict,
) -> tuple[IpCheckService, FakeSwitchAPI, list[tuple[str | None, int]]]:
    service, api = make_service(groups, payload)
    calls: list[tuple[str | None, int]] = []

    def recording_fetch(ip: str | None, timeout: int) -> dict:
        calls.append((ip, timeout))
        return payload

    service.fetch_report = recording_fetch
    return service, api, calls


SAMPLE_PAYLOAD = {
    "geo": {
        "ip": "132.226.225.227",
        "country": "South Korea",
        "city": "Seoul",
        "isp": "Oracle Corporation",
        "asn": "AS31898",
    },
    "ipType": "hosting",
    "usageType": "hosting",
    "nativeType": "broadcast",
    "risk": 35,
    "riskBreakdown": {
        "contributors": [
            {"source": "ip-api", "risk": 55, "weight": 0.5},
            {"source": "Scamalytics", "risk": 25, "weight": 0.9},
        ]
    },
    "signals": ["hosting"],
    "blocklist": {"listed": ["SpamCop"], "checked": 5, "total": 5},
    "sharedUsers": {"range": "1-5", "quality": "good"},
    "intel": {"datacenter": {"name": "Oracle Cloud"}},
    "services": [
        {"key": "chatgpt", "status": "restricted"},
        {"key": "claude", "status": "restricted"},
        {"key": "netflix", "status": "unknown"},
    ],
}


def test_risk_verdict_bands():
    assert risk_verdict(None) == "未知"
    assert risk_verdict(0) == "纯净"
    assert risk_verdict(20) == "一般"
    assert risk_verdict(35) == "一般"
    assert risk_verdict(40) == "注意"
    assert risk_verdict(60) == "高风险"
    assert risk_verdict(95) == "高风险"


def test_parse_report_extracts_all_fields():
    report = parse_report(SAMPLE_PAYLOAD)
    assert report.ip == "132.226.225.227"
    assert report.country == "South Korea"
    assert report.city == "Seoul"
    assert report.isp == "Oracle Corporation"
    assert report.asn == "AS31898"
    assert report.ip_type == "hosting"
    assert report.usage_type == "hosting"
    assert report.native_type == "broadcast"
    assert report.risk == 35
    assert report.verdict == "一般"
    assert report.signals == ["hosting"]
    assert report.blocklist_listed == ["SpamCop"]
    assert report.blocklist_checked == 5
    assert report.shared_users == "1-5"
    assert report.shared_quality == "good"
    assert report.datacenter == "Oracle Cloud"
    assert [(s.source, s.risk, s.weight) for s in report.sources] == [
        ("ip-api", 55, 0.5),
        ("Scamalytics", 25, 0.9),
    ]
    assert {item.key for item in report.ai_services} == {"chatgpt", "claude"}
    assert {item.key for item in report.services} == {"chatgpt", "claude", "netflix"}


def test_parse_report_tolerates_missing_fields():
    report = parse_report({})
    assert report.ip == "-"
    assert report.risk is None
    assert report.verdict == "未知"
    assert report.services == []
    assert report.ai_services == []
    assert isinstance(report, IpPurityReport)


def test_parse_report_tolerates_malformed_scalars():
    payload = {
        "risk": "n/a",
        "blocklist": {"listed": [], "checked": "many"},
        "geo": {"ip": None},
    }
    report = parse_report(payload)
    assert report.risk is None
    assert report.verdict == "未知"
    assert report.blocklist_checked == 0
    assert report.ip == "-"


def test_check_current_exit_without_switching():
    groups = {"CyberGuard": FakeGroup("KR 01", ["KR 01", "JP 01"])}
    service, api = make_service(groups, SAMPLE_PAYLOAD)

    report = service.check()

    assert isinstance(report, IpPurityReport)
    assert report.ip == "132.226.225.227"
    assert api.switch_calls == []
    assert report.switched is False


def test_check_with_node_switches_and_restores():
    groups = {"CyberGuard": FakeGroup("KR 01", ["KR 01", "JP 01"])}
    service, api = make_service(groups, SAMPLE_PAYLOAD)

    report = service.check(node="JP 01")

    assert api.switch_calls == [("CyberGuard", "JP 01"), ("CyberGuard", "KR 01")]
    assert report.switched is True
    assert report.switch_group == "CyberGuard"
    assert report.previous_selection == "KR 01"
    assert groups["CyberGuard"].current == "KR 01"


def test_check_with_explicit_group():
    groups = {"AI-SG": FakeGroup("SG 01", ["SG 01", "SG 02"])}
    service, api = make_service(groups, SAMPLE_PAYLOAD)

    service.check(group="AI-SG", node="SG 02")

    assert api.switch_calls == [("AI-SG", "SG 02"), ("AI-SG", "SG 01")]


def test_check_skips_switch_when_already_on_target():
    groups = {"CyberGuard": FakeGroup("JP 01", ["KR 01", "JP 01"])}
    service, api = make_service(groups, SAMPLE_PAYLOAD)

    report = service.check(node="JP 01")

    assert api.switch_calls == []
    assert report.switched is False


def test_check_rejects_unknown_group_and_node():
    groups = {"CyberGuard": FakeGroup("KR 01", ["KR 01"])}
    service, _ = make_service(groups, SAMPLE_PAYLOAD)

    with pytest.raises(IpCheckError, match="未找到代理组"):
        service.check(group="Nope", node="KR 01")
    with pytest.raises(IpCheckError, match="不存在节点"):
        service.check(group="CyberGuard", node="Ghost")
    assert groups["CyberGuard"].current == "KR 01"


def test_check_restores_selection_even_when_fetch_fails():
    groups = {"CyberGuard": FakeGroup("KR 01", ["KR 01", "JP 01"])}
    service, api = make_service(groups, fetch_error=IpCheckError("错误: ipok.io 请求失败"))

    with pytest.raises(IpCheckError):
        service.check(node="JP 01")

    assert api.switch_calls == [("CyberGuard", "JP 01"), ("CyberGuard", "KR 01")]
    assert groups["CyberGuard"].current == "KR 01"


def test_check_passes_ip_and_clamps_timeout():
    groups = {"CyberGuard": FakeGroup("KR 01", ["KR 01"])}
    service, _, calls = make_recording_service(groups, SAMPLE_PAYLOAD)

    service.check(ip="1.2.3.4", timeout=0)
    assert calls == [("1.2.3.4", 1)]

    service.check(timeout=15)
    assert calls[1] == (None, 15)
