import json
import subprocess
import sys
import tarfile
from pathlib import Path

import yaml

from cproxy.audit import audit_log_file, write_audit_event
from cproxy.backend.models import ConnectionEntry
from cproxy.config import AppPaths, default_paths
from cproxy.security import validate_controller_security
from cproxy.services.query import QueryService
from cproxy.support import build_support_bundle

ROOT_DIR = Path(__file__).resolve().parents[1]
SRC_DIR = ROOT_DIR / "src"


def test_security_report_passes_for_ga_controller_config(tmp_path):
    paths = default_paths(tmp_path)
    paths.config_dir.mkdir(parents=True)
    secret_path = paths.config_dir / "controller-secret"
    secret_path.write_text("controller-secret\n", encoding="utf-8")
    (paths.config_dir / "config.yaml").write_text(
        "external-controller: 127.0.0.1:9090\n"
        "external-controller-tls: 127.0.0.1:9443\n"
        f"secret-file: {secret_path}\n",
        encoding="utf-8",
    )

    report = validate_controller_security(paths)

    assert report.ok
    assert report.issues == []


def test_security_report_rejects_missing_secret_file(tmp_path):
    paths = default_paths(tmp_path)
    paths.config_dir.mkdir(parents=True)
    (paths.config_dir / "config.yaml").write_text(
        "external-controller-tls: 127.0.0.1:9443\n"
        f"secret-file: {paths.config_dir / 'missing-secret'}\n",
        encoding="utf-8",
    )

    report = validate_controller_security(paths)

    assert not report.ok
    assert {issue.code for issue in report.issues} == {"secret-file-missing"}


def test_security_report_rejects_non_loopback_and_missing_secret(tmp_path):
    paths = default_paths(tmp_path)
    paths.config_dir.mkdir(parents=True)
    (paths.config_dir / "config.yaml").write_text(
        "external-controller: 0.0.0.0:9090\n",
        encoding="utf-8",
    )

    report = validate_controller_security(paths)

    assert not report.ok
    assert {issue.code for issue in report.issues} >= {"non-loopback-controller", "missing-secret"}


def test_support_bundle_redacts_sensitive_values(tmp_path):
    paths = AppPaths(tmp_path / "config", tmp_path / "data", tmp_path / "state")
    paths.config_dir.mkdir(parents=True)
    paths.data_dir.mkdir(parents=True)
    paths.state_dir.mkdir(parents=True)
    (paths.config_dir / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "mixed-port": 7890,
                "secret": "super-secret",
                "subscription": "https://example.test/sub?token=super-secret",
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    (paths.data_dir / "runtime.yaml").write_text("secret: runtime-secret\n", encoding="utf-8")
    audit_log_file(paths).write_text('{"token":"super-secret","url":"https://example.test/sub?token=abc"}\n', encoding="utf-8")
    (paths.state_dir / "cproxy.log").write_text(
        "download https://example.test/sub?token=abc Authorization: Bearer abc123\n",
        encoding="utf-8",
    )

    bundle = build_support_bundle(paths, tmp_path / "bundle.tar.gz")

    with tarfile.open(bundle, "r:gz") as archive:
        config_text = archive.extractfile("config.redacted.yaml").read().decode("utf-8")
        runtime_text = archive.extractfile("runtime.redacted.yaml").read().decode("utf-8")
        audit_text = archive.extractfile("audit.redacted.jsonl").read().decode("utf-8")
        log_text = archive.extractfile("log.tail.redacted.txt").read().decode("utf-8")

    combined = "\n".join([config_text, runtime_text, audit_text, log_text])
    assert "super-secret" not in combined
    assert "runtime-secret" not in combined
    assert "token=abc" not in combined
    assert "abc123" not in combined
    assert "[REDACTED]" in combined


def test_audit_redacts_file_and_journald_events(tmp_path, monkeypatch):
    paths = default_paths(tmp_path)
    paths.config_dir.mkdir(parents=True)
    (paths.config_dir / "config.yaml").write_text("audit-journald: true\n", encoding="utf-8")

    calls = []
    monkeypatch.setattr("cproxy.audit.shutil.which", lambda name: "/usr/bin/systemd-cat")
    monkeypatch.setattr("cproxy.audit.subprocess.run", lambda *args, **kwargs: calls.append((args, kwargs)))

    write_audit_event(
        paths,
        "switch_group",
        "https://example.test/sub?token=target-secret",
        "ok",
        {"selected": "Node A", "token": "detail-secret", "error": "Authorization: Bearer abc123"},
    )

    assert calls
    assert "switch_group" in calls[0][1]["input"]
    audit_text = audit_log_file(paths).read_text(encoding="utf-8")
    journald_text = calls[0][1]["input"]
    combined = audit_text + journald_text
    assert "target-secret" not in combined
    assert "detail-secret" not in combined
    assert "abc123" not in combined


def test_security_check_cli_returns_nonzero_for_insecure_config(tmp_path):
    env = {"PYTHONPATH": str(SRC_DIR), "HOME": str(tmp_path)}
    config_dir = tmp_path / ".config" / "cproxy"
    config_dir.mkdir(parents=True)
    (config_dir / "config.yaml").write_text("external-controller: 0.0.0.0:9090\n", encoding="utf-8")

    result = subprocess.run(
        [sys.executable, "-m", "cproxy.cli", "security-check"],
        capture_output=True,
        text=True,
        cwd=ROOT_DIR,
        env=env,
    )

    assert result.returncode == 1
    assert "non-loopback-controller" in result.stdout


def test_ga_artifact_scripts_build_and_verify(tmp_path):
    out_dir = tmp_path / "ga"

    build = subprocess.run(
        ["bash", "scripts/build-ga-artifacts.sh", str(out_dir)],
        capture_output=True,
        text=True,
        cwd=ROOT_DIR,
    )
    assert build.returncode == 0, build.stderr

    verify = subprocess.run(
        ["bash", "scripts/verify-ga-artifacts.sh", str(out_dir)],
        capture_output=True,
        text=True,
        cwd=ROOT_DIR,
    )
    assert verify.returncode == 0, verify.stderr

    provenance = json.loads((out_dir / "provenance.json").read_text(encoding="utf-8"))
    assert provenance["source_archive_sha256"]
    assert (out_dir / "SHA256SUMS").is_file()


def test_connection_mapping_pressure_smoke(tmp_path):
    class FakeAPI:
        def get_connections(self):
            return {
                "connections": [
                    {
                        "id": f"conn-{idx}",
                        "metadata": {"host": f"host-{idx}.example", "process": "curl"},
                        "rule": "MATCH",
                        "chains": ["Proxy", "Node"],
                        "upload": idx,
                        "download": idx * 2,
                    }
                    for idx in range(5000)
                ]
            }

    service = QueryService(default_paths(tmp_path))
    service.api = FakeAPI()

    connections = service.list_connections()

    assert len(connections) == 5000
    assert isinstance(connections[4999], ConnectionEntry)
    assert connections[4999].host == "host-4999.example"
