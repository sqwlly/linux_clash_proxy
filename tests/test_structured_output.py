from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from cproxy.output import build_root_parser

ROOT_DIR = Path(__file__).resolve().parents[1]
SRC_DIR = ROOT_DIR / "src"


def _run_cli(tmp_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["HOME"] = str(tmp_path)
    env["PYTHONPATH"] = str(SRC_DIR)
    config_dir = tmp_path / ".config" / "cproxy"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "config.yaml").write_text(
        "mixed-port: 7890\nexternal-controller: 127.0.0.1:1\nproxy-groups: []\nproxies: []\nrules: []\n",
        encoding="utf-8",
    )
    return subprocess.run(
        [sys.executable, "-m", "cproxy.cli", *args],
        cwd=ROOT_DIR,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )


def test_status_json_has_stable_envelope_and_recovery_actions(tmp_path):
    result = _run_cli(tmp_path, "status", "--json")

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["schema_version"] == 1
    assert payload["command"] == "status"
    assert payload["ok"] is False
    assert "cproxy render" in payload["recommended_actions"]
    assert payload["data"]["running"] is False
    assert payload["data"]["api_available"] is False


def test_doctor_json_is_machine_readable_even_when_checks_fail(tmp_path):
    result = _run_cli(tmp_path, "doctor", "--json")

    assert result.returncode == 1
    payload = json.loads(result.stdout)
    assert payload["schema_version"] == 1
    assert payload["command"] == "doctor"
    assert payload["ok"] is False
    assert payload["data"]["checks"]
    assert payload["recommended_actions"]


def test_raw_and_json_are_mutually_exclusive():
    parser = build_root_parser()

    with pytest.raises(SystemExit) as exc_info:
        parser.parse_args(["status", "--raw", "--json"])

    assert exc_info.value.code == 2
