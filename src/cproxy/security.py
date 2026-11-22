from __future__ import annotations

import ipaddress
import os
import stat
from dataclasses import dataclass
from importlib import import_module
from pathlib import Path
from urllib.parse import urlparse

from .config import AppPaths, read_config


@dataclass(frozen=True)
class SecurityIssue:
    severity: str
    code: str
    detail: str


@dataclass(frozen=True)
class SecurityReport:
    issues: list[SecurityIssue]

    @property
    def ok(self) -> bool:
        return not any(issue.severity == "error" for issue in self.issues)


def validate_controller_security(paths: AppPaths) -> SecurityReport:
    config = read_config(paths)
    issues: list[SecurityIssue] = []

    if config.get("external-controller-unix"):
        issues.append(
            SecurityIssue(
                "error",
                "unix-controller",
                "external-controller-unix is rejected because Mihomo does not verify secret for Unix socket API access",
            )
        )

    if config.get("external-controller-pipe"):
        issues.append(
            SecurityIssue(
                "error",
                "pipe-controller",
                "external-controller-pipe is rejected because Mihomo named pipe API may bypass secret verification",
            )
        )

    checked_any_controller = False
    for key in ("external-controller", "external-controller-tls"):
        raw_val = config.get(key)
        if raw_val is None:
            continue
        controller = str(raw_val).strip()
        if not controller:
            continue
        checked_any_controller = True
        host = _controller_host(controller)
        if not host or not _is_loopback_host(host):
            issues.append(
                SecurityIssue(
                    "error",
                    "non-loopback-controller",
                    f"{key} host is not loopback: {host or controller}",
                )
            )

    if not checked_any_controller:
        # Mihomo/cproxy defaults to 127.0.0.1:9090 when neither controller key is explicitly set
        host = _controller_host("127.0.0.1:9090")
        if not host or not _is_loopback_host(host):
            issues.append(
                SecurityIssue(
                    "error",
                    "non-loopback-controller",
                    f"controller host is not loopback: {host}",
                )
            )

    if not config.get("external-controller-tls"):
        issues.append(SecurityIssue("warning", "missing-controller-tls", "external-controller-tls is not configured"))

    if bool(config.get("allow-lan")):
        bind_addr = str(config.get("bind-address", "*") or "*").strip()
        if bind_addr in {"*", "0.0.0.0", "::"}:
            issues.append(
                SecurityIssue(
                    "warning",
                    "allow-lan-wildcard",
                    f"allow-lan is enabled with wildcard bind-address ({bind_addr}); restrict bind-address or disable allow-lan",
                )
            )

    secret_issues, has_configured_secret = _validate_secret_config(config)
    issues.extend(secret_issues)
    if not has_configured_secret:
        issues.append(SecurityIssue("error", "missing-secret", "controller secret is not configured"))

    return SecurityReport(issues)


def _controller_host(controller: str) -> str:
    raw = controller.strip()
    if not raw or raw.startswith(":"):
        return ""
    try:
        parsed = urlparse(raw if "://" in raw else f"//{raw}")
        return parsed.hostname or ""
    except ValueError:
        return raw


def _is_loopback_host(host: str) -> bool:
    normalized = host.strip().lower()
    if not normalized or normalized == "*":
        return False
    if normalized == "localhost":
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def _validate_secret_config(config: dict) -> tuple[list[SecurityIssue], bool]:
    issues: list[SecurityIssue] = []
    has_configured_secret = False

    credential_name = str(config.get("secret-systemd-credential", "") or "").strip()
    if credential_name:
        if (
            "/" in credential_name
            or "\\" in credential_name
            or credential_name in {".", ".."}
            or ".." in Path(credential_name).parts
        ):
            issues.append(
                SecurityIssue(
                    "error",
                    "systemd-credential-invalid",
                    f"secret-systemd-credential must not contain path separators or '..': {credential_name}",
                )
            )
        else:
            has_configured_secret = True
            credentials_dir = os.environ.get("CREDENTIALS_DIRECTORY")
            if credentials_dir:
                _check_secret_file(
                    Path(credentials_dir) / credential_name,
                    "systemd-credential",
                    issues,
                    check_permissions=False,
                )
            else:
                issues.append(
                    SecurityIssue(
                        "warning",
                        "systemd-credential-not-verifiable",
                        "secret-systemd-credential is configured, but CREDENTIALS_DIRECTORY is not set for this check",
                    )
                )

    secret_file = str(config.get("secret-file", "") or "").strip()
    if secret_file:
        has_configured_secret = True
        _check_secret_file(Path(secret_file).expanduser(), "secret-file", issues, check_permissions=True)

    keyring_service = str(config.get("secret-keyring-service", "") or "").strip()
    if keyring_service:
        has_configured_secret = True
        keyring_username = str(config.get("secret-keyring-username", "controller") or "controller")
        _check_keyring_secret(keyring_service, keyring_username, issues)

    if config.get("secret"):
        has_configured_secret = True
        issues.append(SecurityIssue("warning", "inline-secret", "secret is stored directly in YAML"))

    return issues, has_configured_secret


def _check_secret_file(
    path: Path,
    code_prefix: str,
    issues: list[SecurityIssue],
    *,
    check_permissions: bool = True,
) -> None:
    try:
        st = path.stat()
        secret = path.read_text(encoding="utf-8").strip()
    except OSError:
        issues.append(SecurityIssue("error", f"{code_prefix}-missing", f"controller secret file is not readable: {path}"))
        return
    if check_permissions and os.name != "nt":
        mode = stat.S_IMODE(st.st_mode)
        if mode & 0o077:
            issues.append(
                SecurityIssue(
                    "error",
                    f"{code_prefix}-permissions",
                    f"controller secret file permissions are too open ({oct(mode)}): {path}; expected 0o600",
                )
            )
    if not secret:
        issues.append(SecurityIssue("error", f"{code_prefix}-empty", f"controller secret file is empty: {path}"))


def _check_keyring_secret(service: str, username: str, issues: list[SecurityIssue]) -> None:
    try:
        keyring = import_module("keyring")
    except ImportError:
        issues.append(
            SecurityIssue(
                "error",
                "keyring-unavailable",
                "secret-keyring-service is configured, but keyring is not installed",
            )
        )
        return

    try:
        secret = keyring.get_password(service, username)
    except Exception as exc:
        issues.append(SecurityIssue("error", "keyring-error", f"keyring secret lookup failed: {exc}"))
        return
    if not secret:
        issues.append(SecurityIssue("error", "keyring-secret-missing", "keyring did not return a controller secret"))
