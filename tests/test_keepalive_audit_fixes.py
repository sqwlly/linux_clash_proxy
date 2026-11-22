"""Regression tests for keepalive, ai-watch, rebalance, probe, probe_history, ipcheck & ban audit fixes."""
from __future__ import annotations

import asyncio
import http.client
import json
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

ROOT_DIR = Path(__file__).resolve().parents[1]
SRC_DIR = ROOT_DIR / "src"
sys.path.insert(0, str(SRC_DIR))

from cproxy.backend.models import ProxyGroup
from cproxy.config import (
    AppPaths,
    ai_gemini_order_file,
    ai_manual_order_file,
    ai_watch_state_file,
    conn_activity_state_file,
)
from cproxy.services import ai_watch as ai_watch_mod
from cproxy.services import h2_keepalive as h2_mod
from cproxy.services import ipcheck as ipcheck_mod
from cproxy.services import probe as probe_mod
from cproxy.services import rebalance as rebalance_mod
from cproxy.services.ban import add_ban, load_active_bans, matches_ban_pattern, remove_ban
from cproxy.services.probe_history import (
    MAX_HISTORY_LINES,
    load_history_penalties,
    probe_history_file,
    record_probe_history,
)


def _paths(tmp_path: Path) -> AppPaths:
    return AppPaths(
        config_dir=tmp_path / "cfg",
        data_dir=tmp_path / "data",
        state_dir=tmp_path / "state",
    )


# --- 1. CA/leaf key permissions, openssl renewal fallback, Debian anchor ---


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX mode bits only")
def test_ensure_local_ca_and_leaf_cert_permissions_and_existing_key_chmod(tmp_path: Path) -> None:
    tls_dir = tmp_path / "tls"
    ca_crt, leaf_crt, leaf_key, _mtime = h2_mod.ensure_local_ca_and_leaf_cert(
        tls_dir,
        install_system_trust=False,
    )
    ca_key = tls_dir / "cproxy-h2-ca.key"
    assert ca_crt.is_file() and leaf_crt.is_file()
    assert stat.S_IMODE(tls_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(ca_key.stat().st_mode) == 0o600
    assert stat.S_IMODE(leaf_key.stat().st_mode) == 0o600

    # Relax existing permissions and confirm ensure_local_ca_and_leaf_cert re-tightens them
    os.chmod(tls_dir, 0o755)
    os.chmod(ca_key, 0o644)
    os.chmod(leaf_key, 0o644)
    h2_mod.ensure_local_ca_and_leaf_cert(tls_dir, install_system_trust=False)
    assert stat.S_IMODE(tls_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(ca_key.stat().st_mode) == 0o600
    assert stat.S_IMODE(leaf_key.stat().st_mode) == 0o600


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX mode bits only")
def test_openssl_cert_generation_creates_0600_keys_and_no_tmp_leftovers(tmp_path: Path) -> None:
    if not shutil.which("openssl"):
        pytest.skip("openssl not available")
    tls_dir = tmp_path / "tls_openssl"
    tls_dir.mkdir(mode=0o700)
    ca_key = tls_dir / "cproxy-h2-ca.key"
    ca_crt = tls_dir / "cproxy-h2-ca.crt"
    leaf_key = tls_dir / "cloudcode.key"
    leaf_crt = tls_dir / "cloudcode.crt"
    h2_mod._generate_certs_with_openssl(
        tls_dir,
        ca_key,
        ca_crt,
        leaf_key,
        leaf_crt,
        need_ca=True,
        need_leaf=True,
    )
    assert stat.S_IMODE(ca_key.stat().st_mode) == 0o600
    assert stat.S_IMODE(leaf_key.stat().st_mode) == 0o600
    assert not list(tls_dir.glob("*.tmp"))


def test_leaf_cert_needs_renewal_openssl_fallback_when_cryptography_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if not shutil.which("openssl"):
        pytest.skip("openssl not available")
    tls_dir = tmp_path / "tls_renew"
    _ca_crt, leaf_crt, _leaf_key, _ = h2_mod.ensure_local_ca_and_leaf_cert(
        tls_dir, install_system_trust=False
    )
    san_marker = tls_dir / "cloudcode.sans"

    real_import = __import__

    def _block_crypto(name: str, *args, **kwargs):
        if name == "cryptography" or name.startswith("cryptography."):
            raise ImportError("simulated missing cryptography")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", _block_crypto)
    # Valid 365-day cert -> openssl -checkend 2592000 returns 0 -> no renewal needed
    assert h2_mod._leaf_cert_needs_renewal(leaf_crt, san_marker) is False

    # Corrupt cert -> openssl -checkend returns non-zero -> needs renewal
    leaf_crt.write_text("NOT A PEM CERT", encoding="utf-8")
    assert h2_mod._leaf_cert_needs_renewal(leaf_crt, san_marker) is True


def test_ensure_local_ca_checks_debian_anchor_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tls_dir = tmp_path / "tls_deb"
    ca_crt, _leaf_crt, _leaf_key, _ = h2_mod.ensure_local_ca_and_leaf_cert(
        tls_dir, install_system_trust=False
    )
    deb_anchor = tmp_path / "deb-anchor" / "cproxy-h2-ca.crt"
    deb_anchor.parent.mkdir(parents=True)
    deb_anchor.write_bytes(ca_crt.read_bytes())
    target_mtime = 1_750_000_123.0
    os.utime(deb_anchor, (target_mtime, target_mtime))

    monkeypatch.setattr(
        h2_mod,
        "_SYSTEM_CA_ANCHOR_PATHS",
        (tmp_path / "missing-rhel" / "cproxy-h2-ca.crt", deb_anchor),
    )
    assert h2_mod.is_local_ca_trusted(ca_crt) is True
    _, _, _, trust_mtime = h2_mod.ensure_local_ca_and_leaf_cert(tls_dir, install_system_trust=False)
    assert abs(trust_mtime - target_mtime) < 1.0


# --- 2. SystemExit catching in ai_watch & rebalance ---


def test_ai_watch_survives_system_exit_from_switch_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _paths(tmp_path)
    groups = {
        "AI-MANUAL": ProxyGroup("AI-MANUAL", "fallback", "US-01", ["US-01", "JP-01"]),
        "AI-GEMINI": ProxyGroup("AI-GEMINI", "fallback", "KR-01", ["KR-01", "GB-01"]),
        "US-01": ProxyGroup("US-01", "ss", "-", [], delay=100, alive=True),
        "JP-01": ProxyGroup("JP-01", "ss", "-", [], delay=120, alive=True),
        "KR-01": ProxyGroup("KR-01", "ss", "-", [], delay=100, alive=True),
        "GB-01": ProxyGroup("GB-01", "ss", "-", [], delay=120, alive=True),
    }

    class _SystemExitQuery:
        def __init__(self) -> None:
            self.api = SimpleNamespace(get_groups=lambda: groups, get_connections=lambda: {"connections": []})

        def switch_group(self, group: str, target: str, **_kw):
            raise SystemExit(f"missing group {group}")

    monkeypatch.setattr(
        ai_watch_mod,
        "ai_target_verdict",
        lambda _p, u: ai_watch_mod.PROBE_OK if "google" in u else ai_watch_mod.PROBE_DEAD,
    )
    svc = ai_watch_mod.AIWatchService(paths, agy_log_dir=tmp_path / "no-agy", codex_log_dir=tmp_path / "no-codex")
    svc.query = _SystemExitQuery()  # type: ignore[assignment]

    svc.watch()
    rep2 = svc.watch()
    assert not any(r.switched for r in rep2.results)
    saved = json.loads(ai_watch_state_file(paths).read_text(encoding="utf-8"))
    assert "JP-01" in saved["AI-MANUAL"]["cooldown"]


def test_rebalance_survives_system_exit_from_switch_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _paths(tmp_path)
    order_path = ai_gemini_order_file(paths)

    class _SystemExitQuery:
        def __init__(self, _p) -> None:
            self.api = SimpleNamespace(
                get_groups=lambda: {
                    "AI-MANUAL": ProxyGroup("AI-MANUAL", "fallback", "SLOW", ["SLOW", "FAST"]),
                    "AI-GEMINI": ProxyGroup("AI-GEMINI", "fallback", "BAD", ["BAD", "GOOD"]),
                },
                get_connections=lambda: {"connections": []},
            )

        def switch_group(self, group: str, target: str, **_kw):
            raise SystemExit(f"cannot switch {group} -> {target}")

    monkeypatch.setattr(rebalance_mod, "QueryService", _SystemExitQuery)
    monkeypatch.setattr(rebalance_mod, "RuntimeBackend", lambda _p: SimpleNamespace(mixed_port=lambda: 7890))
    monkeypatch.setattr(probe_mod, "simulate_gemini_stream_hold", lambda **_kw: (False, "EOF"))
    monkeypatch.setattr(rebalance_mod, "ai_target_verdict", lambda *_a, **_kw: ai_watch_mod.PROBE_GEO_BLOCKED)

    svc = rebalance_mod.RebalanceService(paths)
    fake_report = SimpleNamespace(
        current="SLOW",
        current_verdict=probe_mod.StabilityVerdict(False, "fail"),
        verdict=probe_mod.StabilityVerdict(True, "ok"),
        summaries=[
            probe_mod.ProbeSummary("SLOW", (500,), failures=2),
            probe_mod.ProbeSummary("FAST", (100, 110, 105, 108, 102), failures=0),
        ],
    )
    assert svc._switch_if_current_unstable(fake_report, "FAST", "AI-MANUAL") is None

    ord_hold, sw_hold = svc._verify_gemini_stream_hold(["BAD", "GOOD"], order_path, switched_to=None)
    assert ord_hold == ["GOOD", "BAD"] and sw_hold is None

    ord_geo, sw_geo = svc._verify_gemini_geo_eligibility(["BAD", "GOOD"], order_path, switched_to=None)
    assert ord_geo == ["GOOD", "BAD"] and sw_geo is None


# --- 3. Multi-process RMW reload-and-merge & atomic writes ---


def test_ai_watch_save_state_merges_concurrent_rebalance_fields(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    # Initial watch state on disk
    ai_watch_mod._save_state(
        paths,
        {
            "watch_runs": 1,
            "last_rebalance_ts": 100.0,
            "rebalance_runs": 1,
            "gemini_geo_blocked": {"OLD-GEO": 50.0},
        },
    )
    # ai-watch loads state before a long probe
    loaded = ai_watch_mod._load_state(paths)
    loaded["watch_runs"] = 2

    # Concurrently, rebalance updates ai-watch.json on disk
    probe_mod.record_gemini_geo_blocked_node(paths, "CN-TW-01", now=200.0)
    probe_mod.record_stream_fragile_node(paths, "FRAG-01", now=200.0)

    def _bump_rebalance(data: dict) -> bool:
        data["last_rebalance_ts"] = 250.0
        data["rebalance_runs"] = int(data.get("rebalance_runs") or 0) + 1
        return True

    probe_mod.update_ai_watch_state(paths, _bump_rebalance)

    # Now ai-watch finishes and saves its stale snapshot while clearing OLD-GEO
    ai_watch_mod._save_state(paths, loaded, cleared_geo_blocked={"OLD-GEO": 180.0})

    final = ai_watch_mod._load_state(paths)
    assert final["watch_runs"] == 2
    assert final["last_rebalance_ts"] == 250.0
    assert final["rebalance_runs"] == 2
    assert "CN-TW-01" in final["gemini_geo_blocked"]
    assert "OLD-GEO" not in final["gemini_geo_blocked"]
    assert "FRAG-01" in final["stream_fragile"]
    assert not list(paths.state_dir.glob("*.tmp"))


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX mode bits only")
def test_atomic_writes_cleanup_on_error_and_set_0600(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    paths = _paths(tmp_path)
    ai_watch_mod._save_conn_activity(paths, {"c1": {"up": 10, "down": 20}})
    conn_file = conn_activity_state_file(paths)
    assert stat.S_IMODE(conn_file.stat().st_mode) == 0o600
    assert not list(paths.state_dir.glob("*.tmp"))

    target = paths.state_dir / "fail-atomic.json"
    monkeypatch.setattr(probe_mod.os, "replace", lambda _s, _d: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError, match="disk full"):
        probe_mod._atomic_write_text(target, "{}")
    assert not target.exists()
    assert not list(paths.state_dir.glob("*.tmp"))


# --- 4. passthrough scope + off-loop execution + stats offloading ---


def test_passthrough_check_scoped_to_h1_to_h2_hosts_and_runs_in_worker_thread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[int, int]] = []
    loop_tid: dict[str, int] = {}

    def _spy_passthrough(mtime: float, port: int) -> bool:
        calls.append((threading.get_ident(), port))
        return True

    monkeypatch.setattr(h2_mod, "client_prefers_passthrough", _spy_passthrough)
    proxy = h2_mod.H2KeepaliveProxy(
        tmp_path / "tls",
        install_system_trust=False,
        require_system_trust=False,
    )

    async def _run() -> None:
        loop_tid["main"] = threading.get_ident()

        async def mock_socks5(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
            await r.readexactly(3)
            w.write(b"\x05\x00")
            await w.drain()
            await r.readexactly(4)
            dlen = (await r.readexactly(1))[0]
            await r.readexactly(dlen + 2)
            w.write(b"\x05\x00\x00\x01\x7f\x00\x00\x01\x01\xbb")
            await w.drain()
            await r.read(16)
            w.close()

        up_srv = await asyncio.start_server(mock_socks5, "127.0.0.1", 0)
        proxy.upstream_port = up_srv.sockets[0].getsockname()[1]
        px_srv = await asyncio.start_server(proxy.handle_client, "127.0.0.1", 0)
        px_port = px_srv.sockets[0].getsockname()[1]

        # 1) chatgpt.com (NOT in H1_TO_H2_HOSTS) must NOT call client_prefers_passthrough
        r1, w1 = await asyncio.open_connection("127.0.0.1", px_port)
        w1.write(b"CONNECT chatgpt.com:443 HTTP/1.1\r\nHost: chatgpt.com:443\r\n\r\n")
        await w1.drain()
        assert b"200 Connection Established" in await r1.readuntil(b"\r\n\r\n")
        w1.close()
        await asyncio.sleep(0.05)
        assert calls == []

        # 2) daily-cloudcode-pa.googleapis.com (IN H1_TO_H2_HOSTS) runs in a thread off the event loop
        r2, w2 = await asyncio.open_connection("127.0.0.1", px_port)
        w2.write(
            b"CONNECT daily-cloudcode-pa.googleapis.com:443 HTTP/1.1\r\n"
            b"Host: daily-cloudcode-pa.googleapis.com:443\r\n\r\n"
        )
        await w2.drain()
        assert b"200 Connection Established" in await r2.readuntil(b"\r\n\r\n")
        w2.write(b"ping")
        await w2.drain()
        w2.close()
        await asyncio.sleep(0.05)

        px_srv.close()
        up_srv.close()

    asyncio.run(_run())
    assert len(calls) == 1
    assert calls[0][0] != loop_tid["main"]


# --- 5. CancelledError, child task cleanup, server shutdown ---


def test_handle_client_propagates_cancelled_error_and_server_cleans_pid(tmp_path: Path) -> None:
    pid_path = tmp_path / "state" / "h2-keepalive.pid"
    tls_dir = tmp_path / "tls"

    async def _run() -> None:
        # 1) handle_client must propagate CancelledError (not swallow it)
        proxy = h2_mod.H2KeepaliveProxy(tls_dir, install_system_trust=False, require_system_trust=False)
        started = asyncio.Event()
        handler_tasks: list[asyncio.Task[object]] = []

        async def _cb(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
            t = asyncio.current_task()
            assert t is not None
            handler_tasks.append(t)
            started.set()
            await proxy.handle_client(r, w)

        srv = await asyncio.start_server(_cb, "127.0.0.1", 0)
        port = srv.sockets[0].getsockname()[1]
        _cr, cw = await asyncio.open_connection("127.0.0.1", port)
        await asyncio.wait_for(started.wait(), timeout=2.0)
        await asyncio.sleep(0.02)
        handler_tasks[0].cancel()
        with pytest.raises(asyncio.CancelledError):
            await handler_tasks[0]
        assert handler_tasks[0] not in proxy._active_tasks
        cw.close()
        srv.close()

        # 2) run_h2_keepalive_server cancels active tasks and unlinks pid_path on shutdown
        sock_probe = await asyncio.start_server(lambda _r, w: w.close(), "127.0.0.1", 0)
        free_port = sock_probe.sockets[0].getsockname()[1]
        sock_probe.close()
        await sock_probe.wait_closed()

        server_task = asyncio.create_task(
            h2_mod.run_h2_keepalive_server(
                tls_dir,
                listen_port=free_port,
                pid_path=pid_path,
                install_system_trust=False,
                require_system_trust=False,
            )
        )
        for _ in range(40):
            if pid_path.exists():
                break
            await asyncio.sleep(0.02)
        assert pid_path.exists()

        _cr2, cw2 = await asyncio.open_connection("127.0.0.1", free_port)
        await asyncio.sleep(0.03)
        server_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await server_task
        assert not pid_path.exists()
        cw2.close()

    asyncio.run(_run())


def test_forwarders_cancel_all_child_tasks_on_cancellation() -> None:
    async def _run() -> None:
        hold = asyncio.Event()

        async def _hold_srv(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
            try:
                await hold.wait()
            finally:
                w.close()

        srv = await asyncio.start_server(_hold_srv, "127.0.0.1", 0)
        port = srv.sockets[0].getsockname()[1]

        before_tasks = set(asyncio.all_tasks())
        r1, w1 = await asyncio.open_connection("127.0.0.1", port)
        r2, w2 = await asyncio.open_connection("127.0.0.1", port)
        ws_task = asyncio.create_task(
            h2_mod._forward_ws_frames_with_ping(r1, w1, r2, w2, ping_interval=0.05)
        )
        await asyncio.sleep(0.03)
        ws_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await ws_task
        w1.close()
        w2.close()

        r3, w3 = await asyncio.open_connection("127.0.0.1", port)
        r4, w4 = await asyncio.open_connection("127.0.0.1", port)
        br_task = asyncio.create_task(
            h2_mod._bridge_http1_client_to_h2_upstream(
                r3, w3, r4, w4, target_host="daily-cloudcode-pa.googleapis.com", ping_interval=0.05
            )
        )
        await asyncio.sleep(0.03)
        br_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await br_task
        w3.close()
        w4.close()

        # Also verify _forward_h2_frames_with_ping cancels all child tasks cleanly
        async def _send_preface_srv(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
            try:
                w.write(h2_mod.H2_PREFACE)
                await w.drain()
                await hold.wait()
            finally:
                w.close()

        pref_srv = await asyncio.start_server(_send_preface_srv, "127.0.0.1", 0)
        pref_port = pref_srv.sockets[0].getsockname()[1]
        r5, w5 = await asyncio.open_connection("127.0.0.1", pref_port)
        r6, w6 = await asyncio.open_connection("127.0.0.1", port)
        h2_task = asyncio.create_task(
            h2_mod._forward_h2_frames_with_ping(r5, w5, r6, w6, ping_interval=0.05)
        )
        await asyncio.sleep(0.03)
        h2_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await h2_task
        w5.close()
        w6.close()

        hold.set()
        await asyncio.sleep(0.05)
        leaked = [t for t in asyncio.all_tasks() - before_tasks if not t.done()]
        pref_srv.close()
        srv.close()
        assert leaked == []

    asyncio.run(_run())


# --- 6. WebSocket Close handshake ---


def test_ws_close_handshake_forwards_server_close_reply() -> None:
    async def _run() -> None:
        async def mock_up(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
            hdr = await r.readexactly(2)
            assert (hdr[0] & 0x0F) == 0x08
            plen = hdr[1] & 0x7F
            mask = await r.readexactly(4)
            _payload = bytes(b ^ mask[i & 3] for i, b in enumerate(await r.readexactly(plen)))
            # Wait longer than pinger tick (50ms) before replying Close
            await asyncio.sleep(0.14)
            w.write(b"\x88\x02\x03\xe8")
            await w.drain()
            await asyncio.sleep(0.02)
            w.close()

        up_srv = await asyncio.start_server(mock_up, "127.0.0.1", 0)
        up_port = up_srv.sockets[0].getsockname()[1]

        async def px_handler(cr: asyncio.StreamReader, cw: asyncio.StreamWriter) -> None:
            ur, uw = await asyncio.open_connection("127.0.0.1", up_port)
            try:
                await h2_mod._forward_ws_frames_with_ping(
                    cr, cw, ur, uw, ping_interval=0.05, idle_ping_interval=0.05
                )
            finally:
                cw.close()
                uw.close()

        px_srv = await asyncio.start_server(px_handler, "127.0.0.1", 0)
        px_port = px_srv.sockets[0].getsockname()[1]

        cr, cw = await asyncio.open_connection("127.0.0.1", px_port)
        mkey = b"\x11\x22\x33\x44"
        close_body = b"\x03\xe8"
        masked = bytes(b ^ mkey[i & 3] for i, b in enumerate(close_body))
        cw.write(bytes([0x88, 0x80 | len(close_body)]) + mkey + masked)
        await cw.drain()

        reply = await asyncio.wait_for(cr.readexactly(4), timeout=2.0)
        assert reply == b"\x88\x02\x03\xe8"
        cw.close()
        px_srv.close()
        up_srv.close()

    asyncio.run(_run())


# --- 7. TCP half-close SHUT_WR ---


def test_h1_to_h2_bridge_and_pipe_raw_preserve_tcp_half_close() -> None:
    from h2.config import H2Configuration
    from h2.connection import H2Connection
    from h2.events import StreamEnded

    async def _run() -> None:
        # Part A: _pipe_raw half-close
        async def echo_after_eof(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
            req = await r.read()
            assert req == b"half-close-req"
            w.write(b"half-close-resp")
            await w.drain()
            w.close()

        raw_up = await asyncio.start_server(echo_after_eof, "127.0.0.1", 0)
        raw_up_port = raw_up.sockets[0].getsockname()[1]

        async def raw_px(cr: asyncio.StreamReader, cw: asyncio.StreamWriter) -> None:
            ur, uw = await asyncio.open_connection("127.0.0.1", raw_up_port)
            try:
                await asyncio.gather(h2_mod._pipe_raw(cr, uw), h2_mod._pipe_raw(ur, cw))
            finally:
                cw.close()
                uw.close()

        raw_px_srv = await asyncio.start_server(raw_px, "127.0.0.1", 0)
        raw_px_port = raw_px_srv.sockets[0].getsockname()[1]
        rcr, rcw = await asyncio.open_connection("127.0.0.1", raw_px_port)
        rcw.write(b"half-close-req")
        await rcw.drain()
        rcw.write_eof()
        resp_raw = await asyncio.wait_for(rcr.read(), timeout=2.0)
        assert resp_raw == b"half-close-resp"
        rcw.close()
        raw_px_srv.close()
        raw_up.close()

        # Part B: H1->H2 bridge half-close after complete request body
        async def h2_slow_up(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
            srv = H2Connection(config=H2Configuration(client_side=False, header_encoding=None))
            srv.initiate_connection()
            w.write(srv.data_to_send())
            await w.drain()
            while True:
                raw = await r.read(65536)
                if not raw:
                    break
                for ev in srv.receive_data(raw):
                    if isinstance(ev, StreamEnded):
                        # Wait past several pinger ticks while client_r is at EOF
                        await asyncio.sleep(0.15)
                        body = b"half-close-ok"
                        srv.send_headers(
                            ev.stream_id,
                            [(b":status", b"200"), (b"content-length", str(len(body)).encode())],
                            end_stream=False,
                        )
                        srv.send_data(ev.stream_id, body, end_stream=True)
                out = srv.data_to_send()
                if out:
                    w.write(out)
                    await w.drain()
            w.close()

        h2_srv = await asyncio.start_server(h2_slow_up, "127.0.0.1", 0)
        h2_port = h2_srv.sockets[0].getsockname()[1]

        async def br_px(cr: asyncio.StreamReader, cw: asyncio.StreamWriter) -> None:
            ur, uw = await asyncio.open_connection("127.0.0.1", h2_port)
            try:
                await h2_mod._bridge_http1_client_to_h2_upstream(
                    cr,
                    cw,
                    ur,
                    uw,
                    target_host="daily-cloudcode-pa.googleapis.com",
                    ping_interval=0.03,
                    idle_ping_interval=10.0,
                    sse_keepalive_interval=10.0,
                )
            finally:
                cw.close()
                uw.close()

        br_srv = await asyncio.start_server(br_px, "127.0.0.1", 0)
        br_port = br_srv.sockets[0].getsockname()[1]

        cr, cw = await asyncio.open_connection("127.0.0.1", br_port)
        cw.write(
            b"POST /v1 HTTP/1.1\r\n"
            b"Host: daily-cloudcode-pa.googleapis.com\r\n"
            b"Connection: close\r\n"
            b"Content-Length: 2\r\n\r\n{}"
        )
        await cw.drain()
        cw.write_eof()

        hdr = await asyncio.wait_for(cr.readuntil(b"\r\n\r\n"), timeout=2.0)
        assert hdr.startswith(b"HTTP/1.1 200 OK\r\n")
        body = await asyncio.wait_for(cr.readexactly(13), timeout=2.0)
        assert body == b"half-close-ok"
        cw.close()
        br_srv.close()
        h2_srv.close()

    asyncio.run(_run())


# --- 8. H1->H2 protocol hardening, H2 frame cap, CONNECT parsing, daemon Popen ---


def test_h1_to_h2_gzip_sse_no_heartbeat_and_chunked_cl_and_smuggling_guards() -> None:
    import gzip

    from h2.config import H2Configuration
    from h2.connection import H2Connection
    from h2.events import RequestReceived, StreamEnded

    async def _run() -> None:
        seen_headers: list[list[tuple[bytes, bytes]]] = []
        gz_payload = gzip.compress(b"data: compressed\n\n")

        async def mock_up(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
            srv = H2Connection(config=H2Configuration(client_side=False, header_encoding=None))
            srv.initiate_connection()
            w.write(srv.data_to_send())
            await w.drain()
            srv_lock = asyncio.Lock()

            async def _reply(sid: int) -> None:
                async with srv_lock:
                    srv.send_headers(
                        sid,
                        [
                            (b":status", b"200"),
                            (b"content-type", b"text/event-stream"),
                            (b"content-encoding", b"gzip"),
                        ],
                        end_stream=False,
                    )
                    w.write(srv.data_to_send())
                    await w.drain()
                # Wait long enough for several PING/ACK ticks
                await asyncio.sleep(0.14)
                async with srv_lock:
                    srv.send_data(sid, gz_payload, end_stream=True)
                    w.write(srv.data_to_send())
                    await w.drain()

            bg: list[asyncio.Task[None]] = []
            try:
                while True:
                    raw = await r.read(65536)
                    if not raw:
                        break
                    async with srv_lock:
                        for ev in srv.receive_data(raw):
                            if isinstance(ev, RequestReceived):
                                seen_headers.append(list(ev.headers))
                            elif isinstance(ev, StreamEnded):
                                bg.append(asyncio.create_task(_reply(ev.stream_id)))
                        out = srv.data_to_send()
                        if out:
                            w.write(out)
                            await w.drain()
            finally:
                for t in bg:
                    await asyncio.gather(t, return_exceptions=True)
                w.close()

        up_srv = await asyncio.start_server(mock_up, "127.0.0.1", 0)
        up_port = up_srv.sockets[0].getsockname()[1]

        async def br_px(cr: asyncio.StreamReader, cw: asyncio.StreamWriter) -> None:
            ur, uw = await asyncio.open_connection("127.0.0.1", up_port)
            try:
                await h2_mod._bridge_http1_client_to_h2_upstream(
                    cr,
                    cw,
                    ur,
                    uw,
                    target_host="daily-cloudcode-pa.googleapis.com",
                    ping_interval=0.025,
                    idle_ping_interval=10.0,
                    sse_keepalive_interval=0.04,
                )
            finally:
                cw.close()
                uw.close()

        br_srv = await asyncio.start_server(br_px, "127.0.0.1", 0)
        br_port = br_srv.sockets[0].getsockname()[1]

        # 1) Chunked request with Content-Length -> content-length stripped, gzip SSE body uncorrupted
        cr, cw = await asyncio.open_connection("127.0.0.1", br_port)
        cw.write(
            b"POST /v1 HTTP/1.1\r\n"
            b"Host: daily-cloudcode-pa.googleapis.com\r\n"
            b"Transfer-Encoding: chunked\r\n"
            b"Content-Length: 2\r\n"
            b"Connection: close\r\n\r\n"
            b"2\r\n{}\r\n0\r\n\r\n"
        )
        await cw.drain()
        await cr.readuntil(b"\r\n\r\n")
        body = bytearray()
        while True:
            sz = int((await cr.readline()).strip(), 16)
            if sz == 0:
                await cr.readexactly(2)
                break
            body.extend(await cr.readexactly(sz))
            await cr.readexactly(2)
        assert gzip.decompress(bytes(body)) == b"data: compressed\n\n"
        assert all(k != b"content-length" for k, _ in seen_headers[0])
        cw.close()

        # 2) Conflicting duplicate Content-Length rejected before forwarding to upstream
        prev_count = len(seen_headers)
        cr2, cw2 = await asyncio.open_connection("127.0.0.1", br_port)
        cw2.write(
            b"POST /v1 HTTP/1.1\r\n"
            b"Host: daily-cloudcode-pa.googleapis.com\r\n"
            b"Content-Length: 2\r\n"
            b"Content-Length: 4\r\n\r\n{}"
        )
        await cw2.drain()
        assert await cr2.read() == b""
        assert len(seen_headers) == prev_count
        cw2.close()

        # 3) Pseudo-header in HTTP/1.1 request rejected before forwarding
        cr3, cw3 = await asyncio.open_connection("127.0.0.1", br_port)
        cw3.write(
            b"GET /v1 HTTP/1.1\r\n"
            b"Host: daily-cloudcode-pa.googleapis.com\r\n"
            b":path: /evil\r\n\r\n"
        )
        await cw3.drain()
        assert await cr3.read() == b""
        assert len(seen_headers) == prev_count
        cw3.close()

        # 4) Negative chunk size rejected cleanly
        cr4, cw4 = await asyncio.open_connection("127.0.0.1", br_port)
        cw4.write(
            b"POST /v1 HTTP/1.1\r\n"
            b"Host: daily-cloudcode-pa.googleapis.com\r\n"
            b"Transfer-Encoding: chunked\r\n\r\n"
            b"-5\r\nhello\r\n0\r\n\r\n"
        )
        await cw4.drain()
        assert await cr4.read() == b""
        cw4.close()

        br_srv.close()
        up_srv.close()

    asyncio.run(_run())


def test_forward_h2_caps_oversized_frame_payload() -> None:
    async def _run() -> None:
        upstream_received = bytearray()

        async def mock_up(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
            upstream_received.extend(await r.read())
            w.close()

        up_srv = await asyncio.start_server(mock_up, "127.0.0.1", 0)
        up_port = up_srv.sockets[0].getsockname()[1]

        async def px(cr: asyncio.StreamReader, cw: asyncio.StreamWriter) -> None:
            ur, uw = await asyncio.open_connection("127.0.0.1", up_port)
            try:
                await h2_mod._forward_h2_frames_with_ping(cr, cw, ur, uw, ping_interval=1.0)
            finally:
                cw.close()
                uw.close()

        px_srv = await asyncio.start_server(px, "127.0.0.1", 0)
        px_port = px_srv.sockets[0].getsockname()[1]

        cr, cw = await asyncio.open_connection("127.0.0.1", px_port)
        oversize = h2_mod.H2_MAX_FRAME_PAYLOAD_BYTES + 1
        bad_hdr = oversize.to_bytes(3, "big") + b"\x00\x00" + (1).to_bytes(4, "big")
        cw.write(h2_mod.H2_PREFACE + bad_hdr)
        await cw.drain()
        # Bridge must close promptly without waiting for 1MB+ of payload
        assert await asyncio.wait_for(cr.read(), timeout=1.0) == b""
        cw.close()
        px_srv.close()
        up_srv.close()

    asyncio.run(_run())


def test_connect_authority_validation_and_daemon_popen_hardening(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert h2_mod._parse_connect_authority("[::1]:443") == ("::1", 443)
    assert h2_mod._parse_connect_authority("[2001:db8::1]:8443") == ("2001:db8::1", 8443)
    assert h2_mod._parse_connect_authority("chatgpt.com:443") == ("chatgpt.com", 443)
    assert h2_mod._parse_connect_authority("chatgpt.com") == ("chatgpt.com", 443)

    for bad in (
        "example.com:0",
        "example.com:65536",
        "example.com:abc",
        "::1:443",
        "[::1:443",
        f"{'a' * 256}.com:443",
        "",
    ):
        with pytest.raises(ValueError):
            h2_mod._parse_connect_authority(bad)

    captured_kwargs: dict[str, object] = {}

    def _fake_popen(cmd, **kwargs):
        captured_kwargs["cmd"] = cmd
        captured_kwargs.update(kwargs)
        return MagicMock()

    running_calls = iter([False, True])
    monkeypatch.setattr(h2_mod, "is_h2_keepalive_running", lambda *_a, **_kw: next(running_calls))
    monkeypatch.setattr(h2_mod.subprocess, "Popen", _fake_popen)
    monkeypatch.setattr(h2_mod.time, "sleep", lambda _s: None)

    assert h2_mod.ensure_h2_keepalive_daemon(
        tmp_path / "data", tmp_path / "state", listen_port=7895, upstream_port=7890
    )
    assert captured_kwargs["stdin"] is subprocess.DEVNULL
    assert "listen_port=7895, upstream_port=7890" in captured_kwargs["cmd"][2]  # type: ignore[index]

    assert h2_mod.ensure_h2_keepalive_daemon(tmp_path / "data", tmp_path / "state", listen_port=0) is False


# --- 9. TUN active-stream protection & 4-tuple socket map ---


def test_socket_idle_ms_4tuple_and_tun_missing_port_merge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert not hasattr(ai_watch_mod, "_LAST_SOCKET_QUEUE_MAP")
    ss_out = (
        "State Recv-Q Send-Q Local Address:Port Peer Address:Port Process\n"
        "ESTAB 4096   1024   127.0.0.1:7890     127.0.0.1:50123\n"
        "      cubic lastsnd:1500 lastrcv:2200\n"
    )
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *_a, **_kw: SimpleNamespace(returncode=0, stdout=ss_out),
    )
    parsed = ai_watch_mod.get_local_proxy_socket_idle_ms(7890)
    assert parsed == {"50123": (1500, 2200, 4096, 1024)}
    assert ai_watch_mod._unpack_socket_timings(parsed["50123"]) == (1500, 2200, 4096, 1024)
    assert ai_watch_mod._unpack_socket_timings((900, 1200)) == (900, 1200, 0, 0)

    # TUN mode: :7890 idle_map is non-empty (has port 50123), while TUN stream uses 61111
    paths = _paths(tmp_path)
    monkeypatch.setattr(
        ai_watch_mod,
        "get_local_proxy_socket_idle_ms",
        lambda _p=7890: {"50123": (50_000, 50_000, 0, 0)},
    )
    tun_conns = [
        {
            "id": "tun-active-upload",
            "chains": ["US-01", "AI-MANUAL"],
            "upload": 200_000,
            "download": 1_000,
            "metadata": {"host": "chatgpt.com", "sourcePort": "61111"},
        }
    ]
    api = SimpleNamespace(get_connections=lambda: {"connections": tun_conns})
    assert ai_watch_mod.has_active_ai_transfer(paths, api, "AI-MANUAL", "US-01") is True


# --- 10. _pick_next, on_soft_unhealthy, reap_stale_agy_processes, ai_watch edge cases ---


def test_pick_next_non_iepl_subscription_sorts_and_recovers_alive_cooled(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    svc = ai_watch_mod.AIWatchService(paths)
    now = 20_000.0
    groups = {
        "AI-MANUAL": ProxyGroup("AI-MANUAL", "fallback", "US-01", ["US-01", "JP-01", "SG-01"]),
        "AI-GEMINI": ProxyGroup("AI-GEMINI", "fallback", "JP-01", ["JP-01", "SG-01"]),
        "US-01": ProxyGroup("US-01", "ss", "-", [], delay=120, alive=True),
        "JP-01": ProxyGroup("JP-01", "ss", "-", [], delay=100, alive=True),
        "SG-01": ProxyGroup("SG-01", "ss", "-", [], delay=140, alive=True),
    }
    # Without IEPL nodes, AI-MANUAL still avoids colliding with AI-GEMINI's active JP-01
    assert svc._pick_next(groups, groups["AI-MANUAL"], "US-01", {}, now) == "SG-01"
    # When all candidates are still in cooldown, holds current node (None) instead of flapping
    picked = svc._pick_next(
        groups,
        groups["AI-MANUAL"],
        "US-01",
        {"JP-01": now - 300.0, "SG-01": now - 60.0},
        now,
    )
    assert picked is None


def test_on_soft_unhealthy_gated_by_group_busy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _paths(tmp_path)
    groups = {
        "AI-MANUAL": ProxyGroup("AI-MANUAL", "fallback", "KR-01", ["KR-01", "US-01"]),
        "AI-GEMINI": ProxyGroup("AI-GEMINI", "fallback", "KR-01", ["KR-01", "GB-01"]),
        "KR-01": ProxyGroup("KR-01", "ss", "-", [], delay=120, alive=True),
        "US-01": ProxyGroup("US-01", "ss", "-", [], delay=140, alive=True),
        "GB-01": ProxyGroup("GB-01", "ss", "-", [], delay=150, alive=True),
    }
    switches: list[tuple[str, str]] = []

    class _BusyQuery:
        def __init__(self) -> None:
            self.api = SimpleNamespace(
                get_groups=lambda: groups,
                get_connections=lambda: {"connections": []},
            )

        def switch_group(self, g: str, t: str, **_kw) -> None:
            switches.append((g, t))

    monkeypatch.setattr(ai_watch_mod, "has_active_ai_transfer", lambda _p, _a, grp, _cur=None: grp == "AI-MANUAL")
    svc = ai_watch_mod.AIWatchService(paths, agy_log_dir=tmp_path / "no-agy", codex_log_dir=tmp_path / "no-codex")
    svc.query = _BusyQuery()  # type: ignore[assignment]
    # Note: keep default ai_target_verdict reference so on_soft_unhealthy branch is evaluated
    monkeypatch.setattr(svc, "_probe_group_verdict", lambda _g: (ai_watch_mod.PROBE_OK, "ok"))

    rep = svc.watch()
    manual = next(r for r in rep.results if r.group == "AI-MANUAL")
    assert manual.switched is False
    assert switches == []


def test_reap_stale_agy_processes_fails_closed_and_codex_uri_special_chars(tmp_path: Path) -> None:
    proc_root = tmp_path / "proc"
    now = 1_800_000_000.0
    old_exe = "/root/.local/bin/agy.1792306061959817128.old"

    def _mk(pid: int, *, with_uid: bool = True, fd0_target: str | Path | None = None) -> None:
        pdir = proc_root / str(pid)
        pdir.mkdir(parents=True, exist_ok=True)
        status = "Name:\tagy\nPPid:\t1\n" + ("Uid:\t0\t0\t0\t0\n" if with_uid else "")
        (pdir / "status").write_text(status, encoding="utf-8")
        (pdir / "cmdline").write_text("agy", encoding="utf-8")
        (pdir / "exe").symlink_to(old_exe)
        if fd0_target is not None:
            (pdir / "fd").mkdir()
            (pdir / "fd" / "0").symlink_to(fd0_target)

    # 10: missing Uid; 20: missing fd/0; 30: /dev/null; 40: pipe; 50: socket -> ALL must be skipped
    _mk(10, with_uid=False, fd0_target=tmp_path / "pts10")
    _mk(20, with_uid=True, fd0_target=None)
    _mk(30, with_uid=True, fd0_target="/dev/null")
    _mk(40, with_uid=True, fd0_target="pipe:[12345]")
    _mk(50, with_uid=True, fd0_target="socket:[67890]")

    killed: list[int] = []
    reaped = ai_watch_mod.reap_stale_agy_processes(
        proc_root,
        now=now,
        idle_seconds=1800,
        kill_fn=lambda pid, _sig: killed.append(pid),
        current_uid=0,
        self_pid=9999,
    )
    assert reaped == [] and killed == []

    # Codex SQLite path with '#', '?', '%' opens cleanly via as_uri()
    codex_dir = tmp_path / "codex#?%dir"
    codex_dir.mkdir()
    db_file = codex_dir / "logs_1.sqlite"
    now_epoch = int(time.time())
    conn = sqlite3.connect(str(db_file))
    conn.execute(
        "CREATE TABLE logs ("
        "id INTEGER PRIMARY KEY, ts INTEGER, ts_nanos INTEGER, "
        "level TEXT, target TEXT, feedback_log_body TEXT)"
    )
    conn.execute(
        "INSERT INTO logs VALUES (1, ?, 0, 'WARN', 'codex_core::responses_retry', "
        "'retries=3 sampling_error=stream disconnected before completion: error sending request')",
        (now_epoch - 2,),
    )
    conn.commit()
    conn.close()
    hit, _ts, reason = ai_watch_mod.detect_recent_codex_retry_failure(codex_dir, now=float(now_epoch))
    assert hit is True and "error sending request" in reason


def test_ai_watch_prunes_expired_cooldowns(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    paths = _paths(tmp_path)
    now = time.time()
    ai_watch_mod._save_state(
        paths,
        {
            "AI-MANUAL": {
                "fail_node": None,
                "fail_count": 0,
                "cooldown": {"EXPIRED-01": now - 9999.0, "ACTIVE-01": now - 10.0},
            }
        },
    )
    groups = {
        "AI-MANUAL": ProxyGroup("AI-MANUAL", "fallback", "US-01", ["US-01", "EXPIRED-01", "ACTIVE-01"]),
        "AI-GEMINI": ProxyGroup("AI-GEMINI", "fallback", "KR-01", ["KR-01"]),
    }
    monkeypatch.setattr(ai_watch_mod, "ai_target_verdict", lambda _p, _u: ai_watch_mod.PROBE_OK)
    svc = ai_watch_mod.AIWatchService(paths, agy_log_dir=tmp_path / "no-agy", codex_log_dir=tmp_path / "no-codex")
    svc.query = SimpleNamespace(api=SimpleNamespace(get_groups=lambda: groups))  # type: ignore[assignment]
    svc.watch()
    saved = ai_watch_mod._load_state(paths)
    assert "EXPIRED-01" not in saved["AI-MANUAL"]["cooldown"]
    assert "ACTIVE-01" in saved["AI-MANUAL"]["cooldown"]


# --- 11. probe, probe_history, rebalance, ban, ipcheck ---


def test_probe_fragmented_banner_alias_penalties_zero_delay_and_contextvar(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 1) Fragmented CONNECT banner
    raw_chunks = iter([b"HTTP/1.1 200 ", b"Connection established\r\n", b"\r\n"])
    tls_chunks = iter([b"HTTP/1.1 ", b"401 Unauthorized\r\n\r\n"])

    class _FragRaw:
        def sendall(self, _d: bytes) -> None:
            pass

        def recv(self, _n: int) -> bytes:
            return next(raw_chunks, b"")

        def close(self) -> None:
            pass

    class _FragTLS:
        def settimeout(self, _t: float) -> None:
            pass

        def sendall(self, _d: bytes) -> None:
            pass

        def recv(self, _n: int) -> bytes:
            return next(tls_chunks, b"")

        def close(self) -> None:
            pass

    monkeypatch.setattr(probe_mod.socket, "create_connection", lambda *_a, **_k: _FragRaw())
    monkeypatch.setattr(
        probe_mod.ssl,
        "create_default_context",
        lambda: SimpleNamespace(wrap_socket=lambda _s, server_hostname=None: _FragTLS()),
    )
    ok, reason = probe_mod.simulate_gemini_stream_hold(hold_seconds=0.0)
    assert ok is True and "401" in reason

    # 2) History penalties loaded AFTER alias resolution + delay <= 0 treated as failure
    paths = _paths(tmp_path)
    hist_path = probe_history_file(paths)
    record_probe_history(
        hist_path,
        profile="codex",
        strategy_name="conservative",
        group="AI-MANUAL",
        url=probe_mod.PROFILES["codex"]["url"],
        rounds=5,
        timeout_ms=8000,
        current="N1",
        current_stable=False,
        current_reason="fail",
        best="N2",
        stable=True,
        reason="ok",
        switch_requested=False,
        switched=False,
        skip_reason="",
        nodes=[{"name": "N1", "success": 1, "failures": 4}],
    )
    svc = probe_mod.ProbeService(paths)
    svc.api = SimpleNamespace(  # type: ignore[assignment]
        get_groups=lambda: {
            "AI-MANUAL": ProxyGroup("AI-MANUAL", "fallback", "N1", ["N1", "N2", "NZERO"]),
        },
        delay_test=lambda name, *_a, **_kw: {"delay": 0 if name == "NZERO" else 120},
    )
    rep = svc.probe(group="ai", profile="codex", rounds=1)
    assert rep.group == "AI-MANUAL"
    by_name = {s.name: s for s in rep.summaries}
    assert by_name["N1"].history_penalty_ms > 0
    assert by_name["NZERO"].success_count == 0 and by_name["NZERO"].failures == 1

    # 3) ContextVar isolation across threads
    p_a = _paths(tmp_path / "a")
    p_b = _paths(tmp_path / "b")
    observed: dict[str, AppPaths | None] = {}

    def _worker() -> None:
        with probe_mod.probe_paths_context(p_b):
            time.sleep(0.03)
            observed["b"] = probe_mod._ACTIVE_PATHS.get()

    with probe_mod.probe_paths_context(p_a):
        t = threading.Thread(target=_worker)
        t.start()
        time.sleep(0.01)
        observed["a"] = probe_mod._ACTIVE_PATHS.get()
        t.join()
    assert observed["a"] == p_a and observed["b"] == p_b


def test_probe_history_reads_max_history_lines(tmp_path: Path) -> None:
    hist_path = tmp_path / "probe_history.jsonl"
    record_probe_history(
        hist_path,
        profile="codex",
        strategy_name="conservative",
        group="AI-MANUAL",
        url="https://chatgpt.com",
        rounds=5,
        timeout_ms=8000,
        current="N1",
        current_stable=False,
        current_reason="fail",
        best="N2",
        stable=True,
        reason="ok",
        switch_requested=False,
        switched=False,
        skip_reason="",
        nodes=[{"name": "N1", "success": 0, "failures": 5}],
    )
    # Append 100 entries for another profile (> old maxlen=80, <= MAX_HISTORY_LINES=200)
    with hist_path.open("a", encoding="utf-8") as fh:
        for _ in range(100):
            fh.write(
                json.dumps(
                    {
                        "group": "AI-GEMINI",
                        "profile": "gemini",
                        "url": "https://generativelanguage.googleapis.com",
                        "rounds": 5,
                        "nodes": [],
                    }
                )
                + "\n"
            )
    assert MAX_HISTORY_LINES == 200
    penalties = load_history_penalties(hist_path, "AI-MANUAL", "codex", "https://chatgpt.com", 5)
    assert penalties.get("N1", 0) > 0


def test_rebalance_creates_watch_state_and_avoids_double_get_groups(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _paths(tmp_path)
    assert not ai_watch_state_file(paths).exists()

    class _FakeProbe:
        def __init__(self, _p) -> None:
            pass

        def probe(self, **kwargs):
            return SimpleNamespace(summaries=[probe_mod.ProbeSummary("US-01", (100, 110), failures=0)])

    monkeypatch.setattr(rebalance_mod, "ProbeService", _FakeProbe)
    monkeypatch.setattr(
        rebalance_mod,
        "RuntimeBackend",
        lambda _p: SimpleNamespace(render_runtime=lambda: tmp_path / "rt.yaml"),
    )
    monkeypatch.setattr(rebalance_mod, "ProcessBackend", lambda _p: SimpleNamespace(is_running=lambda: False))

    rebalance_mod.RebalanceService(paths).rebalance()
    assert ai_watch_state_file(paths).exists()
    ws = json.loads(ai_watch_state_file(paths).read_text(encoding="utf-8"))
    assert ws["last_rebalance_ts"] > 0 and ws["rebalance_runs"] == 1
    assert ai_manual_order_file(paths).exists()

    # Verify _verify_gemini_geo_eligibility calls get_groups() only once
    get_groups_calls = 0

    class _CountingQuery:
        def __init__(self, _p) -> None:
            def _gg():
                nonlocal get_groups_calls
                get_groups_calls += 1
                return {"AI-GEMINI": ProxyGroup("AI-GEMINI", "fallback", "DE-01", ["DE-01"])}

            self.api = SimpleNamespace(get_groups=_gg)

    monkeypatch.setattr(rebalance_mod, "QueryService", _CountingQuery)
    monkeypatch.setattr(rebalance_mod, "ai_target_verdict", lambda *_a, **_kw: ai_watch_mod.PROBE_OK)
    rebalance_mod.RebalanceService(paths)._verify_gemini_geo_eligibility(
        ["DE-01"], ai_gemini_order_file(paths), switched_to=None
    )
    assert get_groups_calls == 1


def test_ban_empty_normalization_guard_and_invalid_hours(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    assert matches_ban_pattern("🇺🇸US 01", "|") is False
    assert matches_ban_pattern("🇺🇸US 01", "丨") is False

    add_ban(paths, "us01", hours=2.0, reason="test")
    assert remove_ban(paths, "|") == []
    assert len(load_active_bans(paths)) == 1

    for bad_hours in (0, -1, -0.5, float("nan"), float("inf"), float("-inf")):
        with pytest.raises(ValueError):
            add_ban(paths, "jp01", hours=bad_hours)


def test_ipcheck_hardening_scalars_non_dict_bound_and_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 1) Malformed contributor scalars & non-dict nested fields
    report = ipcheck_mod.parse_report(
        {
            "geo": "not-a-dict",
            "risk": True,
            "riskBreakdown": {
                "contributors": [
                    {"source": "bad-src", "risk": "nope", "weight": "nan"},
                    {"source": "ok-src", "risk": "42", "weight": "0.75"},
                ]
            },
            "signals": "not-a-list",
            "intel": {"datacenter": ["not-a-dict"]},
        }
    )
    assert report.risk is None
    assert report.signals == []
    assert [(s.source, s.risk, s.weight) for s in report.sources] == [
        ("bad-src", 0, 0.0),
        ("ok-src", 42, 0.75),
    ]

    with pytest.raises(ipcheck_mod.IpCheckError):
        ipcheck_mod.parse_report(["not-a-dict"])  # type: ignore[arg-type]

    # 2) fetch_report bounds read(1_048_576), validates dict, and catches HTTPException / OSError / UnicodeDecodeError
    svc = ipcheck_mod.IpCheckService(_paths(tmp_path))
    read_limits: list[int | None] = []

    class _Resp:
        def __init__(self, body: bytes) -> None:
            self._body = body

        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

        def read(self, amt: int | None = None) -> bytes:
            read_limits.append(amt)
            return self._body if amt is None else self._body[:amt]

    monkeypatch.setattr(
        ipcheck_mod,
        "build_opener",
        lambda *_a: SimpleNamespace(open=lambda _r, timeout=None: _Resp(b'{"risk": 10}')),
    )
    assert svc.fetch_report(None, 5) == {"risk": 10}
    assert read_limits == [1_048_576]

    monkeypatch.setattr(
        ipcheck_mod,
        "build_opener",
        lambda *_a: SimpleNamespace(open=lambda _r, timeout=None: _Resp(b"[1, 2, 3]")),
    )
    with pytest.raises(ipcheck_mod.IpCheckError):
        svc.fetch_report(None, 5)

    for exc in (http.client.IncompleteRead(b""), OSError("reset"), UnicodeDecodeError("utf-8", b"\xff", 0, 1, "bad")):
        monkeypatch.setattr(
            ipcheck_mod,
            "build_opener",
            lambda *_a, _e=exc: SimpleNamespace(open=lambda _r, timeout=None: (_ for _ in ()).throw(_e)),
        )
        with pytest.raises(ipcheck_mod.IpCheckError):
            svc.fetch_report(None, 5)
