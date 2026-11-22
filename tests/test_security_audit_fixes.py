"""Security, redaction, completion, SSRF, GA artifact, systemd, and script hardening regression tests."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import tarfile
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Thread
from types import SimpleNamespace

import pytest
import yaml

from cproxy.audit import audit_log_file, write_audit_event
from cproxy.config import config_file, default_paths
from cproxy.install import init_user_layout, migrate_from_legacy
from cproxy.proxyenv import run_proxy_shell
from cproxy.redaction import redact_text, redact_value
from cproxy.security import validate_controller_security
from cproxy.services import ops as ops_module
from cproxy.services.bundle import export_bundle, import_bundle
from cproxy.services.notify import (
    _mask_webhook_url,
    notify_state_file,
    save_notify_config,
    send_notification,
)
from cproxy.services.ops import build_incident, get_ai_connections, guard
from cproxy.support import build_support_bundle

ROOT_DIR = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# 1. P0 Bash / Zsh completion RCE regression
# ---------------------------------------------------------------------------


def test_bash_completion_never_executes_command_substitution_in_node_names(tmp_path: Path) -> None:
    pwn_marker = tmp_path / "pwn"
    backtick_marker = tmp_path / "pwn_bt"
    malicious_node = f"US $(touch {pwn_marker}) 01"
    backtick_node = f"JP `touch {backtick_marker}` 02"
    bash_script = ROOT_DIR / "src" / "cproxy" / "data" / "completion" / "cproxy.bash"

    harness = f"""
set -euo pipefail
source "{bash_script}"
cproxy() {{
    if [ "${{1:-}}" = "__complete" ]; then
        printf '%s\\n' {malicious_node!r}
        printf '%s\\n' {backtick_node!r}
        printf '%s\\n' 'AI-MANUAL'
        return 0
    fi
    return 1
}}
COMP_WORDS=(cproxy switch AI-MANUAL "")
COMP_CWORD=3
_cproxy_complete
printf 'COUNT=%d\\n' "${{#COMPREPLY[@]}}"
for entry in "${{COMPREPLY[@]}}"; do
    printf 'ITEM=%s\\n' "$entry"
done
# Even if bash evaluates the escaped completion word on the command line, it must NOT execute the payload
eval "set -- ${{COMPREPLY[0]}}"
printf 'LITERAL0=%s\\n' "$1"
"""
    res = subprocess.run(["/bin/bash", "-c", harness], capture_output=True, text=True, check=False)
    assert res.returncode == 0, res.stderr
    assert not pwn_marker.exists(), "Bash completion executed $(...) command substitution!"
    assert not backtick_marker.exists(), "Bash completion executed backtick command substitution!"
    assert "COUNT=3" in res.stdout
    assert "ITEM=AI-MANUAL" in res.stdout
    assert f"LITERAL0={malicious_node}" in res.stdout


def test_zsh_completion_quotes_words_and_candidates() -> None:
    zsh_script = (ROOT_DIR / "src" / "cproxy" / "data" / "completion" / "_cproxy").read_text(encoding="utf-8")
    assert '"${words[@]}"' in zsh_script
    assert '"${candidates[@]}"' in zsh_script


# ---------------------------------------------------------------------------
# 2. P0 Secret & credential redaction
# ---------------------------------------------------------------------------


def test_redact_value_covers_expanded_sensitive_keys() -> None:
    payload = {
        "uuid": "11111111-2222-3333-4444-555555555555",
        "private-key": "priv-key-val",
        "private_key": "priv-key-val2",
        "preshared-key": "psk-val-1",
        "psk": "psk-val-2",
        "auth": "user:pass",
        "authentication": "auth-val",
        "auth-str": "hysteria-auth",
        "api-key": "ak-live-123",
        "api_key": "ak-live-456",
        "cookie": "session=xyz",
        "webhook": "https://hooks.example.com/abc",
        "short-id": "abcdef12",
        "author": "alice",
        "mode": "rule",
    }
    redacted = redact_value(payload)
    for sensitive in (
        "uuid",
        "private-key",
        "private_key",
        "preshared-key",
        "psk",
        "auth",
        "authentication",
        "auth-str",
        "api-key",
        "api_key",
        "cookie",
        "webhook",
        "short-id",
    ):
        assert redacted[sensitive] == "[REDACTED]", f"{sensitive} was not redacted"
    assert redacted["author"] == "alice"
    assert redacted["mode"] == "rule"


def test_redact_text_handles_prefixed_keys_quoted_values_auth_headers_and_urls() -> None:
    samples = [
        ("access_token=tok_12345 client_secret=cs_987", ("tok_12345", "cs_987")),
        ("bot_token=123456:ABCDEF proxy_password='my secret pass'", ("123456:ABCDEF", "my secret pass")),
        ('password="my secret with spaces" api-key="sk-9999"', ("my secret with spaces", "sk-9999")),
        (
            "Authorization: Bearer b_tok Authorization: Basic dXNlcjpwYXNz Authorization: Token t_tok",
            ("b_tok", "dXNlcjpwYXNz", "t_tok"),
        ),
        ("https://alice:s3cr3t@example.com/path socks5h://u:p@127.0.0.1:7890", ("alice", "s3cr3t", "u:p")),
        ("https://api.telegram.org/bot123456:AAH_SECRET/sendMessage?chat_id=99", ("123456:AAH_SECRET", "chat_id=99")),
        ("https://hooks.slack.com/services/T111/B222/SLACKSECRET", ("T111", "SLACKSECRET")),
        ("https://discord.com/api/webhooks/123456/DISCORDSECRET", ("DISCORDSECRET",)),
        ("https://api.day.app/BARKSECRETKEY/", ("BARKSECRETKEY",)),
        ("https://airport.example.com/sub/PATHTOKEN12345", ("PATHTOKEN12345",)),
    ]
    for raw, secrets in samples:
        out = redact_text(raw)
        for secret in secrets:
            assert secret not in out, f"Secret {secret!r} leaked in: {out!r}"
        assert "[REDACTED]" in out or "?..." in out

    # Standard subscription query redaction keeps trailing ?...
    assert redact_text("https://example.test/api/v1/client/subscribe?token=secret") == (
        "https://example.test/api/v1/client/subscribe?..."
    )


def test_mask_webhook_url_redacts_userinfo_telegram_and_bark() -> None:
    tg = _mask_webhook_url("https://api.telegram.org/bot123456:AAH_MY_SECRET/sendMessage?chat_id=42")
    assert "123456:AAH_MY_SECRET" not in tg
    assert "42" not in tg
    assert "/bot[REDACTED]/" in tg

    bark = _mask_webhook_url("https://api.day.app/MySuperSecretBarkKey/")
    assert "MySuperSecretBarkKey" not in bark
    assert bark == "https://api.day.app/[REDACTED]"

    userinfo = _mask_webhook_url("https://admin:hunter2@hooks.example.com/notify/secret-token")
    assert "admin" not in userinfo
    assert "hunter2" not in userinfo
    assert "secret-token" not in userinfo
    assert "[REDACTED]@hooks.example.com" in userinfo


def test_support_bundle_mode_0600_invalid_yaml_and_deque_tail(tmp_path: Path) -> None:
    paths = default_paths(tmp_path)
    paths.config_dir.mkdir(parents=True)
    paths.data_dir.mkdir(parents=True)
    paths.state_dir.mkdir(parents=True)

    # Deliberately malformed YAML containing secrets
    (paths.config_dir / "config.yaml").write_text(
        'secret: [unclosed\npassword="my secret"\nurl: https://user:pass@host.test/sub/TOKEN999\n',
        encoding="utf-8",
    )
    # 700 lines in cproxy.log; bundle must retain only last 500 lines and redact secrets
    log_lines = [f"line-{i} access_token=tok_{i}" for i in range(700)]
    (paths.state_dir / "cproxy.log").write_text("\n".join(log_lines) + "\n", encoding="utf-8")

    bundle_path = build_support_bundle(paths, tmp_path / "support.tar.gz")
    if os.name != "nt":
        assert stat.S_IMODE(bundle_path.stat().st_mode) == 0o600

    with tarfile.open(bundle_path, "r:gz") as archive:
        cfg_text = archive.extractfile("config.redacted.yaml").read().decode("utf-8")  # type: ignore[union-attr]
        log_text = archive.extractfile("log.tail.redacted.txt").read().decode("utf-8")  # type: ignore[union-attr]

    assert "my secret" not in cfg_text
    assert "user:pass" not in cfg_text
    assert "TOKEN999" not in cfg_text
    assert "[REDACTED]" in cfg_text

    tail_lines = log_text.splitlines()
    assert len(tail_lines) == 500
    assert tail_lines[0].startswith("line-200 ")
    assert "tok_200" not in tail_lines[0]
    assert "tok_699" not in tail_lines[-1]


def test_build_incident_redacts_pgrep_and_errors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    paths = default_paths(tmp_path)

    def _fake_run(argv, **kwargs):
        if argv[0] == "systemctl":
            return SimpleNamespace(returncode=0, stdout="ActiveState=active\n")
        if argv[0] == "pgrep":
            return SimpleNamespace(
                returncode=0,
                stdout="1234 /usr/local/bin/mihomo -f runtime.yaml --secret topsecret123\n",
            )
        return SimpleNamespace(returncode=1, stdout="")

    class _BoomAPI:
        def __init__(self, _paths):
            pass

        def get_groups(self):
            raise RuntimeError("GET https://user:pass@127.0.0.1:9090/proxies Authorization: Bearer leak123")

        def get_connections(self):
            raise RuntimeError("access_token=conn_leak")

    class _BoomProbe:
        def __init__(self, _paths):
            pass

        def probe(self, **kwargs):
            raise RuntimeError('password="probe leak"')

    monkeypatch.setattr(ops_module.subprocess, "run", _fake_run)
    monkeypatch.setattr(ops_module, "APIBackend", _BoomAPI)
    monkeypatch.setattr(ops_module, "ProbeService", _BoomProbe)

    sections = build_incident(paths)
    combined = "\n".join(line for sec in sections for line in sec.lines)
    for leaked in ("topsecret123", "user:pass", "leak123", "conn_leak", "probe leak"):
        assert leaked not in combined
    assert "[REDACTED]" in combined


# ---------------------------------------------------------------------------
# 3. P0 security-check bypasses
# ---------------------------------------------------------------------------


def test_security_check_validates_both_controllers_and_edge_cases(tmp_path: Path) -> None:
    paths = default_paths(tmp_path)
    paths.config_dir.mkdir(parents=True)
    secret_path = paths.config_dir / "ctrl-secret"
    secret_path.write_text("strong-secret\n", encoding="utf-8")
    secret_path.chmod(0o600)

    # Plaintext non-loopback MUST NOT be ignored when TLS controller is also configured
    config_file(paths).write_text(
        "external-controller: 0.0.0.0:9090\n"
        "external-controller-tls: 127.0.0.1:9443\n"
        f"secret-file: {secret_path}\n",
        encoding="utf-8",
    )
    report = validate_controller_security(paths)
    assert not report.ok
    assert "non-loopback-controller" in {i.code for i in report.issues}

    # Empty host (:9090) and wildcard (*) must fail
    for bad_addr in (":9090", "*:9090", "*"):
        config_file(paths).write_text(
            f"external-controller: '{bad_addr}'\n"
            "external-controller-tls: 127.0.0.1:9443\n"
            f"secret-file: {secret_path}\n",
            encoding="utf-8",
        )
        rep = validate_controller_security(paths)
        assert not rep.ok
        assert "non-loopback-controller" in {i.code for i in rep.issues}

    # Inline secret warns unconditionally even when secret-file is also set
    config_file(paths).write_text(
        "external-controller-tls: 127.0.0.1:9443\n"
        f"secret-file: {secret_path}\n"
        "secret: plain-inline\n",
        encoding="utf-8",
    )
    rep_inline = validate_controller_security(paths)
    assert rep_inline.ok  # warning only
    assert "inline-secret" in {i.code for i in rep_inline.issues}

    # Loose secret-file permissions fail
    if os.name != "nt":
        secret_path.chmod(0o644)
        config_file(paths).write_text(
            "external-controller-tls: 127.0.0.1:9443\n"
            f"secret-file: {secret_path}\n",
            encoding="utf-8",
        )
        rep_perm = validate_controller_security(paths)
        assert not rep_perm.ok
        assert "secret-file-permissions" in {i.code for i in rep_perm.issues}
        secret_path.chmod(0o600)

    # Path traversal in secret-systemd-credential, pipe controller, and allow-lan wildcard
    config_file(paths).write_text(
        "external-controller-tls: 127.0.0.1:9443\n"
        "external-controller-pipe: '\\\\.\\pipe\\mihomo'\n"
        "allow-lan: true\n"
        "bind-address: '*'\n"
        "secret-systemd-credential: ../etc/passwd\n",
        encoding="utf-8",
    )
    rep_combo = validate_controller_security(paths)
    codes = {i.code for i in rep_combo.issues}
    assert not rep_combo.ok
    assert {"pipe-controller", "allow-lan-wildcard", "systemd-credential-invalid", "missing-secret"} <= codes


# ---------------------------------------------------------------------------
# 4. P0 Private 0o600 atomic writes
# ---------------------------------------------------------------------------


def test_private_0600_writes_for_install_audit_notify_bundle(tmp_path: Path) -> None:
    if os.name == "nt":
        pytest.skip("POSIX file modes not applicable on Windows")

    paths = default_paths(tmp_path / "user")
    cfg = init_user_layout(paths)
    assert stat.S_IMODE(cfg.stat().st_mode) == 0o600

    legacy = tmp_path / "legacy"
    legacy.mkdir()
    (legacy / "config.yaml").write_text("mixed-port: 7890\nsecret: s1\n", encoding="utf-8")
    migrated = migrate_from_legacy(default_paths(tmp_path / "migrated"), legacy)
    assert stat.S_IMODE(migrated.stat().st_mode) == 0o600

    write_audit_event(paths, "test", "target", "ok", {"token": "abc"})
    assert stat.S_IMODE(audit_log_file(paths).stat().st_mode) == 0o600

    save_notify_config(paths, webhook_url="https://hooks.example.com/hook/tok123", desktop=False)
    assert stat.S_IMODE(notify_state_file(paths).stat().st_mode) == 0o600


# ---------------------------------------------------------------------------
# 5. P1 SSRF & --redact import corruption
# ---------------------------------------------------------------------------


def test_notify_rejects_non_http_and_disables_redirects(tmp_path: Path) -> None:
    paths = default_paths(tmp_path)
    paths.state_dir.mkdir(parents=True)
    notify_state_file(paths).write_text(
        json.dumps({"webhook_url": "file:///etc/passwd", "desktop": False}),
        encoding="utf-8",
    )
    with pytest.raises(RuntimeError, match="http/https"):
        send_notification(paths, "t", "m", raise_on_error=True)

    with pytest.raises(ValueError, match="http/https"):
        save_notify_config(paths, webhook_url="ftp://example.com/hook")

    # Spin up local server that 302-redirects to a forbidden target endpoint
    target_hits: list[str] = []

    class _RedirectHandler(BaseHTTPRequestHandler):
        def do_POST(self):
            if self.path.startswith("/start"):
                self.send_response(302)
                self.send_header("Location", f"http://127.0.0.1:{self.server.server_port}/secret-target")  # type: ignore[attr-defined]
                self.end_headers()
                return
            target_hits.append(self.path)
            self.send_response(200)
            self.end_headers()

        def do_GET(self):
            target_hits.append(self.path)
            self.send_response(200)
            self.end_headers()

        def log_message(self, format, *args):
            return

    server = HTTPServer(("127.0.0.1", 0), _RedirectHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        save_notify_config(paths, webhook_url=f"http://127.0.0.1:{server.server_port}/start?token=SECRET_XYZ")
        with pytest.raises(RuntimeError) as exc_info:
            send_notification(paths, "title", "body", raise_on_error=True)
        assert "SECRET_XYZ" not in str(exc_info.value)
        assert target_hits == [], f"Redirect target must NEVER be requested, got {target_hits}"
    finally:
        server.shutdown()
        thread.join()


def test_bundle_import_preserves_real_subscriptions_and_validates_urls_and_rules(tmp_path: Path) -> None:
    src = default_paths(tmp_path / "src")
    dst = default_paths(tmp_path / "dst")
    src.config_dir.mkdir(parents=True)
    dst.config_dir.mkdir(parents=True)

    config_file(src).write_text(
        yaml.safe_dump(
            {
                "subscription-url": "https://main.example.com/sub?token=SECRET_MAIN",
                "subscriptions": [
                    {"name": "ExtraA", "url": "https://a.example.com/sub?token=SECRET_A"},
                ],
                "max-ai-multiplier": 2.0,
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    config_file(dst).write_text(
        yaml.safe_dump(
            {
                "subscription-url": "https://real-main.example.com/sub?token=REAL_MAIN",
                "subscriptions": [
                    {"name": "ExtraA", "url": "https://real-a.example.com/sub?token=REAL_A"},
                ],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    bundle = export_bundle(src, redact=True)
    bundle_file = tmp_path / "redacted-bundle.yaml"
    bundle_file.write_text(yaml.safe_dump(bundle, sort_keys=False), encoding="utf-8")

    import_bundle(dst, bundle_file)
    restored = yaml.safe_load(config_file(dst).read_text(encoding="utf-8"))
    # Existing real subscriptions MUST NOT be overwritten by redacted entries
    assert restored["subscription-url"] == "https://real-main.example.com/sub?token=REAL_MAIN"
    assert restored["subscriptions"] == [{"name": "ExtraA", "url": "https://real-a.example.com/sub?token=REAL_A"}]
    assert restored["max-ai-multiplier"] == 2.0
    if os.name != "nt":
        assert stat.S_IMODE(config_file(dst).stat().st_mode) == 0o600

    # Reject non-http URLs in bundle
    bad_url_bundle = tmp_path / "bad-url.yaml"
    bad_url_bundle.write_text(
        yaml.safe_dump(
            {"bundle_version": 1, "config": {"subscription-url": "file:///etc/passwd"}},
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="http/https"):
        import_bundle(dst, bad_url_bundle)

    # Reject unsupported rule_type and rule delimiter injection
    bad_rule_type = tmp_path / "bad-rule-type.yaml"
    bad_rule_type.write_text(
        yaml.safe_dump(
            {
                "bundle_version": 1,
                "custom_rules": [{"rule_type": "MATCH", "value": "x", "policy": "DIRECT"}],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="不支持的规则类型"):
        import_bundle(dst, bad_rule_type)

    bad_rule_chars = tmp_path / "bad-rule-chars.yaml"
    bad_rule_chars.write_text(
        yaml.safe_dump(
            {
                "bundle_version": 1,
                "custom_rules": [{"rule_type": "DOMAIN", "value": "evil.com,DIRECT\nMATCH", "policy": "DIRECT"}],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="逗号或换行符"):
        import_bundle(dst, bad_rule_chars)


# ---------------------------------------------------------------------------
# 6. P1 GA artifact verification hardening
# ---------------------------------------------------------------------------


def test_verify_ga_artifacts_rejects_traversal_and_sha_mismatch(tmp_path: Path) -> None:
    out_dir = tmp_path / "ga"
    build = subprocess.run(
        ["bash", "scripts/build-ga-artifacts.sh", str(out_dir)],
        capture_output=True,
        text=True,
        cwd=ROOT_DIR,
    )
    assert build.returncode == 0, build.stderr

    # 1. Tamper provenance sha256 -> must fail
    prov_path = out_dir / "provenance.json"
    orig_prov = json.loads(prov_path.read_text(encoding="utf-8"))
    tampered = dict(orig_prov, source_archive_sha256="0" * 64)
    prov_path.write_text(json.dumps(tampered), encoding="utf-8")
    res = subprocess.run(["bash", "scripts/verify-ga-artifacts.sh", str(out_dir)], capture_output=True, text=True, cwd=ROOT_DIR)
    assert res.returncode != 0

    # 2. Path traversal in source_archive -> must fail
    evil = tmp_path / "outside.tar.gz"
    evil.write_bytes(b"evil")
    traversal = dict(orig_prov, source_archive="../outside.tar.gz", source_archive_sha256=hashlib.sha256(b"evil").hexdigest())
    prov_path.write_text(json.dumps(traversal), encoding="utf-8")
    res2 = subprocess.run(["bash", "scripts/verify-ga-artifacts.sh", str(out_dir)], capture_output=True, text=True, cwd=ROOT_DIR)
    assert res2.returncode != 0

    # 3. SHA256SUMS covers a different file than provenance source_archive -> must fail
    prov_path.write_text(json.dumps(orig_prov), encoding="utf-8")
    decoy = out_dir / "decoy.txt"
    decoy_bytes = b"ok\n"
    decoy.write_bytes(decoy_bytes)
    decoy_sha = hashlib.sha256(decoy_bytes).hexdigest()
    (out_dir / "SHA256SUMS").write_text(f"{decoy_sha}  decoy.txt\n", encoding="utf-8")
    res3 = subprocess.run(["bash", "scripts/verify-ga-artifacts.sh", str(out_dir)], capture_output=True, text=True, cwd=ROOT_DIR)
    assert res3.returncode != 0


# ---------------------------------------------------------------------------
# 7/8/9. Windows scripts, install.sh, systemd units, proxyenv, ops, config.example
# ---------------------------------------------------------------------------


def test_windows_scripts_and_gitattributes() -> None:
    bat_bytes = (ROOT_DIR / "setup-windows-verge.bat").read_bytes()
    assert b"\r\n" in bat_bytes
    assert b"\n" not in bat_bytes.replace(b"\r\n", b"")
    assert b'if "%~1"==""' in bat_bytes

    gitattributes = (ROOT_DIR / ".gitattributes").read_text(encoding="utf-8")
    assert "*.bat text eol=crlf" in gitattributes

    ps1 = (ROOT_DIR / "scripts" / "windows" / "install-verge-task.ps1").read_text(encoding="utf-8")
    assert "UnauthorizedAccessException" in ps1
    assert "& $PythonExe -m cproxy.cli verge once --no-rebalance" in ps1
    assert "cproxy-daemon.exe" in ps1.split("if ($Uninstall)")[1].split("exit 0")[0]


def test_install_script_copies_ruleset_uses_pipx_venv_and_handles_cron_and_special_paths(tmp_path: Path) -> None:
    home_dir = tmp_path / "home&dir"
    home_dir.mkdir()
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()

    pipx_log = tmp_path / "pipx.log"
    venv_py = home_dir / ".local" / "pipx" / "venvs" / "cproxy" / "bin" / "python"
    venv_py.parent.mkdir(parents=True)
    venv_log = tmp_path / "venv-python.log"
    venv_py.write_text(
        f"""#!/bin/bash
printf '%s\\n' "$*" >> "{venv_log}"
mkdir -p "$HOME/.config/cproxy"
touch "$HOME/.config/cproxy/config.yaml"
exit 0
""",
        encoding="utf-8",
    )
    venv_py.chmod(0o755)

    (fake_bin / "python3").write_text("#!/bin/bash\nexit 0\n", encoding="utf-8")
    (fake_bin / "python3").chmod(0o755)
    (fake_bin / "pipx").write_text(f'#!/bin/bash\nprintf \'%s\\n\' "$*" >> "{pipx_log}"\nexit 0\n', encoding="utf-8")
    (fake_bin / "pipx").chmod(0o755)
    (fake_bin / "id").write_text(
        '#!/bin/bash\n[ "${1:-}" = "-u" ] && echo 1000 && exit 0\nexec /usr/bin/id "$@"\n',
        encoding="utf-8",
    )
    (fake_bin / "id").chmod(0o755)

    # Failing crontab must NOT print "已添加"
    (fake_bin / "crontab").write_text("#!/bin/bash\nexit 1\n", encoding="utf-8")
    (fake_bin / "crontab").chmod(0o755)

    logrotate_dir = tmp_path / "logrotate.d"
    logrotate_dir.mkdir()
    env = os.environ.copy()
    env["HOME"] = str(home_dir)
    env["PATH"] = f"{fake_bin}:{env['PATH']}"
    env["CPROXY_LOGROTATE_DIR"] = str(logrotate_dir)
    env["CPROXY_GEODATA_DOWNLOAD"] = "0"

    res = subprocess.run(
        ["/bin/bash", str(ROOT_DIR / "scripts" / "install.sh")],
        capture_output=True,
        text=True,
        cwd=ROOT_DIR,
        env=env,
    )
    assert res.returncode == 0, res.stderr
    assert "日志轮转定时任务: 已添加" not in res.stdout
    assert "添加日志轮转定时任务失败" in res.stderr
    assert (home_dir / ".local" / "share" / "cproxy" / "ruleset" / "ChinaMax.yml").is_file()
    assert "-m cproxy.cli init" in venv_log.read_text(encoding="utf-8")
    assert "-m cproxy.cli bootstrap" in venv_log.read_text(encoding="utf-8")

    # Quoted & literal path containing '&' substituted safely in logrotate config
    conf_text = (logrotate_dir / "cproxy").read_text(encoding="utf-8")
    expected_log = str(home_dir / ".local" / "state" / "cproxy" / "cproxy.log")
    assert f'"{expected_log}" {{' in conf_text


def test_proxyenv_ops_systemd_and_config_example_hardening(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    paths = default_paths(tmp_path)

    # proxyenv: empty SHELL falls back to /bin/bash
    captured_argv: list[list[str]] = []
    monkeypatch.setenv("SHELL", "")
    monkeypatch.setattr(
        "cproxy.proxyenv.subprocess.run",
        lambda argv, **kwargs: captured_argv.append(list(argv)) or SimpleNamespace(returncode=0),
    )
    assert run_proxy_shell(paths, ["-c", "true"]) == 0
    assert captured_argv[0][0] == "/bin/bash"

    # ops: get_ai_connections reverses Mihomo leaf->group chains to match get_active_ai_processes
    monkeypatch.setattr(
        "cproxy.services.ops.APIBackend.get_connections",
        lambda _self: {
            "connections": [
                {"metadata": {"host": "api.openai.com"}, "chains": ["🇺🇸US 01", "AI-MANUAL"]},
            ]
        },
    )
    conns = get_ai_connections(paths)
    assert len(conns) == 1
    assert conns[0].route == "AI-MANUAL -> 🇺🇸US 01"

    # ops: guard honors PROXY_NO_PROXY
    captured_env: dict[str, str] = {}
    monkeypatch.setenv("PROXY_NO_PROXY", "127.0.0.1,localhost,.corp.local")
    monkeypatch.setattr(
        "cproxy.services.ops.ProbeService.probe",
        lambda self, **kw: SimpleNamespace(
            switched=False,
            skip_reason="当前已是推荐稳定节点",
            current_verdict=SimpleNamespace(stable=True),
        ),
    )
    monkeypatch.setattr(
        "cproxy.services.ops.subprocess.run",
        lambda cmd, env=None, **kw: captured_env.update(env or {}) or SimpleNamespace(returncode=0),
    )
    assert guard(paths, profile="codex", command=["echo", "ok"]) == 0
    assert captured_env["NO_PROXY"] == "127.0.0.1,localhost,.corp.local"
    assert captured_env["no_proxy"] == "127.0.0.1,localhost,.corp.local"

    # config.example.yaml hardened defaults
    example_cfg = yaml.safe_load((ROOT_DIR / "config.example.yaml").read_text(encoding="utf-8"))
    assert example_cfg["allow-lan"] is False
    assert example_cfg["bind-address"] == "127.0.0.1"
    assert example_cfg["dns"]["listen"] == "127.0.0.1:1053"

    # Systemd oneshot units have hardening & no hardcoded /root/clash_proxy
    for rel in (
        "systemd/clash-proxy-traffic-collector.service",
        "systemd-user/cproxy-subscription.service",
        "systemd-user/cproxy-refresh.service",
        "systemd-user/cproxy-ai-watch.service",
        "systemd-user/cproxy-ai-rebalance.service",
    ):
        content = (ROOT_DIR / rel).read_text(encoding="utf-8")
        assert "TimeoutStartSec=" in content, rel
        assert "NoNewPrivileges=yes" in content, rel
        assert "PrivateTmp=yes" in content, rel
        assert "/root/clash_proxy" not in content, rel
    assert "After=network-online.target cproxy.service" in (
        ROOT_DIR / "systemd-user" / "cproxy-subscription.service"
    ).read_text(encoding="utf-8")
