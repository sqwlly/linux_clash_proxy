from __future__ import annotations

import os
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

    controller = str(config.get("external-controller-tls") or config.get("external-controller") or "127.0.0.1:9090")
    host = _controller_host(controller)
    if host and not _is_loopback_host(host):
        issues.append(
            SecurityIssue(
                "error",
                "non-loopback-controller",
                f"controller host is not loopback: {host}",
            )
        )

    if not config.get("external-controller-tls"):
        issues.append(SecurityIssue("warning", "missing-controller-tls", "external-controller-tls is not configured"))

    secret_issues, has_configured_secret = _validate_secret_config(config)
    issues.extend(secret_issues)
    if not has_configured_secret:
        issues.append(SecurityIssue("error", "missing-secret", "controller secret is not configured"))

    return SecurityReport(issues)


def _controller_host(controller: str) -> str:
    parsed = urlparse(controller if "://" in controller else f"//{controller}")
    return parsed.hostname or ""


def _is_loopback_host(host: str) -> bool:
    return host in {"127.0.0.1", "::1", "localhost"}


def _validate_secret_config(config: dict) -> tuple[list[SecurityIssue], bool]:
    issues: list[SecurityIssue] = []
    has_configured_secret = False

    credential_name = str(config.get("secret-systemd-credential", "") or "").strip()
    if credential_name:
        has_configured_secret = True
        credentials_dir = os.environ.get("CREDENTIALS_DIRECTORY")
        if credentials_dir:
            _check_secret_file(Path(credentials_dir) / credential_name, "systemd-credential", issues)
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
        _check_secret_file(Path(secret_file).expanduser(), "secret-file", issues)

    keyring_service = str(config.get("secret-keyring-service", "") or "").strip()
    if keyring_service:
        has_configured_secret = True
        keyring_username = str(config.get("secret-keyring-username", "controller") or "controller")
        _check_keyring_secret(keyring_service, keyring_username, issues)

    if config.get("secret"):
        has_configured_secret = True
        if not (credential_name or secret_file or keyring_service):
            issues.append(SecurityIssue("warning", "inline-secret", "secret is stored directly in YAML"))

    return issues, has_configured_secret


def _check_secret_file(path: Path, code_prefix: str, issues: list[SecurityIssue]) -> None:
    try:
        secret = path.read_text(encoding="utf-8").strip()
    except OSError:
        issues.append(SecurityIssue("error", f"{code_prefix}-missing", f"controller secret file is not readable: {path}"))
        return
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
