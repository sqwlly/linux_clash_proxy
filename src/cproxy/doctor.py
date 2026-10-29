from __future__ import annotations

import time
from dataclasses import asdict, dataclass

from .backend.api import APIBackend
from .config import AppPaths, config_file, load_yaml_file, runtime_file
from .geodata import check_country_mmdb
from .process import get_status
from .services.subscription_info import (
    MAIN_SUBSCRIPTION_KEY,
    load_subscription_info,
    parse_panel_info_nodes,
)

LOW_QUOTA_FAIL_BYTES = 1 * 1024**3
LOW_QUOTA_WARN_BYTES = 10 * 1024**3


@dataclass(frozen=True)
class DoctorCheck:
    name: str
    status: str
    detail: str
    action: str = ""


def _subscription_doctor_check(paths: AppPaths, source_data: dict | None) -> DoctorCheck | None:
    entries = load_subscription_info(paths)
    usage = entries.get(MAIN_SUBSCRIPTION_KEY)
    if usage is None and isinstance(source_data, dict):
        usage = parse_panel_info_nodes(source_data.get("proxies"))
    if usage is None:
        return None
    remaining = usage.remaining
    now_ts = int(time.time())
    if usage.expire and usage.expire < now_ts:
        return DoctorCheck("订阅余量", "失败", "订阅已过期", "更新订阅套餐后执行 cproxy refresh")
    if remaining is not None:
        gb = remaining / (1024**3)
        if remaining <= LOW_QUOTA_FAIL_BYTES:
            return DoctorCheck("订阅余量", "失败", f"剩余流量极低 ({gb:.2f} GB)", "cproxy refresh")
        if remaining < LOW_QUOTA_WARN_BYTES:
            return DoctorCheck("订阅余量", "部分异常", f"剩余流量偏低 ({gb:.2f} GB)", "留意套餐余量")
        return DoctorCheck("订阅余量", "正常", f"剩余 {gb:.2f} GB")
    return None


def _scheduler_doctor_check(paths: AppPaths) -> DoctorCheck | None:
    try:
        from .services.verge import collect_scheduler_telemetry

        telemetry = collect_scheduler_telemetry(paths)
    except Exception:
        return None
    if not telemetry.get("configured"):
        return None
    mode = telemetry.get("scheduler_mode")
    installed = telemetry.get("installed_timers") or []
    active = telemetry.get("active_timers") or []
    age = telemetry.get("heartbeat_age_sec")
    age_text = f"最近看护 {age}s 前" if age is not None else "尚无看护记录"
    if telemetry.get("daemon_ok"):
        if mode == "systemd-timer" and installed:
            detail = f"systemd 定时器 {len(active)}/{len(installed)} 活跃 · {age_text}"
        elif telemetry.get("daemon_pid"):
            detail = f"守护进程 PID {telemetry['daemon_pid']} · {age_text}"
        else:
            detail = age_text
        return DoctorCheck("调度看护", "正常", detail)
    if installed:
        return DoctorCheck(
            "调度看护",
            "失败",
            f"systemd 定时器 {len(active)}/{len(installed)} 活跃 · {age_text}",
            "systemctl --user enable --now cproxy-ai-watch.timer cproxy-ai-rebalance.timer",
        )
    return DoctorCheck("调度看护", "失败", f"看护守护未运行或心跳超时 ({age_text})", "cproxy verge daemon")


def run_doctor(paths: AppPaths) -> dict:
    checks: list[DoctorCheck] = []
    source = config_file(paths)
    runtime = runtime_file(paths)
    source_data: dict | None = None

    if not source.is_file():
        checks.append(DoctorCheck("原始配置", "失败", f"文件不存在: {source}", "cproxy init"))
    else:
        try:
            data = load_yaml_file(source)
            if not isinstance(data, dict):
                raise ValueError("顶层必须是映射")
            source_data = data
            checks.append(DoctorCheck("原始配置", "正常", str(source)))
        except Exception as exc:
            checks.append(DoctorCheck("原始配置", "失败", f"YAML 无效: {exc}", f"编辑 {source}"))

    if runtime.is_file():
        checks.append(DoctorCheck("运行配置", "正常", str(runtime)))
    else:
        checks.append(DoctorCheck("运行配置", "失败", f"文件不存在: {runtime}", "cproxy render"))

    try:
        snapshot = get_status(paths)
    except Exception as exc:
        snapshot = None
        checks.append(DoctorCheck("代理状态", "失败", f"无法读取状态: {exc}", "先修复原始配置，再执行 cproxy doctor"))
    if snapshot is not None:
        if snapshot.running:
            checks.append(DoctorCheck("代理进程", "正常", "运行中"))
        else:
            checks.append(DoctorCheck("代理进程", "失败", "未运行", "cproxy start"))
        if snapshot.runtime_stale:
            checks.append(DoctorCheck("配置时效", "失败", "运行实例未应用最新运行配置", "cproxy restart"))
        elif snapshot.runtime_ready:
            checks.append(DoctorCheck("配置时效", "正常", "运行实例与磁盘配置一致"))

    try:
        version = APIBackend(paths).version()
        version_text = str(version.get("version") or version.get("meta") or "可访问") if isinstance(version, dict) else "可访问"
        checks.append(DoctorCheck("Mihomo API", "正常", version_text))
    except Exception as exc:
        checks.append(DoctorCheck("Mihomo API", "失败", str(exc), "cproxy logs --lines 100"))

    geodata = check_country_mmdb(paths)
    checks.append(
        DoctorCheck(
            "GeoIP 数据",
            "正常" if geodata.ok else "失败",
            geodata.detail,
            "将 country.mmdb 放入 cproxy 数据目录后执行 cproxy test" if not geodata.ok else "",
        )
    )

    sub_check = _subscription_doctor_check(paths, source_data)
    if sub_check is not None:
        checks.append(sub_check)

    sched_check = _scheduler_doctor_check(paths)
    if sched_check is not None:
        checks.append(sched_check)

    failed = [item for item in checks if item.status == "失败"]
    actions = list(dict.fromkeys(item.action for item in failed if item.action))
    return {
        "ok": not failed,
        "checks": [asdict(item) for item in checks],
        "recommended_actions": actions,
    }
