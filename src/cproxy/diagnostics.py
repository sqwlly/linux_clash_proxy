from __future__ import annotations

from .backend.models import AIProbeReport, ConnectivityCheckResult, ConnectivityReport, DelayCheckResult, GroupCheckReport
from .config import AppPaths
from .services.diagnostics import DiagnosticsService


def test_group(paths: AppPaths, group_name: str) -> GroupCheckReport:
    return DiagnosticsService(paths).test_group(group_name)


def run_connectivity_test(paths: AppPaths) -> ConnectivityReport:
    return DiagnosticsService(paths).run_connectivity_test()


def run_ai_probe(paths: AppPaths) -> AIProbeReport:
    return DiagnosticsService(paths).run_ai_probe()
