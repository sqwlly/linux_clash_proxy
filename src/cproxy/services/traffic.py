from __future__ import annotations

import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from ..backend.api import APIBackend
from ..config import AppPaths, traffic_db_file

DEFAULT_TOP = 15
SEEN_RETENTION = timedelta(hours=24)
SAMPLE_RETENTION_DAYS = 90

DIMENSIONS = ("node", "rule", "host", "process")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS traffic_samples (
    day TEXT NOT NULL,
    hour TEXT NOT NULL,
    chain TEXT NOT NULL,
    rule TEXT NOT NULL,
    host TEXT NOT NULL,
    download INTEGER NOT NULL,
    upload INTEGER NOT NULL,
    PRIMARY KEY (day, hour, chain, rule, host)
);
CREATE TABLE IF NOT EXISTS traffic_process_samples (
    day TEXT NOT NULL,
    hour TEXT NOT NULL,
    process TEXT NOT NULL,
    chain TEXT NOT NULL,
    download INTEGER NOT NULL,
    upload INTEGER NOT NULL,
    PRIMARY KEY (day, hour, process, chain)
);
CREATE TABLE IF NOT EXISTS connections_seen (
    id TEXT PRIMARY KEY,
    host TEXT NOT NULL,
    rule TEXT NOT NULL,
    chain TEXT NOT NULL,
    download INTEGER NOT NULL,
    upload INTEGER NOT NULL,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    process TEXT NOT NULL DEFAULT ''
);
"""


def _conn_process(conn: dict[str, Any]) -> str:
    metadata = conn.get("metadata") or {}
    process = str(metadata.get("processPath") or "").strip()
    if not process:
        process = str(metadata.get("process") or "").strip()
    return process or "-"


@dataclass(frozen=True)
class TrafficRow:
    label: str
    download: int
    upload: int


@dataclass(frozen=True)
class ProcessTrafficRow:
    """按进程聚合的全量流量，附代理/直连拆分，用于回答“流量消耗在哪些进程”。"""

    label: str
    download: int
    upload: int
    proxy_download: int
    proxy_upload: int

    @property
    def total(self) -> int:
        return self.download + self.upload


@dataclass(frozen=True)
class ProcessTrafficReport:
    """按进程流量报表。

    ``total`` 是窗口内**所有进程**的合计，作为占比分母——不能用
    ``traffic_samples`` 的合计代替：进程归因上线时间晚于样本表，
    两者历史累计并不相等（实测差 ~24%），混用会让占比系统性低估。
    """

    rows: list[ProcessTrafficRow]
    total: int


@dataclass(frozen=True)
class CollectResult:
    connections: int
    download_delta: int
    upload_delta: int

    @property
    def total_delta(self) -> int:
        return self.download_delta + self.upload_delta


def format_bytes(size: int | float) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if abs(value) < 1024:
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.2f} {unit}"
        value /= 1024
    return f"{value:.2f} TB"


# 链路是否走直连。mihomo 以字面量 DIRECT 表示直连出站；若某分组当前选中的节点就是
# DIRECT，链路形如 "Group -> ... -> DIRECT"，该流量同样不经代理，所以判据是
# **链路末端动作**为 DIRECT。原实现用 `chain LIKE '%DIRECT%'` 子串匹配，有两个问题：
# SQLite 的 LIKE 对 ASCII 大小写不敏感（"direct" 也算直连），且名字里含 direct 的
# 代理节点（如 "DIRECT-US"）会被误判为直连。
_DIRECT_CHAIN = "(chain = 'DIRECT' OR chain LIKE '% -> DIRECT')"

# 按进程聚合全量流量并拆出代理部分。不做直连过滤：直连常占绝大多数，
# 只统计代理会漏掉主要消耗方（“仅代理”视角改由 proxy_* 两列体现）。
# grand_total 用窗口函数在同一次查询里取全量合计，作为占比分母（SQLite ≥ 3.25）。
_PROCESS_BREAKDOWN_SQL = f"""
SELECT process AS label,
       SUM(download) AS download,
       SUM(upload) AS upload,
       SUM(CASE WHEN NOT {_DIRECT_CHAIN} THEN download ELSE 0 END) AS proxy_download,
       SUM(CASE WHEN NOT {_DIRECT_CHAIN} THEN upload ELSE 0 END) AS proxy_upload,
       SUM(SUM(download) + SUM(upload)) OVER () AS grand_total
FROM traffic_process_samples
WHERE day >= ? AND day <= ?
GROUP BY process
ORDER BY (SUM(download) + SUM(upload)) DESC
LIMIT ?
"""


def _process_breakdown(
    db: sqlite3.Connection,
    since: str,
    until: str,
    top: int,
) -> ProcessTrafficReport:
    rows = db.execute(_PROCESS_BREAKDOWN_SQL, (since, until, top)).fetchall()
    return ProcessTrafficReport(
        rows=[
            ProcessTrafficRow(
                label=str(row["label"]),
                download=int(row["download"]),
                upload=int(row["upload"]),
                proxy_download=int(row["proxy_download"]),
                proxy_upload=int(row["proxy_upload"]),
            )
            for row in rows
        ],
        total=int(rows[0]["grand_total"]) if rows else 0,
    )


def _conn_label(conn: dict[str, Any]) -> tuple[str, str, str]:
    metadata = conn.get("metadata") or {}
    host = str(metadata.get("host") or metadata.get("sniffHost") or "").strip()
    if not host:
        host = str(metadata.get("destinationIP") or "").strip()
    host = host or "-"
    rule = str(conn.get("rule") or "").strip()
    payload = str(conn.get("rulePayload") or "").strip()
    if rule and payload:
        rule = f"{rule}({payload})"
    rule = rule or "-"
    chains = [str(item) for item in (conn.get("chains") or []) if str(item)]
    chain = " -> ".join(reversed(chains)) if chains else "-"
    return chain, rule, host


class TrafficService:
    def __init__(self, paths: AppPaths):
        self.paths = paths
        self.api = APIBackend(paths)

    def _connect(self) -> sqlite3.Connection:
        db_path = traffic_db_file(self.paths)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.executescript(_SCHEMA)
        # 旧库迁移：connections_seen 补 process 列
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(connections_seen)")}
        if "process" not in columns:
            conn.execute("ALTER TABLE connections_seen ADD COLUMN process TEXT NOT NULL DEFAULT ''")
        return conn

    def collect(self) -> CollectResult:
        payload = self.api.get_connections()
        connections = payload.get("connections") or []
        now = datetime.now().astimezone()
        day = now.strftime("%Y-%m-%d")
        hour = now.strftime("%H")
        now_text = now.isoformat(timespec="seconds")

        with closing(self._connect()) as db, db:
            seen = {
                row["id"]: row
                for row in db.execute("SELECT * FROM connections_seen")
            }
            download_delta = 0
            upload_delta = 0
            active_ids: set[str] = set()

            for conn in connections:
                conn_id = str(conn.get("id") or "")
                if not conn_id:
                    continue
                active_ids.add(conn_id)
                chain, rule, host = _conn_label(conn)
                download = int(conn.get("download") or 0)
                upload = int(conn.get("upload") or 0)
                previous = seen.get(conn_id)
                if previous is not None:
                    delta_download = max(0, download - int(previous["download"]))
                    delta_upload = max(0, upload - int(previous["upload"]))
                else:
                    delta_download = download
                    delta_upload = upload
                download_delta += delta_download
                upload_delta += delta_upload

                db.execute(
                    """
                    INSERT INTO traffic_samples (day, hour, chain, rule, host, download, upload)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT (day, hour, chain, rule, host) DO UPDATE SET
                        download = download + excluded.download,
                        upload = upload + excluded.upload
                    """,
                    (day, hour, chain, rule, host, delta_download, delta_upload),
                )
                process = _conn_process(conn)
                db.execute(
                    """
                    INSERT INTO traffic_process_samples (day, hour, process, chain, download, upload)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT (day, hour, process, chain) DO UPDATE SET
                        download = download + excluded.download,
                        upload = upload + excluded.upload
                    """,
                    (day, hour, process, chain, delta_download, delta_upload),
                )
                db.execute(
                    """
                    INSERT INTO connections_seen
                        (id, host, rule, chain, download, upload, first_seen, last_seen, process)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT (id) DO UPDATE SET
                        host = excluded.host,
                        rule = excluded.rule,
                        chain = excluded.chain,
                        download = excluded.download,
                        upload = excluded.upload,
                        last_seen = excluded.last_seen,
                        process = excluded.process
                    """,
                    (conn_id, host, rule, chain, download, upload, now_text, now_text, process),
                )

            closed_ids = set(seen) - active_ids
            if closed_ids:
                placeholders = ",".join("?" for _ in closed_ids)
                db.execute(
                    f"DELETE FROM connections_seen WHERE id IN ({placeholders})",
                    tuple(sorted(closed_ids)),
                )
            cutoff = (now - SEEN_RETENTION).isoformat(timespec="seconds")
            db.execute("DELETE FROM connections_seen WHERE last_seen < ?", (cutoff,))
            db.execute(
                "DELETE FROM traffic_samples WHERE day < ?",
                ((now - timedelta(days=SAMPLE_RETENTION_DAYS)).strftime("%Y-%m-%d"),),
            )
            db.execute(
                "DELETE FROM traffic_process_samples WHERE day < ?",
                ((now - timedelta(days=SAMPLE_RETENTION_DAYS)).strftime("%Y-%m-%d"),),
            )

        return CollectResult(
            connections=len(connections),
            download_delta=download_delta,
            upload_delta=upload_delta,
        )

    def report(
        self,
        days: int = 1,
        dimension: str | None = None,
        top: int = DEFAULT_TOP,
    ) -> dict[str, Any]:
        days = max(1, days)
        top = max(1, top)
        now = datetime.now().astimezone()
        since = (now - timedelta(days=days - 1)).strftime("%Y-%m-%d")
        until = now.strftime("%Y-%m-%d")

        with closing(self._connect()) as db, db:
            dimensions: dict[str, list[TrafficRow]] = {}
            wanted = [dimension] if dimension in DIMENSIONS else list(DIMENSIONS)
            for name in wanted:
                dimensions[name] = self._dimension_rows(db, name, since, until, top)
            daily = [
                TrafficRow(row["day"], int(row["download"]), int(row["upload"]))
                for row in db.execute(
                    """
                    SELECT day, SUM(download) AS download, SUM(upload) AS upload
                    FROM traffic_samples
                    WHERE day >= ? AND day <= ?
                    GROUP BY day ORDER BY day
                    """,
                    (since, until),
                )
            ]
            totals = db.execute(
                """
                SELECT COALESCE(SUM(download), 0) AS download, COALESCE(SUM(upload), 0) AS upload
                FROM traffic_samples
                WHERE day >= ? AND day <= ?
                """,
                (since, until),
            ).fetchone()

        return {
            "since": since,
            "until": until,
            "days": days,
            "total_download": int(totals["download"]),
            "total_upload": int(totals["upload"]),
            "dimensions": dimensions,
            "daily": daily,
        }

    def audit(
        self,
        days: int = 1,
        top: int = DEFAULT_TOP,
    ) -> dict[str, Any]:
        """代理流量审计：区分走代理与直连的流量，并列出走代理的目标主机明细。

        进程维度不在这里返回——调用方改用 :meth:`process_breakdown`，其口径为
        “全量 + 代理拆分”，与 status 面板一致。
        """
        days = max(1, days)
        top = max(1, top)
        now = datetime.now().astimezone()
        since = (now - timedelta(days=days - 1)).strftime("%Y-%m-%d")
        until = now.strftime("%Y-%m-%d")

        with closing(self._connect()) as db, db:
            rows = db.execute(
                f"""
                SELECT host AS label, SUM(download) AS download, SUM(upload) AS upload
                FROM traffic_samples
                WHERE day >= ? AND day <= ? AND NOT {_DIRECT_CHAIN}
                GROUP BY host ORDER BY (SUM(download) + SUM(upload)) DESC LIMIT ?
                """,
                (since, until, top),
            ).fetchall()
            totals = db.execute(
                f"""
                SELECT
                    COALESCE(SUM(CASE WHEN NOT {_DIRECT_CHAIN} THEN download ELSE 0 END), 0) AS p_down,
                    COALESCE(SUM(CASE WHEN NOT {_DIRECT_CHAIN} THEN upload ELSE 0 END), 0) AS p_up,
                    COALESCE(SUM(CASE WHEN {_DIRECT_CHAIN} THEN download ELSE 0 END), 0) AS d_down,
                    COALESCE(SUM(CASE WHEN {_DIRECT_CHAIN} THEN upload ELSE 0 END), 0) AS d_up
                FROM traffic_samples
                WHERE day >= ? AND day <= ?
                """,
                (since, until),
            ).fetchone()

        return {
            "since": since,
            "until": until,
            "proxy_download": int(totals["p_down"]),
            "proxy_upload": int(totals["p_up"]),
            "direct_download": int(totals["d_down"]),
            "direct_upload": int(totals["d_up"]),
            "rows": [
                TrafficRow(str(row["label"]), int(row["download"]), int(row["upload"]))
                for row in rows
            ],
        }

    def process_breakdown(
        self,
        days: int = 1,
        top: int = DEFAULT_TOP,
    ) -> ProcessTrafficReport:
        """按进程聚合全量流量，并拆出其中走代理的部分。

        不过滤 DIRECT 链路：直连常占绝大多数（实测 ~92%），只看代理会漏掉主要消耗方；
        直连量由 ``download - proxy_download`` 等派生（直连是总量的子集，恒非负）。
        """
        days = max(1, days)
        top = max(1, top)
        now = datetime.now().astimezone()
        since = (now - timedelta(days=days - 1)).strftime("%Y-%m-%d")
        until = now.strftime("%Y-%m-%d")

        with closing(self._connect()) as db, db:
            return _process_breakdown(db, since, until, top)

    def _dimension_rows(
        self,
        db: sqlite3.Connection,
        dimension: str,
        since: str,
        until: str,
        top: int,
    ) -> list[TrafficRow]:
        if dimension == "process":
            rows = db.execute(
                """
                SELECT process AS label, SUM(download) AS download, SUM(upload) AS upload
                FROM traffic_process_samples
                WHERE day >= ? AND day <= ?
                GROUP BY process
                ORDER BY (SUM(download) + SUM(upload)) DESC
                LIMIT ?
                """,
                (since, until, top),
            )
            return [
                TrafficRow(str(row["label"]), int(row["download"]), int(row["upload"]))
                for row in rows
            ]
        column = {
            "node": "chain",
            "rule": "rule",
            "host": "host",
        }[dimension]
        rows = db.execute(
            f"""
            SELECT {column} AS label, SUM(download) AS download, SUM(upload) AS upload
            FROM traffic_samples
            WHERE day >= ? AND day <= ?
            GROUP BY {column}
            ORDER BY (SUM(download) + SUM(upload)) DESC
            LIMIT ?
            """,
            (since, until, top),
        )
        return [
            TrafficRow(str(row["label"]), int(row["download"]), int(row["upload"]))
            for row in rows
        ]
