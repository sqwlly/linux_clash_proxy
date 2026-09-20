from __future__ import annotations

from dataclasses import asdict, dataclass

from .backend.api import APIBackend
from .config import AppPaths, config_file, load_yaml_file, runtime_file
from .geodata import check_country_mmdb
from .process import get_status


@dataclass(frozen=True)
class DoctorCheck:
    name: str
    status: str
    detail: str
    action: str = ""


def run_doctor(paths: AppPaths) -> dict:
    checks: list[DoctorCheck] = []
    source = config_file(paths)
    runtime = runtime_file(paths)

    if not source.is_file():
        checks.append(DoctorCheck("原始配置", "失败", f"文件不存在: {source}", "cproxy init"))
    else:
        try:
            data = load_yaml_file(source)
            if not isinstance(data, dict):
                raise ValueError("顶层必须是映射")
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

    failed = [item for item in checks if item.status == "失败"]
    actions = list(dict.fromkeys(item.action for item in failed if item.action))
    return {
        "ok": not failed,
        "checks": [asdict(item) for item in checks],
        "recommended_actions": actions,
    }
