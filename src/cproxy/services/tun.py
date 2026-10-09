from __future__ import annotations

import os
import socket
import stat
import sys
import tempfile
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from ..backend.api import APIBackend, APIUnavailableError
from ..backend.process import ProcessBackend
from ..backend.runtime import RuntimeBackend
from ..config import AppPaths, config_file, load_yaml_file, read_config
from ..snapshots import snapshot_file

TUN_DEFAULTS = {
    "stack": "mixed",
    "device": "cproxy-tun",
    "auto-route": True,
    "auto-detect-interface": True,
    "dns-hijack": ["any:53", "tcp://any:53"],
    # 不额外依赖 nftables，也不强制远程主机采用严格路由。
    "auto-redirect": False,
    "strict-route": False,
}


def _section(config: dict, name: str) -> dict:
    value = config.get(name, {})
    if not isinstance(value, dict):
        raise ValueError(f"{name} 必须是映射")
    return value


def _net_admin(pid: int | None) -> bool | None:
    try:
        text = Path(f"/proc/{pid or 'self'}/status").read_text(encoding="utf-8")
        for line in text.splitlines():
            if line.startswith("CapEff:"):
                return bool(int(line.split()[1], 16) & (1 << 12))
    except (OSError, ValueError):
        pass
    return None


def _prerequisites(pid: int | None) -> list[str]:
    if sys.platform != "linux":
        return ["自动前置检查目前仅支持 Linux"]
    issues = []
    try:
        if not stat.S_ISCHR(Path("/dev/net/tun").stat().st_mode):
            issues.append("/dev/net/tun 不是字符设备")
    except OSError:
        issues.append("缺少 /dev/net/tun，请检查宿主机 TUN 驱动或容器设备映射")
    capability = _net_admin(pid)
    if capability is False:
        issues.append("Mihomo 需要 CAP_NET_ADMIN；请检查服务权限或容器 NET_ADMIN 能力")
    elif capability is None:
        issues.append("无法核验进程 CAP_NET_ADMIN")
    return issues


@dataclass
class TunStatus:
    configured_enabled: bool
    kernel_enabled: bool | None = None
    device: str = ""
    interface_present: bool | None = None
    prerequisites: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


class TunService:
    def __init__(self, paths: AppPaths):
        self.paths = paths
        self.api = APIBackend(paths)
        self.process = ProcessBackend(paths)

    def status(self) -> TunStatus:
        tun = _section(read_config(self.paths), "tun")
        process = self.process.status()
        report = TunStatus(
            configured_enabled=tun.get("enable") is True,
            device=str(tun.get("device") or ""),
            prerequisites=_prerequisites(process.pid),
        )
        if process.running:
            try:
                live = _section(self.api.get_config(), "tun")
                enabled = live.get("enable")
                report.kernel_enabled = enabled if isinstance(enabled, bool) else None
                report.device = str(live.get("device") or report.device)
            except (APIUnavailableError, ValueError):
                report.warnings.append("Mihomo API 不可访问或未返回 TUN 状态")
        else:
            report.warnings.append("cproxy 管理的 Mihomo 未运行")
        if report.kernel_enabled is None:
            report.warnings.append("内核 TUN 状态未知，配置开关不能证明流量已被接管")
        elif report.kernel_enabled != report.configured_enabled:
            report.warnings.append("配置与内核 TUN 开关不同；执行 cproxy tun on/off --apply 应用")
        if sys.platform == "linux" and report.device and report.kernel_enabled is not None:
            # sysfs 可能挂载自另一个网络命名空间，使用当前命名空间的接口查询。
            try:
                report.interface_present = report.device in {name for _, name in socket.if_nameindex()}
            except OSError:
                report.warnings.append("无法核验当前网络命名空间的 TUN 网卡")
            if report.kernel_enabled and report.interface_present is False:
                report.warnings.append("内核配置已开启 TUN，但未发现对应网卡；检查 cproxy logs")
        return report

    def configure(self, enabled: bool, *, apply: bool = False) -> TunStatus:
        path = config_file(self.paths)
        config = load_yaml_file(path)
        if not isinstance(config, dict):
            raise ValueError(f"请先初始化有效配置: {path}")
        config = deepcopy(config)
        tun = dict(_section(config, "tun"))
        if enabled:
            for key, value in TUN_DEFAULTS.items():
                tun.setdefault(key, deepcopy(value))
            dns = dict(_section(config, "dns"))
            dns["enable"] = True
            dns.setdefault("enhanced-mode", "fake-ip")
            dns.setdefault("nameserver", ["223.5.5.5", "119.29.29.29"])
            config["dns"] = dns
        tun["enable"] = enabled
        config["tun"] = tun

        if apply:
            process = self.process.status()
            if not process.running:
                raise RuntimeError("--apply 需要已运行的 cproxy Mihomo；未修改配置，请先 cproxy start")
            if enabled:
                issues = _prerequisites(process.pid)
                if issues:
                    raise RuntimeError("TUN 前置检查失败，未修改配置: " + "；".join(issues))
            # 确认 controller 可用后才保存，不回退到会中断连接的进程重启。
            self.api.get_config()

        original = path.read_bytes()
        rendered = yaml.safe_dump(config, allow_unicode=True, sort_keys=False).encode("utf-8")
        if original != rendered:
            snapshot_file(self.paths, path, "config")
            self._write_config(path, rendered)
        try:
            runtime = RuntimeBackend(self.paths).render_runtime()
        except Exception:
            if original != rendered:
                self._write_config(path, original)
            raise
        if apply:
            try:
                self.api.reload_config(str(runtime))
            except APIUnavailableError as exc:
                raise RuntimeError("TUN 配置已保存，但热重载失败；内核状态未知，请检查 cproxy tun status 和 cproxy logs") from exc
        report = self.status()
        if apply and (report.kernel_enabled is not enabled or (enabled and report.interface_present is not True)):
            raise RuntimeError("TUN 配置已保存并提交热重载，但未确认生效；请检查 cproxy tun status 和 cproxy logs")
        if not apply:
            report.warnings.append("已保存并渲染；下次启动或 refresh 会应用，立即应用需加 --apply")
        return report

    @staticmethod
    def _write_config(path: Path, content: bytes) -> None:
        fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        temporary = Path(name)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(content)
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
