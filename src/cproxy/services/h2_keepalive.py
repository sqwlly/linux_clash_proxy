from __future__ import annotations

import asyncio
import os
import re
import shutil
import ssl
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

DEFAULT_KEEPALIVE_HOST = "127.0.0.1"
DEFAULT_KEEPALIVE_PORT = 7895
DEFAULT_UPSTREAM_PROXY_HOST = "127.0.0.1"
DEFAULT_UPSTREAM_PROXY_PORT = 7890
CLOUDCODE_HOST = "daily-cloudcode-pa.googleapis.com"
KEEPALIVE_HOSTS = frozenset(
    {
        CLOUDCODE_HOST,
        "aicode.googleapis.com",
        "chatgpt.com",
        "ws.chatgpt.com",
        "api.openai.com",
    }
)
LEAF_CERT_SANS = (
    CLOUDCODE_HOST,
    "*.googleapis.com",
    "chatgpt.com",
    "*.chatgpt.com",
    "openai.com",
    "*.openai.com",
)
LEAF_SAN_VERSION = "v2:googleapis+chatgpt+openai"
CA_COMMON_NAME = "cproxy Local H2 Keepalive CA"

H2_PREFACE = b"PRI * HTTP/2.0\r\n\r\nSM\r\n\r\n"
PING_INTERVAL_SEC = 4.5
WS_IDLE_PING_INTERVAL_SEC = 35.0
WS_PRE_REPLY_TURN_TIMEOUT_SEC = 600.0
WS_POST_REPLY_TURN_TIMEOUT_SEC = 180.0

_WS_TERMINAL_EVENT_MARKERS = (
    b'"response.completed"',
    b'"response.failed"',
    b'"response.incomplete"',
    b'"response.cancelled"',
    b'"type":"error"',
    b'"type": "error"',
)

_SS_AGY_RE = re.compile(r'users:\(\("agy[^"]*",pid=(\d+),')
_SS_LASTSND_RE = re.compile(r"\blastsnd:(\d+)\b")
_SS_BYTES_RCV_RE = re.compile(r"\bbytes_received:(\d+)\b")
_SS_BYTES_SENT_RE = re.compile(r"\bbytes_sent:(\d+)\b")


def is_windows_admin() -> bool:
    if os.name != "nt":
        return False
    try:
        import ctypes

        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _is_posix_root() -> bool:
    geteuid = getattr(os, "geteuid", None)
    return bool(callable(geteuid) and geteuid() == 0)


def _is_pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            kernel32.OpenProcess.restype = wintypes.HANDLE
            kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
            kernel32.GetExitCodeProcess.restype = wintypes.BOOL
            kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel32.CloseHandle.restype = wintypes.BOOL
            handle = kernel32.OpenProcess(0x1000, False, pid)
            if not handle:
                return False
            try:
                exit_code = wintypes.DWORD()
                if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                    return False
                return exit_code.value == 259
            finally:
                kernel32.CloseHandle(handle)
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def is_local_ca_trusted(ca_crt: Path) -> bool:
    """检查本地保活 Root CA 是否已被当前操作系统根证书库信任（零弹窗只读检查）。"""
    if not ca_crt.is_file():
        return False
    try:
        target_der = ssl.PEM_cert_to_DER_cert(ca_crt.read_text(encoding="utf-8"))
    except Exception:
        return False

    if os.name == "nt" and hasattr(ssl, "enum_certificates"):
        try:
            for der_bytes, encoding_type, _trust in ssl.enum_certificates("ROOT"):
                if encoding_type == "x509_asn" and der_bytes == target_der:
                    return True
        except Exception:
            pass
        return False

    for anchor in (
        Path("/etc/pki/ca-trust/source/anchors/cproxy-h2-ca.crt"),
        Path("/usr/local/share/ca-certificates/cproxy-h2-ca.crt"),
    ):
        try:
            if anchor.is_file() and ssl.PEM_cert_to_DER_cert(anchor.read_text(encoding="utf-8")) == target_der:
                return True
        except Exception:
            continue
    return (ca_crt.parent / "ca-trust.installed").is_file()


def _leaf_cert_needs_renewal(leaf_crt: Path, san_marker: Path) -> bool:
    if not leaf_crt.is_file() or not san_marker.is_file():
        return True
    try:
        if san_marker.read_text(encoding="utf-8").strip() != LEAF_SAN_VERSION:
            return True
    except OSError:
        return True
    try:
        from cryptography import x509

        cert = x509.load_pem_x509_certificate(leaf_crt.read_bytes())
        not_after = getattr(cert, "not_valid_after_utc", None)
        if not_after is None:
            not_after = cert.not_valid_after.replace(tzinfo=timezone.utc)
        if not_after - datetime.now(timezone.utc) < timedelta(days=30):
            return True
    except Exception:
        pass
    return False


def _generate_certs_with_cryptography(
    ca_key_path: Path,
    ca_crt_path: Path,
    leaf_key_path: Path,
    leaf_crt_path: Path,
    *,
    need_ca: bool,
    need_leaf: bool,
) -> bool:
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
    except Exception:
        return False

    now = datetime.now(timezone.utc)
    if need_ca:
        ca_priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, CA_COMMON_NAME)])
        ca_ski = x509.SubjectKeyIdentifier.from_public_key(ca_priv.public_key())
        ca_cert = (
            x509.CertificateBuilder()
            .subject_name(ca_name)
            .issuer_name(ca_name)
            .public_key(ca_priv.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(days=1))
            .not_valid_after(now + timedelta(days=3650))
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .add_extension(
                x509.KeyUsage(
                    digital_signature=True,
                    content_commitment=False,
                    key_encipherment=False,
                    data_encipherment=False,
                    key_agreement=False,
                    key_cert_sign=True,
                    crl_sign=True,
                    encipher_only=False,
                    decipher_only=False,
                ),
                critical=True,
            )
            .add_extension(ca_ski, critical=False)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_subject_key_identifier(ca_ski), critical=False)
            .sign(private_key=ca_priv, algorithm=hashes.SHA256())
        )
        ca_key_path.write_bytes(
            ca_priv.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.TraditionalOpenSSL,
                encryption_algorithm=serialization.NoEncryption(),
            )
        )
        ca_crt_path.write_bytes(ca_cert.public_bytes(serialization.Encoding.PEM))
    else:
        ca_priv = serialization.load_pem_private_key(ca_key_path.read_bytes(), password=None)
        ca_cert = x509.load_pem_x509_certificate(ca_crt_path.read_bytes())

    if need_leaf:
        leaf_priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        leaf_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, CLOUDCODE_HOST)])
        leaf_ski = x509.SubjectKeyIdentifier.from_public_key(leaf_priv.public_key())
        try:
            ca_ski_ext = ca_cert.extensions.get_extension_for_class(x509.SubjectKeyIdentifier).value
            aki = x509.AuthorityKeyIdentifier.from_issuer_subject_key_identifier(ca_ski_ext)
        except Exception:
            aki = x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_priv.public_key())  # type: ignore[arg-type]

        leaf_cert = (
            x509.CertificateBuilder()
            .subject_name(leaf_name)
            .issuer_name(ca_cert.subject)
            .public_key(leaf_priv.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(days=1))
            .not_valid_after(now + timedelta(days=365))
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(
                x509.KeyUsage(
                    digital_signature=True,
                    content_commitment=False,
                    key_encipherment=True,
                    data_encipherment=False,
                    key_agreement=False,
                    key_cert_sign=False,
                    crl_sign=False,
                    encipher_only=False,
                    decipher_only=False,
                ),
                critical=True,
            )
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .add_extension(x509.SubjectAlternativeName([x509.DNSName(h) for h in LEAF_CERT_SANS]), critical=False)
            .add_extension(leaf_ski, critical=False)
            .add_extension(aki, critical=False)
            .sign(private_key=ca_priv, algorithm=hashes.SHA256())  # type: ignore[arg-type]
        )
        leaf_key_path.write_bytes(
            leaf_priv.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.TraditionalOpenSSL,
                encryption_algorithm=serialization.NoEncryption(),
            )
        )
        leaf_crt_path.write_bytes(leaf_cert.public_bytes(serialization.Encoding.PEM))
    return True


def _generate_certs_with_openssl(
    tls_dir: Path,
    ca_key_path: Path,
    ca_crt_path: Path,
    leaf_key_path: Path,
    leaf_crt_path: Path,
    *,
    need_ca: bool,
    need_leaf: bool,
) -> None:
    if need_ca:
        subprocess.run(
            [
                "openssl",
                "req",
                "-x509",
                "-newkey",
                "rsa:2048",
                "-nodes",
                "-keyout",
                str(ca_key_path),
                "-out",
                str(ca_crt_path),
                "-days",
                "3650",
                "-subj",
                f"/CN={CA_COMMON_NAME}",
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    if need_leaf:
        csr_path = tls_dir / "cloudcode.csr"
        ext_path = tls_dir / "cloudcode.ext"
        san_csv = ",".join(f"DNS:{h}" for h in LEAF_CERT_SANS)
        ext_path.write_text(
            "authorityKeyIdentifier=keyid,issuer\n"
            "basicConstraints=critical,CA:FALSE\n"
            "keyUsage = critical, digitalSignature, keyEncipherment\n"
            "extendedKeyUsage = serverAuth\n"
            f"subjectAltName = {san_csv}\n",
            encoding="utf-8",
        )
        subprocess.run(
            [
                "openssl",
                "req",
                "-newkey",
                "rsa:2048",
                "-nodes",
                "-keyout",
                str(leaf_key_path),
                "-out",
                str(csr_path),
                "-subj",
                f"/CN={CLOUDCODE_HOST}",
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        subprocess.run(
            [
                "openssl",
                "x509",
                "-req",
                "-in",
                str(csr_path),
                "-CA",
                str(ca_crt_path),
                "-CAkey",
                str(ca_key_path),
                "-CAcreateserial",
                "-out",
                str(leaf_crt_path),
                "-days",
                "365",
                "-extfile",
                str(ext_path),
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )


def _install_system_ca_trust(
    ca_crt: Path,
    trust_marker: Path,
    *,
    allow_user_prompt: bool = False,
) -> None:
    if os.name == "nt":
        if is_local_ca_trusted(ca_crt):
            if not trust_marker.exists():
                trust_marker.write_text(str(time.time()), encoding="utf-8")
            return
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        if is_windows_admin():
            subprocess.run(
                ["certutil.exe", "-addstore", "-f", "Root", str(ca_crt)],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=15.0,
                creationflags=creationflags,
            )
        elif allow_user_prompt:
            subprocess.run(
                ["certutil.exe", "-user", "-addstore", "-f", "Root", str(ca_crt)],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=60.0,
                creationflags=creationflags,
            )
        if is_local_ca_trusted(ca_crt):
            trust_marker.write_text(str(time.time()), encoding="utf-8")
        return

    if not _is_posix_root():
        return

    rhel_dir = Path("/etc/pki/ca-trust/source/anchors")
    deb_dir = Path("/usr/local/share/ca-certificates")
    if rhel_dir.is_dir():
        anchor_file = rhel_dir / "cproxy-h2-ca.crt"
        need_update = not anchor_file.exists() or anchor_file.read_bytes() != ca_crt.read_bytes()
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
    elif deb_dir.is_dir():
        anchor_file = deb_dir / "cproxy-h2-ca.crt"
        need_update = not anchor_file.exists() or anchor_file.read_bytes() != ca_crt.read_bytes()
        if need_update:
            shutil.copyfile(ca_crt, anchor_file)
            subprocess.run(
                ["update-ca-certificates"],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            trust_marker.write_text(str(time.time()), encoding="utf-8")
        elif not trust_marker.exists():
            trust_marker.write_text(str(anchor_file.stat().st_mtime), encoding="utf-8")


def ensure_local_ca_and_leaf_cert(
    tls_dir: Path,
    *,
    install_system_trust: bool = True,
    allow_user_prompt: bool = False,
) -> tuple[Path, Path, Path, float]:
    """确保本地 H2/WebSocket 保活网关的 Root CA 与多域名叶子证书（Cloud Code + ChatGPT/Codex）就绪。

    返回 `(ca_cert_path, leaf_cert_path, leaf_key_path, trust_installed_mtime)`。
    """
    tls_dir.mkdir(parents=True, exist_ok=True)
    ca_key = tls_dir / "cproxy-h2-ca.key"
    ca_crt = tls_dir / "cproxy-h2-ca.crt"
    leaf_key = tls_dir / "cloudcode.key"
    leaf_crt = tls_dir / "cloudcode.crt"
    san_marker = tls_dir / "cloudcode.sans"
    trust_marker = tls_dir / "ca-trust.installed"

    need_ca = not ca_crt.exists() or not ca_key.exists()
    need_leaf = need_ca or not leaf_key.exists() or _leaf_cert_needs_renewal(leaf_crt, san_marker)

    if need_ca or need_leaf:
        ok = _generate_certs_with_cryptography(
            ca_key,
            ca_crt,
            leaf_key,
            leaf_crt,
            need_ca=need_ca,
            need_leaf=need_leaf,
        )
        if not ok:
            _generate_certs_with_openssl(
                tls_dir,
                ca_key,
                ca_crt,
                leaf_key,
                leaf_crt,
                need_ca=need_ca,
                need_leaf=need_leaf,
            )
        san_marker.write_text(f"{LEAF_SAN_VERSION}\n", encoding="utf-8")

    if install_system_trust:
        _install_system_ca_trust(ca_crt, trust_marker, allow_user_prompt=allow_user_prompt)

    anchor_file = Path("/etc/pki/ca-trust/source/anchors/cproxy-h2-ca.crt")

    if anchor_file.exists():
        trust_mtime = anchor_file.stat().st_mtime
    elif trust_marker.exists():
        try:
            trust_mtime = float(trust_marker.read_text(encoding="utf-8").strip())
        except ValueError:
            trust_mtime = trust_marker.stat().st_mtime
    else:
        trust_mtime = ca_crt.stat().st_mtime
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

    Go 标准库 `crypto/x509.systemRootsPool()`（Linux）使用 `sync.Once` 在进程首次 TLS 握手时缓存
    系统根证书池；对于 CA 安装前已启动的旧 `agy` 进程，自动走透明 TCP 直通避免报证书未知错误，
    而新启动的 `agy` 进程（或显式配置了 `SSL_CERT_FILE` 的进程）自动启用 TLS 终结与 H2 PING 保活。
    """
    if os.name == "nt":
        return False
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
    best_key: tuple[int, int, int] = (1 << 60, 1 << 60, 1 << 60)
    lines = proc.stdout.splitlines()
    for idx, line in enumerate(lines):
        m_pid = _SS_AGY_RE.search(line)
        if not m_pid:
            continue
        pid = int(m_pid.group(1))
        detail = lines[idx + 1] if idx + 1 < len(lines) else ""
        m_rcv = _SS_BYTES_RCV_RE.search(detail)
        m_snt = _SS_BYTES_SENT_RE.search(detail)
        m_snd = _SS_LASTSND_RE.search(detail)
        bytes_rcv = int(m_rcv.group(1)) if m_rcv else 0
        bytes_snt = int(m_snt.group(1)) if m_snt else 0
        lastsnd = int(m_snd.group(1)) if m_snd else 0
        # 刚发送 CONNECT 尚未收到 200 响应的新建连接 bytes_received == 0 且 bytes_sent 极小
        cand_key = (bytes_rcv, lastsnd, bytes_snt)
        if cand_key < best_key and lastsnd <= 1000:
            best_key = cand_key
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


def h2_keepalive_stats_file(state_dir: Path) -> Path:
    return state_dir / "h2-keepalive-stats.json"


def load_h2_keepalive_stats(state_dir: Path) -> dict[str, int | float]:
    import json

    path = h2_keepalive_stats_file(state_dir)
    if not path.exists():
        return {"h2_sessions": 0, "pings_sent": 0, "passthrough_sessions": 0, "updated_at": 0.0}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return {
                "h2_sessions": int(data.get("h2_sessions") or 0),
                "pings_sent": int(data.get("pings_sent") or 0),
                "passthrough_sessions": int(data.get("passthrough_sessions") or 0),
                "updated_at": float(data.get("updated_at") or 0.0),
            }
    except Exception:
        pass
    return {"h2_sessions": 0, "pings_sent": 0, "passthrough_sessions": 0, "updated_at": 0.0}


def _record_h2_keepalive_stat(stats_path: Path | None, field: str, delta: int = 1) -> None:
    if stats_path is None:
        return
    try:
        import json

        stats_path.parent.mkdir(parents=True, exist_ok=True)
        cur = load_h2_keepalive_stats(stats_path.parent)
        cur[field] = int(cur.get(field) or 0) + delta
        cur["updated_at"] = time.time()
        tmp = stats_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(cur, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, stats_path)
    except Exception:
        pass


async def _forward_h2_frames_with_ping(
    client_r: asyncio.StreamReader,
    client_w: asyncio.StreamWriter,
    upstream_r: asyncio.StreamReader,
    upstream_w: asyncio.StreamWriter,
    *,
    ping_interval: float = PING_INTERVAL_SEC,
    stats_path: Path | None = None,
) -> None:
    """在 HTTP/2 帧边界上透明转发双向流量，并在存在未完结流 (active_streams > 0) 且静默达到阈值时
    自动向远端发送 17 字节 HTTP/2 PING 帧，彻底阻断上游 VLESS/anytls 80s connIdle 断流。
    """
    preface = await client_r.readexactly(len(H2_PREFACE))
    if preface != H2_PREFACE:
        raise ValueError("Invalid HTTP/2 client preface")
    upstream_w.write(preface)
    await upstream_w.drain()
    _record_h2_keepalive_stat(stats_path, "h2_sessions", 1)

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
                    _record_h2_keepalive_stat(stats_path, "pings_sent", 1)
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


def build_ws_masked_ping(payload: bytes, mask_key: bytes | None = None) -> bytes:
    """构造符合 RFC 6455 §5.3 / §5.5.2 的客户端到服务端掩码 Ping 控制帧 (opcode=0x9)。"""
    if len(payload) > 125:
        raise ValueError("WebSocket Ping payload must not exceed 125 bytes")
    mkey = mask_key if mask_key is not None and len(mask_key) == 4 else os.urandom(4)
    masked_payload = bytes(b ^ mkey[i & 3] for i, b in enumerate(payload))
    return bytes([0x89, 0x80 | len(payload)]) + mkey + masked_payload


def _is_websocket_upgrade_request(raw_headers: bytes) -> bool:
    has_upgrade_ws = False
    for line in raw_headers.split(b"\r\n")[1:]:
        if not line or b":" not in line:
            continue
        k, v = line.split(b":", 1)
        if k.strip().lower() == b"upgrade" and b"websocket" in v.lower():
            has_upgrade_ws = True
            break
    return has_upgrade_ws


def _is_http_101_response(raw_headers: bytes) -> bool:
    first_line = raw_headers.split(b"\r\n", 1)[0].strip()
    parts = first_line.split()
    return len(parts) >= 2 and parts[0].upper().startswith(b"HTTP/1.") and parts[1] == b"101"


async def _forward_ws_frames_with_ping(
    client_r: asyncio.StreamReader,
    client_w: asyncio.StreamWriter,
    upstream_r: asyncio.StreamReader,
    upstream_w: asyncio.StreamWriter,
    *,
    ping_interval: float = PING_INTERVAL_SEC,
    idle_ping_interval: float = WS_IDLE_PING_INTERVAL_SEC,
) -> None:
    """在 RFC 6455 WebSocket 帧边界上流式透明转发双向帧（支持 20MB+ 超大上下文帧零缓冲直传），
    并在模型长思考/上下文压缩静默期按 `ping_interval` (4.5s) 注入掩码 Ping 控制帧，在轮次间空闲期按
    `idle_ping_interval` (35s) 维持长连接存活，同时吸收对应的 `cproxy*` Pong 帧，彻底消除 80s connIdle 10054 断流。
    """
    upstream_lock = asyncio.Lock()
    client_lock = asyncio.Lock()
    now0 = time.monotonic()
    state: dict[str, float | bool] = {
        "last_io": now0,
        "active_turn_until": 0.0,
        "server_replied": False,
        "closing": False,
        "closed": False,
    }

    async def _read_ws_frame_header(reader: asyncio.StreamReader) -> tuple[int, bool, int, bytes]:
        hdr2 = await reader.readexactly(2)
        b0, b1 = hdr2[0], hdr2[1]
        masked = bool(b1 & 0x80)
        len7 = b1 & 0x7F
        if len7 == 126:
            ext = await reader.readexactly(2)
            plen = int.from_bytes(ext, "big")
        elif len7 == 127:
            ext = await reader.readexactly(8)
            plen = int.from_bytes(ext, "big")
        else:
            ext = b""
            plen = len7
        mkey = await reader.readexactly(4) if masked else b""
        return b0, masked, plen, hdr2 + ext + mkey

    async def c2u() -> None:
        try:
            while True:
                b0, _masked, payload_len, wire_hdr = await _read_ws_frame_header(client_r)
                opcode = b0 & 0x0F
                if opcode == 0x08:  # Close
                    state["closing"] = True
                elif opcode in (0x01, 0x02):  # Text / Binary turn request
                    state["server_replied"] = False
                    state["active_turn_until"] = time.monotonic() + WS_PRE_REPLY_TURN_TIMEOUT_SEC

                async with upstream_lock:
                    upstream_w.write(wire_hdr)
                    remaining = payload_len
                    while remaining > 0:
                        chunk = await client_r.read(min(65536, remaining))
                        if not chunk:
                            raise EOFError("Unexpected EOF inside client WebSocket frame")
                        upstream_w.write(chunk)
                        remaining -= len(chunk)
                        state["last_io"] = time.monotonic()
                        if remaining > 0:
                            await upstream_w.drain()
                    await upstream_w.drain()
                now_c = time.monotonic()
                state["last_io"] = now_c
                if opcode in (0x00, 0x01, 0x02) and float(state["active_turn_until"]) > 0.0:
                    state["active_turn_until"] = now_c + WS_PRE_REPLY_TURN_TIMEOUT_SEC
        finally:
            state["closed"] = True

    async def u2c() -> None:
        try:
            while True:
                b0, masked, payload_len, wire_hdr = await _read_ws_frame_header(upstream_r)
                opcode = b0 & 0x0F
                uncompressed_text = opcode == 0x01 and (b0 & 0x40) == 0

                if opcode == 0x0A and payload_len <= 125:  # Pong
                    payload = await upstream_r.readexactly(payload_len) if payload_len else b""
                    if masked:
                        mkey = wire_hdr[-4:]
                        unmasked = bytes(b ^ mkey[i & 3] for i, b in enumerate(payload))
                    else:
                        unmasked = payload
                    if unmasked.startswith(b"cproxy"):
                        state["last_io"] = time.monotonic()
                        continue
                    async with client_lock:
                        client_w.write(wire_hdr + payload)
                        await client_w.drain()
                    state["last_io"] = time.monotonic()
                    continue

                if opcode == 0x08:  # Close
                    state["closing"] = True

                head_sample = b""
                async with client_lock:
                    client_w.write(wire_hdr)
                    remaining = payload_len
                    while remaining > 0:
                        chunk = await upstream_r.read(min(65536, remaining))
                        if not chunk:
                            raise EOFError("Unexpected EOF inside upstream WebSocket frame")
                        if uncompressed_text and len(head_sample) < 256:
                            need = 256 - len(head_sample)
                            head_sample += chunk[:need]
                        client_w.write(chunk)
                        remaining -= len(chunk)
                        state["last_io"] = time.monotonic()
                        if remaining > 0:
                            await client_w.drain()
                    await client_w.drain()

                now_u = time.monotonic()
                state["last_io"] = now_u
                if opcode in (0x00, 0x01, 0x02):
                    if uncompressed_text and any(m in head_sample for m in _WS_TERMINAL_EVENT_MARKERS):
                        state["active_turn_until"] = 0.0
                        state["server_replied"] = False
                    elif float(state["active_turn_until"]) > 0.0:
                        state["server_replied"] = True
                        state["active_turn_until"] = now_u + WS_POST_REPLY_TURN_TIMEOUT_SEC
        finally:
            state["closed"] = True

    async def pinger() -> None:
        seq = 0
        eff_idle = idle_ping_interval if idle_ping_interval > 0 else ping_interval
        min_iv = min(ping_interval, eff_idle) if eff_idle > 0 else ping_interval
        tick = min(1.0, max(0.05, min_iv / 4.0))
        try:
            while not state["closed"] and not state["closing"]:
                await asyncio.sleep(tick)
                if state["closed"] or state["closing"]:
                    break
                now_p = time.monotonic()
                in_turn = now_p < float(state["active_turn_until"])
                threshold = ping_interval if in_turn else eff_idle
                if threshold <= 0:
                    continue
                if (now_p - float(state["last_io"])) >= threshold:
                    seq = (seq + 1) % 100
                    ping_frame = build_ws_masked_ping(f"cproxy{seq:02d}".encode("ascii"))
                    async with upstream_lock:
                        if state["closed"] or state["closing"]:
                            break
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


async def _forward_http1_or_ws_with_ping(
    client_r: asyncio.StreamReader,
    client_w: asyncio.StreamWriter,
    upstream_r: asyncio.StreamReader,
    upstream_w: asyncio.StreamWriter,
    *,
    ping_interval: float = PING_INTERVAL_SEC,
    idle_ping_interval: float = WS_IDLE_PING_INTERVAL_SEC,
) -> None:
    """识别 HTTP/1.1 `Upgrade: websocket` -> `101 Switching Protocols` 握手并切入 RFC 6455 帧级 Ping 保活；
    非 WebSocket 升级的普通 HTTP/1.1 请求保持零改动双向流直通。
    """
    try:
        req_hdr = await asyncio.wait_for(client_r.readuntil(b"\r\n\r\n"), timeout=30.0)
    except asyncio.IncompleteReadError as exc:
        if exc.partial:
            upstream_w.write(exc.partial)
            await upstream_w.drain()
        return
    except (asyncio.LimitOverrunError, TimeoutError):
        await asyncio.gather(
            _pipe_raw(client_r, upstream_w),
            _pipe_raw(upstream_r, client_w),
        )
        return

    upstream_w.write(req_hdr)
    await upstream_w.drain()

    if not _is_websocket_upgrade_request(req_hdr):
        await asyncio.gather(
            _pipe_raw(client_r, upstream_w),
            _pipe_raw(upstream_r, client_w),
        )
        return

    try:
        resp_hdr = await asyncio.wait_for(upstream_r.readuntil(b"\r\n\r\n"), timeout=60.0)
    except asyncio.IncompleteReadError as exc:
        if exc.partial:
            client_w.write(exc.partial)
            await client_w.drain()
        return
    except (asyncio.LimitOverrunError, TimeoutError):
        await asyncio.gather(
            _pipe_raw(client_r, upstream_w),
            _pipe_raw(upstream_r, client_w),
        )
        return

    client_w.write(resp_hdr)
    await client_w.drain()

    if _is_http_101_response(resp_hdr):
        await _forward_ws_frames_with_ping(
            client_r,
            client_w,
            upstream_r,
            upstream_w,
            ping_interval=ping_interval,
            idle_ping_interval=idle_ping_interval,
        )
    else:
        await asyncio.gather(
            _pipe_raw(client_r, upstream_w),
            _pipe_raw(upstream_r, client_w),
        )


async def _connect_upstream_socks5(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    target_host: str,
    target_port: int,
) -> bool:
    """通过 SOCKS5 协议向上游 mihomo mixed-port 发起建连（以区分于客户端 HTTP CONNECT / TUN 入站）。"""
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
        idle_ping_interval: float = WS_IDLE_PING_INTERVAL_SEC,
        install_system_trust: bool = True,
        allow_user_prompt: bool = False,
        require_system_trust: bool = False,
        stats_path: Path | None = None,
    ) -> None:
        self.tls_dir = tls_dir
        self.listen_host = listen_host
        self.listen_port = listen_port
        self.upstream_host = upstream_host
        self.upstream_port = upstream_port
        self.upstream_protocol = upstream_protocol
        self.ping_interval = ping_interval
        self.idle_ping_interval = idle_ping_interval
        self.require_system_trust = require_system_trust
        self.stats_path = stats_path
        ca_crt, leaf_crt, leaf_key, trust_mtime = ensure_local_ca_and_leaf_cert(
            tls_dir,
            install_system_trust=install_system_trust,
            allow_user_prompt=allow_user_prompt,
        )
        self.ca_cert_path = ca_crt
        self.trust_installed_mtime = trust_mtime
        self._ca_trusted_cached = is_local_ca_trusted(ca_crt)
        self._ca_trust_checked_at = time.monotonic()
        self._passthrough_until: dict[str, float] = {}
        self.server_ssl = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.server_ssl.load_cert_chain(certfile=str(leaf_crt), keyfile=str(leaf_key))
        self.server_ssl.set_alpn_protocols(["h2", "http/1.1"])
        self.client_ssl = ssl.create_default_context()
        self.client_ssl.set_alpn_protocols(["h2", "http/1.1"])
        self.client_ssl_h1 = ssl.create_default_context()
        self.client_ssl_h1.set_alpn_protocols(["http/1.1"])

    def is_ca_trusted(self) -> bool:
        if self._ca_trusted_cached:
            return True
        now = time.monotonic()
        if (now - self._ca_trust_checked_at) >= 15.0:
            self._ca_trust_checked_at = now
            self._ca_trusted_cached = is_local_ca_trusted(self.ca_cert_path)
            if self._ca_trusted_cached:
                self.trust_installed_mtime = time.time()
        return self._ca_trusted_cached

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

            now_mono = time.monotonic()
            host_cooldown = self._passthrough_until.get(target_host, 0.0) > now_mono
            ca_blocked = self.require_system_trust and not self.is_ca_trusted()
            passthrough = (
                ca_blocked
                or host_cooldown
                or target_host not in KEEPALIVE_HOSTS
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
                if target_host == CLOUDCODE_HOST:
                    _record_h2_keepalive_stat(self.stats_path, "passthrough_sessions", 1)
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
                    stats_path=self.stats_path,
                )
            else:
                await _forward_http1_or_ws_with_ping(
                    client_r,
                    client_w,
                    upstream_r,
                    upstream_w,
                    ping_interval=self.ping_interval,
                    idle_ping_interval=self.idle_ping_interval,
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
    idle_ping_interval: float = WS_IDLE_PING_INTERVAL_SEC,
    pid_path: Path | None = None,
    install_system_trust: bool = True,
    allow_user_prompt: bool = False,
    require_system_trust: bool = True,
) -> None:
    stats_path = h2_keepalive_stats_file(pid_path.parent) if pid_path is not None else None
    proxy = H2KeepaliveProxy(
        tls_dir,
        listen_host=listen_host,
        listen_port=listen_port,
        upstream_host=upstream_host,
        upstream_port=upstream_port,
        ping_interval=ping_interval,
        idle_ping_interval=idle_ping_interval,
        install_system_trust=install_system_trust,
        allow_user_prompt=allow_user_prompt,
        require_system_trust=require_system_trust,
        stats_path=stats_path,
    )
    server = await asyncio.start_server(proxy.handle_client, listen_host, listen_port)
    if pid_path is not None:
        try:
            pid_path.parent.mkdir(parents=True, exist_ok=True)
            pid_path.write_text(f"{os.getpid()}\n", encoding="utf-8")
            _record_h2_keepalive_stat(stats_path, "pings_sent", 0)
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
        if not _is_pid_alive(pid):
            return False
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
    """确保本地 H2/WebSocket PING 保活网关守护进程已在后台监听。"""
    import sys

    if is_h2_keepalive_running(state_dir, port=listen_port):
        return True
    state_dir.mkdir(parents=True, exist_ok=True)
    log_path = state_dir / "h2-keepalive.log"
    pid_path = h2_keepalive_pid_file(state_dir)
    tls_dir = data_dir / "tls"

    exe_path = Path(sys.executable)
    if os.name == "nt":
        daemon_candidate = exe_path.parent / "cproxy-daemon.exe"
        if daemon_candidate.is_file():
            exe_path = daemon_candidate

    code = (
        "import asyncio; from pathlib import Path; "
        "from cproxy.services.h2_keepalive import run_h2_keepalive_server; "
        f"asyncio.run(run_h2_keepalive_server(Path({str(tls_dir)!r}), "
        f"listen_port={listen_port}, upstream_port={upstream_port}, "
        f"pid_path=Path({str(pid_path)!r})))"
    )
    popen_kwargs: dict[str, object] = {}
    if os.name == "nt":
        popen_kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(
            subprocess, "DETACHED_PROCESS", 0
        )
    else:
        popen_kwargs["start_new_session"] = True
    try:
        with log_path.open("a", encoding="utf-8") as log_handle:
            subprocess.Popen(
                [str(exe_path), "-c", code],
                stdout=log_handle,
                stderr=log_handle,
                **popen_kwargs,  # type: ignore[arg-type]
            )
        for _ in range(25):
            time.sleep(0.1)
            if is_h2_keepalive_running(state_dir, port=listen_port):
                return True
    except Exception:
        return False
    return False
