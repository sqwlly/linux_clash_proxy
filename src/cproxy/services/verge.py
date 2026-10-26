"""Clash Verge (Rev) 旁路调度服务（Windows / 跨平台 Sidecar 模式）。

由 Clash Verge 负责内核生命周期、订阅拉取、TUN/系统代理与 GUI；
本模块通过 External Controller REST API 接管运行时动态调度：
1. 自动发现 Clash Verge 的 `external-controller`、`secret` 与 `mixed-port` 并生成旁路配置；
2. 导出 Clash Verge 全局扩展预处理脚本（`clash-verge-script.js`）；
3. 常驻守护循环（`cproxy verge daemon`）：高频采样 `/connections` 字节增量（Windows 免 `ss` 精确感知连接空闲时长）、
   每 20s 执行 `ai-watch` 语义看门狗与闲置 HTTP/2 连接池回收、每 30m 执行纯 API `ai-rebalance` 多轮测速与优选切流。
"""
from __future__ import annotations

import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from importlib.resources import files
from pathlib import Path

import yaml

from ..backend.api import APIBackend, APIUnavailableError
from ..config import AppPaths, config_file, load_yaml_file, read_config
from .ai_watch import AIWatchService, WatchReport, sample_connections_activity
from .rebalance import RebalanceReport, RebalanceService

DEFAULT_VERGE_CONTROLLER = "127.0.0.1:9097"
DEFAULT_VERGE_MIXED_PORT = 7897


@dataclass(frozen=True)
class VergeDetectedConfig:
    controller: str
    secret: str
    mixed_port: int
    source_dir: Path | None = None


@dataclass
class VergeTickReport:
    timestamp: float
    sampled_connections: int = 0
    watch_report: WatchReport | None = None
    rebalance_report: RebalanceReport | None = None
    warnings: list[str] = field(default_factory=list)


@dataclass
class VergeInstallReport:
    config_path: Path
    controller: str
    mixed_port: int
    secret_configured: bool
    verge_dir: Path | None = None
    injected_script_path: Path | None = None
    fallback_script_path: Path | None = None
    chain_enabled: bool = False


def candidate_clash_verge_dirs(home: Path | None = None) -> list[Path]:
    """返回 Windows / macOS / Linux 下 Clash Verge (Rev) 的标准配置目录候选。"""
    base_home = home or Path.home()
    candidates: list[Path] = []
    appdata = os.environ.get("APPDATA")
    localappdata = os.environ.get("LOCALAPPDATA")
    if appdata:
        candidates.append(Path(appdata) / "io.github.clash-verge-rev.clash-verge-rev")
        candidates.append(Path(appdata) / "clash-verge")
    if localappdata:
        candidates.append(Path(localappdata) / "io.github.clash-verge-rev.clash-verge-rev")
    candidates.extend(
        [
            base_home / "AppData" / "Roaming" / "io.github.clash-verge-rev.clash-verge-rev",
            base_home / ".config" / "io.github.clash-verge-rev.clash-verge-rev",
            base_home / ".local" / "share" / "io.github.clash-verge-rev.clash-verge-rev",
            base_home / "Library" / "Application Support" / "io.github.clash-verge-rev.clash-verge-rev",
            base_home / ".config" / "clash-verge",
        ]
    )
    deduped: list[Path] = []
    seen: set[str] = set()
    for path in candidates:
        key = str(path)
        if key not in seen:
            seen.add(key)
            deduped.append(path)
    return deduped


def discover_clash_verge_config(
    verge_dir: Path | None = None,
    *,
    home: Path | None = None,
) -> VergeDetectedConfig:
    """从 Clash Verge 目录的 `clash-verge.yaml` / `verge.yaml` / `config.yaml` 自动读取控制器与端口配置。"""
    dirs = [verge_dir] if verge_dir is not None else candidate_clash_verge_dirs(home=home)
    for d in dirs:
        if d is None or not d.is_dir():
            continue
        runtime_cfg = load_yaml_file(d / "clash-verge.yaml") or load_yaml_file(d / "config.yaml") or {}
        verge_cfg = load_yaml_file(d / "verge.yaml") or {}
        if not isinstance(runtime_cfg, dict):
            runtime_cfg = {}
        if not isinstance(verge_cfg, dict):
            verge_cfg = {}
        if not runtime_cfg and not verge_cfg:
            continue

        controller = str(
            runtime_cfg.get("external-controller")
            or verge_cfg.get("clash_core_external_controller")
            or DEFAULT_VERGE_CONTROLLER
        ).strip()
        secret = str(
            runtime_cfg.get("secret")
            or verge_cfg.get("clash_core_secret")
            or ""
        ).strip()
        raw_port = (
            runtime_cfg.get("mixed-port")
            or verge_cfg.get("verge_mixed_port")
            or runtime_cfg.get("port")
            or DEFAULT_VERGE_MIXED_PORT
        )
        try:
            mixed_port = int(raw_port)
        except (TypeError, ValueError):
            mixed_port = DEFAULT_VERGE_MIXED_PORT
        return VergeDetectedConfig(
            controller=controller,
            secret=secret,
            mixed_port=mixed_port,
            source_dir=d,
        )

    return VergeDetectedConfig(
        controller=DEFAULT_VERGE_CONTROLLER,
        secret="",
        mixed_port=DEFAULT_VERGE_MIXED_PORT,
        source_dir=None,
    )


def init_verge_config(
    paths: AppPaths,
    *,
    controller: str | None = None,
    secret: str | None = None,
    mixed_port: int | None = None,
    verge_dir: Path | None = None,
) -> tuple[Path, VergeDetectedConfig]:
    """初始化或更新供 Clash Verge 旁路模式使用的 `config.yaml`。"""
    detected = discover_clash_verge_config(verge_dir)
    eff_controller = (controller or detected.controller or DEFAULT_VERGE_CONTROLLER).strip()
    eff_secret = secret if secret is not None else detected.secret
    eff_port = int(mixed_port) if mixed_port is not None else detected.mixed_port

    cfg_path = config_file(paths)
    existing = read_config(paths) if cfg_path.exists() else {}
    merged = dict(existing) if isinstance(existing, dict) else {}
    merged["verge-mode"] = True
    merged["external-controller"] = eff_controller
    merged["secret"] = eff_secret
    merged["mixed-port"] = eff_port

    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    rendered = yaml.safe_dump(merged, allow_unicode=True, sort_keys=False, default_flow_style=False)
    tmp_path = cfg_path.with_suffix(".tmp")
    tmp_path.write_text(rendered, encoding="utf-8")
    os.replace(tmp_path, cfg_path)

    final_detected = VergeDetectedConfig(
        controller=eff_controller,
        secret=eff_secret,
        mixed_port=eff_port,
        source_dir=detected.source_dir,
    )
    return cfg_path, final_detected


def load_verge_preprocess_script() -> str:
    """读取内置的 Clash Verge 全局扩展脚本 `clash-verge-script.js`。"""
    try:
        res = files("cproxy").joinpath("data/verge/clash-verge-script.js")
        return res.read_text(encoding="utf-8")
    except Exception:
        repo_fallback = Path(__file__).resolve().parents[3] / "scripts" / "clash-verge-script.js"
        return repo_fallback.read_text(encoding="utf-8")


def export_verge_script(output_path: Path | None = None) -> str:
    """导出 Clash Verge 全局扩展脚本内容；若给定 output_path 则写入文件。"""
    content = load_verge_preprocess_script()
    if output_path is not None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(content, encoding="utf-8")
    return content


def inject_verge_global_script(verge_dir: Path) -> tuple[Path, bool]:
    """将全局扩展脚本直接注入 Clash Verge 的 `profiles/Script.js`，并在 `profiles.yaml` 的 `chain` 中自动启用。

    返回 `(写入的 JS 路径, 是否自动启用了 chain)`。
    """
    content = load_verge_preprocess_script()
    profiles_yaml = verge_dir / "profiles.yaml"
    script_uid = "Script"
    script_filename = "Script.js"
    chain_enabled = False

    if profiles_yaml.exists():
        raw_profiles = load_yaml_file(profiles_yaml)
        if isinstance(raw_profiles, dict):
            profiles_data = dict(raw_profiles)
            items = profiles_data.get("items")
            items_list = list(items) if isinstance(items, list) else []
            found_item = None
            for item in items_list:
                if not isinstance(item, dict):
                    continue
                itype = str(item.get("type") or "").lower()
                iuid = str(item.get("uid") or "")
                ifile = str(item.get("file") or "")
                if itype == "script" or iuid == "Script" or ifile == "Script.js":
                    found_item = item
                    break
            modified = False
            if found_item is not None:
                script_uid = str(found_item.get("uid") or "Script")
                script_filename = str(found_item.get("file") or "Script.js")
            else:
                items_list.append(
                    {
                        "uid": script_uid,
                        "type": "script",
                        "name": "cproxy AI Global Script",
                        "file": script_filename,
                    }
                )
                profiles_data["items"] = items_list
                modified = True

            chain = profiles_data.get("chain")
            chain_list = list(chain) if isinstance(chain, list) else []
            if script_uid not in chain_list:
                chain_list.append(script_uid)
                profiles_data["chain"] = chain_list
                modified = True
            chain_enabled = True

            if modified:
                bak_yaml = profiles_yaml.with_suffix(".yaml.bak")
                if not bak_yaml.exists():
                    try:
                        bak_yaml.write_bytes(profiles_yaml.read_bytes())
                    except OSError:
                        pass
                rendered = yaml.safe_dump(profiles_data, allow_unicode=True, sort_keys=False, default_flow_style=False)
                tmp_yaml = profiles_yaml.with_suffix(".yaml.tmp")
                tmp_yaml.write_text(rendered, encoding="utf-8")
                os.replace(tmp_yaml, profiles_yaml)

    profiles_dir = verge_dir / "profiles"
    profiles_dir.mkdir(parents=True, exist_ok=True)
    target_js = profiles_dir / script_filename
    if target_js.exists():
        try:
            old_js = target_js.read_text(encoding="utf-8", errors="replace")
            if old_js.strip() and old_js != content:
                bak_js = target_js.with_suffix(".js.bak")
                if not bak_js.exists():
                    bak_js.write_text(old_js, encoding="utf-8")
        except OSError:
            pass
    target_js.write_text(content, encoding="utf-8")
    return target_js, chain_enabled


def install_verge_oneclick(
    paths: AppPaths,
    *,
    controller: str | None = None,
    secret: str | None = None,
    mixed_port: int | None = None,
    verge_dir: Path | None = None,
) -> VergeInstallReport:
    """一键完成 Clash Verge 旁路配置初始化 + 全局扩展脚本自动注入。"""
    cfg_path, detected = init_verge_config(
        paths,
        controller=controller,
        secret=secret,
        mixed_port=mixed_port,
        verge_dir=verge_dir,
    )
    fallback_js = paths.config_dir / "clash-verge-script.js"
    export_verge_script(fallback_js)

    injected_js: Path | None = None
    chain_enabled = False
    target_dir = verge_dir or detected.source_dir
    if target_dir is not None and target_dir.is_dir():
        try:
            injected_js, chain_enabled = inject_verge_global_script(target_dir)
        except OSError:
            injected_js = None

    return VergeInstallReport(
        config_path=cfg_path,
        controller=detected.controller,
        mixed_port=detected.mixed_port,
        secret_configured=bool(detected.secret),
        verge_dir=target_dir,
        injected_script_path=injected_js,
        fallback_script_path=fallback_js,
        chain_enabled=chain_enabled,
    )


class VergeDaemonService:
    """Clash Verge 旁路守护进程：周期采样连接时序、执行语义看门狗与稳定度重排。"""

    def __init__(self, paths: AppPaths):
        self.paths = paths

    def run_once(
        self,
        *,
        run_rebalance: bool = True,
        dry_run: bool = False,
    ) -> VergeTickReport:
        now = time.time()
        report = VergeTickReport(timestamp=now)
        api = APIBackend(self.paths)
        idle_map = sample_connections_activity(self.paths, api, now=now)
        report.sampled_connections = len(idle_map)

        watch_svc = AIWatchService(self.paths)
        report.watch_report = watch_svc.watch(dry_run=dry_run)

        if run_rebalance:
            try:
                report.rebalance_report = RebalanceService(self.paths).rebalance(
                    dry_run=dry_run,
                    external_only=True,
                )
            except APIUnavailableError as exc:
                report.warnings.append(str(exc))
            except Exception as exc:
                report.warnings.append(f"rebalance 跳过: {exc}")
        return report

    def run_daemon(
        self,
        *,
        watch_interval: float = 20.0,
        rebalance_interval: float = 1800.0,
        sample_interval: float = 3.0,
        dry_run: bool = False,
        max_ticks: int | None = None,
        sleep_fn: Callable[[float], None] = time.sleep,
        on_tick: Callable[[VergeTickReport], None] | None = None,
    ) -> int:
        watch_sec = max(5.0, float(watch_interval))
        rebalance_sec = max(60.0, float(rebalance_interval))
        sample_sec = max(1.0, min(float(sample_interval), watch_sec))

        last_watch_ts = 0.0
        last_rebalance_ts = 0.0
        ticks = 0
        api = APIBackend(self.paths)

        try:
            while max_ticks is None or ticks < max_ticks:
                now = time.time()
                sample_connections_activity(self.paths, api, now=now)

                due_watch = (now - last_watch_ts) >= watch_sec
                due_rebalance = (now - last_rebalance_ts) >= rebalance_sec
                if due_watch or due_rebalance:
                    tick_report = VergeTickReport(timestamp=now)
                    if due_watch:
                        tick_report.watch_report = AIWatchService(self.paths).watch(dry_run=dry_run)
                        last_watch_ts = now
                    if due_rebalance:
                        try:
                            tick_report.rebalance_report = RebalanceService(self.paths).rebalance(
                                dry_run=dry_run,
                                external_only=True,
                            )
                        except Exception as exc:
                            tick_report.warnings.append(str(exc))
                        last_rebalance_ts = now
                    if on_tick is not None:
                        on_tick(tick_report)

                ticks += 1
                if max_ticks is not None and ticks >= max_ticks:
                    break
                sleep_fn(sample_sec)
        except KeyboardInterrupt:
            return 0
        return 0
