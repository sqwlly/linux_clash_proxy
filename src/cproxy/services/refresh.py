from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from urllib.request import Request, urlopen

import time

import yaml

from .. import __version__
from ..backend.api import APIUnavailableError
from ..backend.models import GroupCheckReport
from ..backend.process import ProcessBackend
from ..backend.runtime import RuntimeBackend
from ..config import AppPaths, config_file, read_config
from ..redaction import redact_text
from ..snapshots import snapshot_file
from .diagnostics import DiagnosticsService
from .query import QueryService

SUBSCRIPTION_MAX_BYTES = 4 * 1024 * 1024
SUBSCRIPTION_TIMEOUT = 20
API_READY_TIMEOUT = 5.0
API_READY_INTERVAL = 0.2

# 安全相关键：订阅内容一律剔除，防止恶意/被劫持订阅注入
# （例如 program-path 会让下次 cproxy start 执行攻击者指定的二进制）
SUBSCRIPTION_STRIP_KEYS = {
    "program-path",
    "api-timeout",
    "external-controller",
    "external-controller-tls",
    "external-controller-unix",
    "secret",
    "secret-file",
    "secret-systemd-credential",
    "secret-keyring-service",
    "secret-keyring-username",
    "audit-journald",
    "allow-lan",
    "bind-address",
}

# 本地优先键：本地配置已存在时保留本地值；本地缺失时接受订阅提供的值
LOCAL_PREFERRED_KEYS = {
    "mixed-port",
    "port",
    "mode",
    "log-level",
    "output-color",
    "output-icons",
    "test-url",
    "test-timeout",
    "connectivity-timeout",
    "connectivity-test-urls",
    "ip-check-urls",
    "ai-chatgpt-url",
    "ai-openai-api-url",
    "refresh-groups",
}


@dataclass
class GroupSwitchResult:
    group: str
    current: str | None
    action: str
    target: str | None = None
    detail: str = ""


@dataclass
class RefreshReport:
    subscription: str
    subscription_detail: str = ""
    runtime_path: Path | None = None
    was_running: bool = False
    restarted: bool = False
    groups: list[GroupSwitchResult] = field(default_factory=list)


def update_source_from_subscription(paths: AppPaths, url: str, timeout: int = SUBSCRIPTION_TIMEOUT) -> Path:
    """下载订阅并合并进原始配置；订阅内容覆盖节点/规则，本地环境键保留。"""
    request = Request(url, headers={"User-Agent": f"cproxy/{__version__}"})
    with urlopen(request, timeout=timeout) as response:
        raw = response.read(SUBSCRIPTION_MAX_BYTES + 1)
    if len(raw) > SUBSCRIPTION_MAX_BYTES:
        raise RuntimeError("错误: 订阅内容超过大小限制")

    data = yaml.safe_load(raw.decode("utf-8"))
    if not isinstance(data, dict) or not (data.get("proxies") or data.get("proxy-groups")):
        raise RuntimeError("错误: 订阅内容不是有效的 Clash/Mihomo 配置")

    path = config_file(paths)
    existing = read_config(paths)
    # 订阅数据先剔除安全相关键；本地已存在的值（含安全键与本地优先键）一律保留。
    # 区别仅在于本地缺失时：安全键保持缺失，本地优先键接受订阅值。
    merged = {key: value for key, value in data.items() if key not in SUBSCRIPTION_STRIP_KEYS}
    for key in SUBSCRIPTION_STRIP_KEYS | LOCAL_PREFERRED_KEYS:
        if key in existing:
            merged[key] = existing[key]
    merged["subscription-url"] = url

    snapshot_file(paths, path, "config")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        yaml.safe_dump(merged, fh, allow_unicode=True, sort_keys=False)
    return path


def _config_groups(value: object) -> list[str]:
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, list):
        return [str(item) for item in value]
    return []


class RefreshService:
    def __init__(self, paths: AppPaths):
        self.paths = paths
        self.process = ProcessBackend(paths)
        self.query = QueryService(paths)
        self.diagnostics = DiagnosticsService(paths)

    def refresh(self, subscription_url: str | None = None, groups: list[str] | None = None) -> RefreshReport:
        config = read_config(self.paths)
        url = (subscription_url or str(config.get("subscription-url") or "")).strip()
        if groups:
            target_groups = list(groups)
        else:
            target_groups = _config_groups(config.get("refresh-groups"))

        report = RefreshReport(subscription="跳过", subscription_detail="未配置订阅地址")
        if url:
            try:
                update_source_from_subscription(self.paths, url)
                report.subscription = "已更新"
                report.subscription_detail = redact_text(url)
            except Exception as exc:
                # 订阅失败不阻断后续 render/探测，避免定时任务因订阅站波动整体失效
                report.subscription = "失败"
                report.subscription_detail = redact_text(str(exc))

        report.runtime_path = RuntimeBackend(self.paths).render_runtime()

        report.was_running = self.process.is_running()
        if report.was_running:
            self.process.restart()
            report.restarted = True

        if target_groups and report.restarted:
            self._wait_for_api()
        for name in target_groups:
            if report.restarted:
                report.groups.append(self._probe_and_switch(name))
            else:
                report.groups.append(GroupSwitchResult(group=name, current=None, action="跳过", detail="代理未运行"))
        return report

    def _wait_for_api(self) -> None:
        deadline = time.monotonic() + API_READY_TIMEOUT
        while True:
            try:
                self.query.api.get_groups()
                return
            except APIUnavailableError:
                if time.monotonic() >= deadline:
                    raise RuntimeError("错误: 代理重启后 Mihomo API 在预期时间内未就绪")
                time.sleep(API_READY_INTERVAL)

    def _probe_and_switch(self, group_name: str) -> GroupSwitchResult:
        try:
            group = self.query.get_group(group_name, require_api=True)
        except SystemExit as exc:
            # QueryService 对未知组抛 SystemExit；逐组容错，不中断后续组的探测
            return GroupSwitchResult(group=group_name, current=None, action="失败", detail=str(exc))
        group_type = str(group.type or "").lower()
        if group_type not in {"select", "selector"}:
            return GroupSwitchResult(group=group_name, current=group.current, action="保持不变", detail=f"{group.type} 类型自动选路")

        check: GroupCheckReport = self.diagnostics.test_group(group_name)
        ok_items = [item for item in check.results if item.ok and item.delay is not None]
        if not ok_items:
            return GroupSwitchResult(group=group_name, current=group.current, action="保持不变", detail="无可用节点")

        current_check = next((item for item in check.results if item.name == group.current), None)
        if current_check is not None and current_check.ok:
            return GroupSwitchResult(group=group_name, current=group.current, action="保持不变", detail=f"{current_check.delay}ms")

        best = min(ok_items, key=lambda item: item.delay)
        self.query.switch_group(group_name, best.name)
        return GroupSwitchResult(group=group_name, current=group.current, action="已切换", target=best.name, detail=f"{best.delay}ms")
