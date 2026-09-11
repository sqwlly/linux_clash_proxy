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
class ConnectionEntry:
    id: str
    host: str
    process: str
    rule: str
    proxy_chain: list[str]
    upload: int
    download: int


@dataclass(frozen=True)
class ProviderEntry:
    name: str
    type: str
    vehicle: str
    proxy_count: int
    updated_at: str


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
    # 启动时 runtime 文件的内容指纹：运行实例加载的配置是否已落后于磁盘当前版本
    # 只能靠内容比对判断（runtime 路径恒定，且 render 会重写同一文件）
    runtime_hash: str = ""


@dataclass(frozen=True)
class StatusSnapshot:
    source_config: str
    runtime_config: str
    controller: str
    port: str
    runtime_ready: bool
    running: bool
    pid: int | None
    # 运行实例加载的 runtime 是否已落后于磁盘当前版本。runtime 路径恒定
    # （process.start 与 status 都取 runtime_file(paths)），因此**不能**用路径比对
    # 判断——那会恒等、永远不触发。改比内容指纹：为 True 时 status 多显示一行
    # “配置时效”，提示运行实例未跟随最近一次 render。
    runtime_stale: bool = False


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


@dataclass(frozen=True)
class AIProbeResult:
    name: str
    url: str
    ok: bool
    detail: str


@dataclass(frozen=True)
class AIProbeReport:
    results: list[AIProbeResult]
