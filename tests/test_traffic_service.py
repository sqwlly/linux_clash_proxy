from __future__ import annotations

import sqlite3

import pytest

from cproxy.config import AppPaths, traffic_db_file
from cproxy.services.traffic import TrafficService, format_bytes


class FakeAPIBackend:
    def __init__(self, snapshots: list[dict]):
        self.snapshots = snapshots
        self.calls = 0

    def get_connections(self) -> dict:
        payload = self.snapshots[min(self.calls, len(self.snapshots) - 1)]
        self.calls += 1
        return payload


def make_service(tmp_path, snapshots: list[dict]) -> TrafficService:
    paths = AppPaths(
        config_dir=tmp_path / "config",
        data_dir=tmp_path / "data",
        state_dir=tmp_path / "state",
    )
    service = TrafficService(paths)
    service.api = FakeAPIBackend(snapshots)
    return service


def connection(conn_id: str, host: str, rule: str, chain: list[str], download: int, upload: int, process: str = "") -> dict:
    return {
        "id": conn_id,
        "metadata": {"host": host, "destinationIP": "1.2.3.4", "process": process, "processPath": f"/usr/bin/{process}" if process else ""},
        "rule": rule,
        "rulePayload": "",
        "chains": list(chain),
        "download": download,
        "upload": upload,
    }


def test_collect_credits_deltas_per_connection(tmp_path):
    service = make_service(
        tmp_path,
        [
            {"connections": [connection("a", "example.com", "Match", ["Node1", "AI"], 1000, 100, process="python3")]},
            {
                "connections": [
                    connection("a", "example.com", "Match", ["Node1", "AI"], 3000, 150, process="python3"),
                    connection("b", "other.com", "DomainSuffix", ["DIRECT"], 500, 50, process="curl"),
                ]
            },
            {"connections": [connection("b", "other.com", "DomainSuffix", ["DIRECT"], 800, 80, process="curl")]},
        ],
    )

    first = service.collect()
    assert (first.download_delta, first.upload_delta) == (1000, 100)

    second = service.collect()
    assert (second.download_delta, second.upload_delta) == (2500, 100)

    third = service.collect()
    assert (third.download_delta, third.upload_delta) == (300, 30)

    report = service.report(days=1)
    assert report["total_download"] == 3800
    assert report["total_upload"] == 230

    node_rows = {row.label: row for row in report["dimensions"]["node"]}
    assert node_rows["AI -> Node1"].download == 3000
    assert node_rows["DIRECT"].download == 800

    # 进程维度: 全量历史聚合（连接 a 与 b 的累计差值）
    process_rows = {row.label: row for row in report["dimensions"]["process"]}
    assert process_rows["/usr/bin/python3"].download == 3000
    assert process_rows["/usr/bin/curl"].download == 800

    # 审计的进程口径: 仅统计走代理的进程
    audit = service.audit(days=1)
    audit_processes = {row.label: row for row in audit["process_rows"]}
    assert "/usr/bin/python3" in audit_processes
    assert "/usr/bin/curl" not in audit_processes


def test_report_grouping_by_rule_host_and_day(tmp_path):
    service = make_service(
        tmp_path,
        [
            {
                "connections": [
                    connection("a", "a.com", "Match", ["Node1", "AI"], 100, 10),
                    connection("b", "b.com", "DomainSuffix", ["Node2", "AI"], 200, 20),
                ]
            }
        ],
    )
    service.collect()

    report = service.report(days=7)
    assert [row.label for row in report["dimensions"]["rule"]] == ["DomainSuffix", "Match"]
    assert [row.label for row in report["dimensions"]["host"]] == ["b.com", "a.com"]
    assert len(report["daily"]) == 1


def test_collect_prunes_closed_connections_and_survives_restart_gap(tmp_path):
    service = make_service(
        tmp_path,
        [
            {"connections": [connection("a", "a.com", "Match", ["Node1"], 1000, 100)]},
            {"connections": []},
        ],
    )
    service.collect()
    service.collect()

    db_path = traffic_db_file(service.paths)
    with sqlite3.connect(db_path) as db:
        count = db.execute("SELECT COUNT(*) FROM connections_seen").fetchone()[0]
    assert count == 0

    report = service.report(days=1)
    assert report["total_download"] == 1000


def test_format_bytes_units():
    assert format_bytes(0) == "0 B"
    assert format_bytes(512) == "512 B"
    assert format_bytes(1024) == "1.00 KB"
    assert format_bytes(1024 * 1024 * 3.5) == "3.50 MB"


def test_process_display_label_strips_deleted_marker():
    from cproxy.cli_render import _process_display_label

    assert _process_display_label("/root/x/bin/codex (deleted)") == "/root/x/bin/codex"
    assert _process_display_label("/usr/bin/curl") == "/usr/bin/curl"
    assert _process_display_label("-") == "-"


def test_report_clamps_non_positive_top_and_days(tmp_path):
    service = make_service(
        tmp_path,
        [{"connections": [connection("a", "a.com", "Match", ["Node1"], 100, 10)]}],
    )
    service.collect()

    report = service.report(days=1, top=0)
    assert report["dimensions"]["node"], "top=0 不应导致 SQLite LIMIT -1 之外的异常行为且仍应返回行"

    clamped = service.report(days=-5)
    assert clamped["days"] == 1


def test_collect_tolerates_zero_delta_polls(tmp_path):
    snapshot = {"connections": [connection("a", "a.com", "Match", ["Node1"], 100, 10)]}
    service = make_service(tmp_path, [snapshot, snapshot, snapshot])
    service.collect()
    second = service.collect()
    third = service.collect()
    assert second.total_delta == 0
    assert third.total_delta == 0


@pytest.mark.parametrize("missing", ["metadata", "chains", "rule"])
def test_collect_tolerates_partial_metadata(tmp_path, missing):
    conn = connection("a", "a.com", "Match", ["Node1"], 100, 10)
    conn.pop(missing)
    service = make_service(tmp_path, [{"connections": [conn]}])
    result = service.collect()
    assert result.total_delta == 110


def test_collect_and_report_close_db_connections(tmp_path):
    service = make_service(tmp_path, [{"connections": [connection("a", "a.com", "Match", ["Node1"], 100, 10)]}])
    captured = []
    original_connect = service._connect

    def spy_connect():
        db = original_connect()
        captured.append(db)
        return db

    service._connect = spy_connect
    service.collect()
    service.report(days=1)

    assert len(captured) == 2
    for db in captured:
        with pytest.raises(sqlite3.ProgrammingError):
            db.execute("SELECT 1")
