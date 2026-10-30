from __future__ import annotations

import asyncio
import os
import re
import shutil
import ssl
import subprocess
import time
from pathlib import Path

DEFAULT_KEEPALIVE_HOST = "127.0.0.1"
DEFAULT_KEEPALIVE_PORT = 7895
DEFAULT_UPSTREAM_PROXY_HOST = "127.0.0.1"
DEFAULT_UPSTREAM_PROXY_PORT = 7890
CLOUDCODE_HOST = "daily-cloudcode-pa.googleapis.com"
H2_PREFACE = b"PRI * HTTP/2.0\r\n\r\nSM\r\n\r\n"
PING_INTERVAL_SEC = 4.5

_SS_AGY_RE = re.compile(r'users:\(\("agy[^"]*",pid=(\d+),')
_SS_LASTSND_RE = re.compile(r"\blastsnd:(\d+)\b")


def ensure_local_ca_and_leaf_cert(
    tls_dir: Path,
    *,
    install_system_trust: bool = True,
) -> tuple[Path, Path, Path, float]:
    """确保本地 H2 保活网关的 Root CA 与 daily-cloudcode-pa.googleapis.com 叶子证书就绪。

    返回 `(ca_cert_path, leaf_cert_path, leaf_key_path, trust_installed_mtime)`。
    """
    tls_dir.mkdir(parents=True, exist_ok=True)
    ca_key = tls_dir / "cproxy-h2-ca.key"
    ca_crt = tls_dir / "cproxy-h2-ca.crt"
    leaf_key = tls_dir / "cloudcode.key"
    leaf_crt = tls_dir / "cloudcode.crt"
    trust_marker = tls_dir / "ca-trust.installed"

    if not ca_crt.exists() or not ca_key.exists():
        subprocess.run(
            [
                "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                "-keyout", str(ca_key), "-out", str(ca_crt),
                "-days", "3650", "-subj", "/CN=cproxy Local H2 Keepalive CA",
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    if not leaf_crt.exists() or not leaf_key.exists():
        csr_path = tls_dir / "cloudcode.csr"
        ext_path = tls_dir / "cloudcode.ext"
        ext_path.write_text(
            "authorityKeyIdentifier=keyid,issuer\n"
            "basicConstraints=CA:FALSE\n"
            "keyUsage = digitalSignature, keyEncipherment\n"
            "extendedKeyUsage = serverAuth\n"
            f"subjectAltName = DNS:{CLOUDCODE_HOST},DNS:*.googleapis.com\n",
            encoding="utf-8",
        )
        subprocess.run(
            [
                "openssl", "req", "-newkey", "rsa:2048", "-nodes",
                "-keyout", str(leaf_key), "-out", str(csr_path),
                "-subj", f"/CN={CLOUDCODE_HOST}",
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        subprocess.run(
            [
                "openssl", "x509", "-req", "-in", str(csr_path),
                "-CA", str(ca_crt), "-CAkey", str(ca_key), "-CAcreateserial",
                "-out", str(leaf_crt), "-days", "1095", "-extfile", str(ext_path),
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    if install_system_trust and os.geteuid() == 0:
        anchor_dir = Path("/etc/pki/ca-trust/source/anchors")
        anchor_file = anchor_dir / "cproxy-h2-ca.crt"
        if anchor_dir.is_dir():
            need_update = (
                not anchor_file.exists()
                or anchor_file.read_bytes() != ca_crt.read_bytes()
            )
            if need_update:
                shutil.copyfile(ca_crt, anchor_file)
                subprocess.run(
                    ["update-ca-trust"],
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                trust_marker.write_text(str(time.time()), encoding="utf-8")
            elif not trust_marker.exists():
                trust_marker.write_text(str(anchor_file.stat().st_mtime), encoding="utf-8")

    trust_mtime = trust_marker.stat().st_mtime if trust_marker.exists() else ca_crt.stat().st_mtime
    return ca_crt, leaf_crt, leaf_key, trust_mtime


def _proc_start_time(pid: int) -> float:
    try:
        stat_fields = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8", errors="replace").rsplit(")", 1)[1].split()
        starttime_ticks = int(stat_fields[19])
        clk_tck = os.sysconf(os.sysconf_names["SC_CLK_TCK"])
        for line in Path("/proc/stat").read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("btime "):
                return int(line.split()[1]) + (starttime_ticks / clk_tck)
    except Exception:
        pass
    return os.stat(f"/proc/{pid}").st_mtime


def client_prefers_passthrough(
    trust_installed_mtime: float,
    mixed_port: int = DEFAULT_UPSTREAM_PROXY_PORT,
) -> bool:
    """检查当前刚向 mihomo 发起连接的 agy 进程是否启动于 CA 信任安装之前。

    Go 标准库 `crypto/x509.systemRootsPool()` 使用 `sync.Once` 在进程首次 TLS 握手时缓存
    系统根证书池；对于 CA 安装前已启动的旧 `agy` 进程，自动走透明 TCP 直通避免报证书未知错误，
    而新启动的 `agy` 进程（或显式配置了 `SSL_CERT_FILE` 的进程）自动启用 TLS 终结与 H2 PING 保活。
    """
    try:
        proc = subprocess.run(
            ["ss", "-tnpi", f"dport = :{mixed_port}"],
            capture_output=True,
            text=True,
            timeout=1.0,
            check=False,
        )
    except Exception:
        return False

    best_pid: int | None = None
    best_lastsnd = 300
    lines = proc.stdout.splitlines()
    for idx, line in enumerate(lines):
        m_pid = _SS_AGY_RE.search(line)
        if not m_pid:
            continue
        pid = int(m_pid.group(1))
        detail = lines[idx + 1] if idx + 1 < len(lines) else ""
        m_snd = _SS_LASTSND_RE.search(detail)
        lastsnd = int(m_snd.group(1)) if m_snd else 0
        if lastsnd <= best_lastsnd:
            best_lastsnd = lastsnd
            best_pid = pid

    if best_pid is None:
        return False
    try:
        environ_bytes = Path(f"/proc/{best_pid}/environ").read_bytes()
        if b"SSL_CERT_FILE=" in environ_bytes:
            return False
        return _proc_start_time(best_pid) < trust_installed_mtime
    except OSError:
        return False


async def _pipe_raw(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while True:
            chunk = await reader.read(65536)
            if not chunk:
                break
            writer.write(chunk)
            await writer.drain()
    except Exception:
        pass
    finally:
        try:
            writer.close()
        except Exception:
            pass


async def _forward_h2_frames_with_ping(
    client_r: asyncio.StreamReader,
    client_w: asyncio.StreamWriter,
    upstream_r: asyncio.StreamReader,
    upstream_w: asyncio.StreamWriter,
    *,
    ping_interval: float = PING_INTERVAL_SEC,
) -> None:
    """在 HTTP/2 帧边界上透明转发双向流量，并在存在未完结流 (active_streams > 0) 且静默达到阈值时
    自动向远端发送 17 字节 HTTP/2 PING 帧，彻底阻断上游 VLESS 80s connIdle 断流。
    """
    preface = await client_r.readexactly(len(H2_PREFACE))
    if preface != H2_PREFACE:
        raise ValueError("Invalid HTTP/2 client preface")
    upstream_w.write(preface)
    await upstream_w.drain()

    upstream_lock = asyncio.Lock()
    client_lock = asyncio.Lock()
    active_streams: set[int] = set()
    state = {"last_io": time.monotonic(), "closed": False}

    async def c2u() -> None:
        try:
            while True:
                hdr = await client_r.readexactly(9)
                length = int.from_bytes(hdr[0:3], "big")
                ftype = hdr[3]
                stream_id = int.from_bytes(hdr[5:9], "big") & 0x7FFFFFFF
                payload = await client_r.readexactly(length) if length else b""
                if stream_id > 0:
                    if ftype == 0x01:  # HEADERS
                        active_streams.add(stream_id)
                    elif ftype == 0x03:  # RST_STREAM
                        active_streams.discard(stream_id)
                async with upstream_lock:
                    upstream_w.write(hdr + payload)
                    await upstream_w.drain()
                state["last_io"] = time.monotonic()
        finally:
            state["closed"] = True

    async def u2c() -> None:
        try:
            while True:
                hdr = await upstream_r.readexactly(9)
                length = int.from_bytes(hdr[0:3], "big")
                ftype = hdr[3]
                flags = hdr[4]
                stream_id = int.from_bytes(hdr[5:9], "big") & 0x7FFFFFFF
                payload = await upstream_r.readexactly(length) if length else b""
                if stream_id > 0:
                    if ftype in (0x00, 0x01) and (flags & 0x01):  # END_STREAM
                        active_streams.discard(stream_id)
                    elif ftype == 0x03:  # RST_STREAM
                        active_streams.discard(stream_id)
                elif ftype == 0x06 and (flags & 0x01) and payload.startswith(b"cproxy"):
                    state["last_io"] = time.monotonic()
                    continue
                async with client_lock:
                    client_w.write(hdr + payload)
                    await client_w.drain()
                state["last_io"] = time.monotonic()
        finally:
            state["closed"] = True

    async def pinger() -> None:
        seq = 0
        tick = min(1.0, max(0.05, ping_interval / 4.0))
        try:
            while not state["closed"]:
                await asyncio.sleep(tick)
                if state["closed"] or not active_streams:
                    continue
                if (time.monotonic() - state["last_io"]) >= ping_interval:
                    seq = (seq + 1) % 100
                    ping_frame = b"\x00\x00\x08\x06\x00\x00\x00\x00\x00" + f"cproxy{seq:02d}".encode()
                    async with upstream_lock:
                        upstream_w.write(ping_frame)
                        await upstream_w.drain()
                    state["last_io"] = time.monotonic()
        except Exception:
            pass

    tasks = [
        asyncio.create_task(c2u()),
        asyncio.create_task(u2c()),
        asyncio.create_task(pinger()),
    ]
    _done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    for t in pending:
        t.cancel()


async def _connect_upstream_socks5(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    target_host: str,
    target_port: int,
) -> bool:
    """通过 SOCKS5 协议向上游 mihomo mixed-port 发起建连（以区分于客户端 HTTP CONNECT 入站）。"""
    writer.write(b"\x05\x01\x00")
    await writer.drain()
    auth_resp = await asyncio.wait_for(reader.readexactly(2), timeout=10.0)
    if auth_resp != b"\x05\x00":
        return False
    host_bytes = target_host.encode("idna")
    req = b"\x05\x01\x00\x03" + bytes([len(host_bytes)]) + host_bytes + target_port.to_bytes(2, "big")
    writer.write(req)
    await writer.drain()
    hdr = await asyncio.wait_for(reader.readexactly(4), timeout=15.0)
    if hdr[1] != 0x00:
        return False
    atyp = hdr[3]
    if atyp == 0x01:
        await asyncio.wait_for(reader.readexactly(6), timeout=5.0)
    elif atyp == 0x03:
        dlen = (await asyncio.wait_for(reader.readexactly(1), timeout=5.0))[0]
        await asyncio.wait_for(reader.readexactly(dlen + 2), timeout=5.0)
    elif atyp == 0x04:
        await asyncio.wait_for(reader.readexactly(18), timeout=5.0)
    else:
        return False
    return True


class H2KeepaliveProxy:
    def __init__(
        self,
        tls_dir: Path,
        *,
        listen_host: str = DEFAULT_KEEPALIVE_HOST,
        listen_port: int = DEFAULT_KEEPALIVE_PORT,
        upstream_host: str = DEFAULT_UPSTREAM_PROXY_HOST,
        upstream_port: int = DEFAULT_UPSTREAM_PROXY_PORT,
        upstream_protocol: str = "socks5",
        ping_interval: float = PING_INTERVAL_SEC,
        install_system_trust: bool = True,
    ) -> None:
        self.tls_dir = tls_dir
        self.listen_host = listen_host
        self.listen_port = listen_port
        self.upstream_host = upstream_host
        self.upstream_port = upstream_port
        self.upstream_protocol = upstream_protocol
        self.ping_interval = ping_interval
        ca_crt, leaf_crt, leaf_key, trust_mtime = ensure_local_ca_and_leaf_cert(
            tls_dir, install_system_trust=install_system_trust
        )
        self.ca_cert_path = ca_crt
        self.trust_installed_mtime = trust_mtime
        self.server_ssl = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.server_ssl.load_cert_chain(certfile=str(leaf_crt), keyfile=str(leaf_key))
        self.server_ssl.set_alpn_protocols(["h2", "http/1.1"])
        self.client_ssl = ssl.create_default_context()
        self.client_ssl.set_alpn_protocols(["h2", "http/1.1"])
        self.client_ssl_h1 = ssl.create_default_context()
        self.client_ssl_h1.set_alpn_protocols(["http/1.1"])

    async def handle_client(
        self,
        client_r: asyncio.StreamReader,
        client_w: asyncio.StreamWriter,
    ) -> None:
        upstream_w: asyncio.StreamWriter | None = None
        try:
            req_head = await asyncio.wait_for(client_r.readuntil(b"\r\n\r\n"), timeout=10.0)
            first_line = req_head.split(b"\r\n", 1)[0].decode("latin1", errors="replace")
            parts = first_line.split()
            if len(parts) >= 2 and parts[0].upper() in ("GET", "HEAD") and "generate_204" in parts[1]:
                client_w.write(b"HTTP/1.1 204 No Content\r\nContent-Length: 0\r\n\r\n")
                await client_w.drain()
                return
            if len(parts) < 2 or parts[0].upper() != "CONNECT":
                client_w.write(b"HTTP/1.1 405 Method Not Allowed\r\n\r\n")
                await client_w.drain()
                return
            host_port = parts[1]
            target_host, _, port_str = host_port.partition(":")
            target_host = target_host.strip().lower()
            target_port = int(port_str) if port_str.isdigit() else 443

            passthrough = (
                target_host != CLOUDCODE_HOST
                or target_port != 443
                or client_prefers_passthrough(self.trust_installed_mtime, self.upstream_port)
            )

            upstream_r, upstream_w = await asyncio.wait_for(
                asyncio.open_connection(self.upstream_host, self.upstream_port),
                timeout=10.0,
            )
            if self.upstream_protocol == "socks5":
                ok = await _connect_upstream_socks5(upstream_r, upstream_w, target_host, target_port)
                if not ok:
                    client_w.write(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
                    await client_w.drain()
                    return
            else:
                connect_req = (
                    f"CONNECT {target_host}:{target_port} HTTP/1.1\r\n"
                    f"Host: {target_host}:{target_port}\r\n\r\n"
                ).encode("ascii")
                upstream_w.write(connect_req)
                await upstream_w.drain()
                up_resp = await asyncio.wait_for(upstream_r.readuntil(b"\r\n\r\n"), timeout=15.0)
                if b" 200 " not in up_resp.split(b"\r\n", 1)[0]:
                    client_w.write(up_resp)
                    await client_w.drain()
                    return

            client_w.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            await client_w.drain()

            if passthrough:
                await asyncio.gather(
                    _pipe_raw(client_r, upstream_w),
                    _pipe_raw(upstream_r, client_w),
                )
                return

            loop = asyncio.get_running_loop()
            try:
                client_tls_t = await loop.start_tls(
                    client_w.transport,
                    client_w._protocol,
                    self.server_ssl,
                    server_side=True,
                )
            except ssl.SSLError:
                self.trust_installed_mtime = time.time() + 1.0
                return
            client_w._transport = client_tls_t
            client_r._transport = client_tls_t
            client_w._protocol._over_ssl = True

            cli_ssl_obj = client_tls_t.get_extra_info("ssl_object")
            cli_alpn = cli_ssl_obj.selected_alpn_protocol() if cli_ssl_obj else None
            up_ssl_ctx = self.client_ssl if cli_alpn == "h2" else self.client_ssl_h1

            up_tls_t = await loop.start_tls(
                upstream_w.transport,
                upstream_w._protocol,
                up_ssl_ctx,
                server_side=False,
                server_hostname=target_host,
            )
            upstream_w._transport = up_tls_t
            upstream_r._transport = up_tls_t
            upstream_w._protocol._over_ssl = True

            ssl_obj = up_tls_t.get_extra_info("ssl_object")
            alpn = ssl_obj.selected_alpn_protocol() if ssl_obj else None
            if cli_alpn == "h2" and alpn == "h2":
                await _forward_h2_frames_with_ping(
                    client_r,
                    client_w,
                    upstream_r,
                    upstream_w,
                    ping_interval=self.ping_interval,
                )
            else:
                await asyncio.gather(
                    _pipe_raw(client_r, upstream_w),
                    _pipe_raw(upstream_r, client_w),
                )
        except Exception:
            pass
        finally:
            for w in (client_w, upstream_w):
                if w is not None:
                    try:
                        w.close()
                    except Exception:
                        pass


async def run_h2_keepalive_server(
    tls_dir: Path,
    *,
    listen_host: str = DEFAULT_KEEPALIVE_HOST,
    listen_port: int = DEFAULT_KEEPALIVE_PORT,
    upstream_host: str = DEFAULT_UPSTREAM_PROXY_HOST,
    upstream_port: int = DEFAULT_UPSTREAM_PROXY_PORT,
    ping_interval: float = PING_INTERVAL_SEC,
    pid_path: Path | None = None,
) -> None:
    proxy = H2KeepaliveProxy(
        tls_dir,
        listen_host=listen_host,
        listen_port=listen_port,
        upstream_host=upstream_host,
        upstream_port=upstream_port,
        ping_interval=ping_interval,
    )
    server = await asyncio.start_server(proxy.handle_client, listen_host, listen_port)
    if pid_path is not None:
        try:
            pid_path.parent.mkdir(parents=True, exist_ok=True)
            pid_path.write_text(f"{os.getpid()}\n", encoding="utf-8")
        except OSError:
            pass
    async with server:
        await server.serve_forever()


def h2_keepalive_pid_file(state_dir: Path) -> Path:
    return state_dir / "h2-keepalive.pid"


def is_h2_keepalive_running(
    state_dir: Path,
    *,
    host: str = DEFAULT_KEEPALIVE_HOST,
    port: int = DEFAULT_KEEPALIVE_PORT,
) -> bool:
    import socket

    pid_path = h2_keepalive_pid_file(state_dir)
    if not pid_path.exists():
        return False
    try:
        pid = int(pid_path.read_text(encoding="utf-8").strip())
        os.kill(pid, 0)
    except (OSError, ValueError):
        return False
    try:
        with socket.create_connection((host, port), timeout=0.25):
            return True
    except OSError:
        return False


def ensure_h2_keepalive_daemon(
    data_dir: Path,
    state_dir: Path,
    *,
    listen_port: int = DEFAULT_KEEPALIVE_PORT,
    upstream_port: int = DEFAULT_UPSTREAM_PROXY_PORT,
) -> bool:
    """确保本地 H2 PING 保活网关守护进程已在后台监听。"""
    import sys

    if is_h2_keepalive_running(state_dir, port=listen_port):
        return True
    state_dir.mkdir(parents=True, exist_ok=True)
    log_path = state_dir / "h2-keepalive.log"
    pid_path = h2_keepalive_pid_file(state_dir)
    tls_dir = data_dir / "tls"
    code = (
        "import asyncio; from pathlib import Path; "
        "from cproxy.services.h2_keepalive import run_h2_keepalive_server; "
        f"asyncio.run(run_h2_keepalive_server(Path({str(tls_dir)!r}), "
        f"listen_port={listen_port}, upstream_port={upstream_port}, "
        f"pid_path=Path({str(pid_path)!r})))"
    )
    try:
        with log_path.open("a", encoding="utf-8") as log_handle:
            subprocess.Popen(
                [sys.executable, "-c", code],
                stdout=log_handle,
                stderr=log_handle,
                start_new_session=True,
            )
        for _ in range(25):
            time.sleep(0.1)
            if is_h2_keepalive_running(state_dir, port=listen_port):
                return True
    except Exception:
        return False
    return False
