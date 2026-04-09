from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ProxyGroup:
    name: str
    type: str
    current: str
    candidates: list[str]
    alive: bool | None = None
    delay: int | None = None
    source: str = "api"


@dataclass(frozen=True)
class QueryContext:
    groups: dict[str, ProxyGroup]
    api_available: bool
    runtime_available: bool


@dataclass(frozen=True)
class ProcessOwner:
    pid: int
    program: str
    runtime: str


@dataclass(frozen=True)
class StatusSnapshot:
    source_config: str
    runtime_config: str
    controller: str
    port: str
    runtime_ready: bool
    running: bool
    pid: int | None


@dataclass(frozen=True)
class DelayCheckResult:
    name: str
    ok: bool
    delay: int | None


@dataclass(frozen=True)
class GroupCheckReport:
    group_name: str
    results: list[DelayCheckResult]


@dataclass(frozen=True)
class ConnectivityCheckResult:
    name: str
    ok: bool
    detail: str


@dataclass(frozen=True)
class ConnectivityReport:
    results: list[ConnectivityCheckResult]
    exit_ip: str | None
