"""Clash Verge (Rev) 旁路调度服务（Windows / 跨平台 Sidecar 模式）。

由 Clash Verge 负责内核生命周期、订阅拉取、TUN/系统代理与 GUI；
本模块通过 External Controller REST API 接管运行时动态调度：
1. 自动发现 Clash Verge 的 `external-controller`、`secret` 与 `mixed-port` 并生成旁路配置；
2. 导出 Clash Verge 全局扩展预处理脚本（`clash-verge-script.js`）；
3. 常驻守护循环（`cproxy verge daemon`）：高频采样 `/connections` 字节增量（Windows 免 `ss` 精确感知连接空闲时长）、
   每 20s 执行 `ai-watch` 语义看门狗与闲置 HTTP/2 连接池回收、每 30m 执行纯 API `ai-rebalance` 多轮测速与优选切流。
"""
from __future__ import annotations

import json
import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from importlib.resources import files
from pathlib import Path
from typing import Any

import yaml

from ..backend.api import APIBackend, APIUnavailableError
from ..config import AppPaths, config_file, load_yaml_file, read_config, verge_daemon_state_file
from .ai_watch import AIWatchService, WatchReport, sample_connections_activity
from .probe import is_iepl_node, resolve_current_leaf
from .rebalance import RebalanceReport, RebalanceService

DEFAULT_VERGE_CONTROLLER = "127.0.0.1:9097"
DEFAULT_VERGE_MIXED_PORT = 7897
REQUIRED_VERGE_GROUPS = ("AI-MANUAL", "AI-GEMINI", "AI-AUTO")
AI_WATCH_GROUPS = ("AI-MANUAL", "AI-GEMINI")
SWITCH_STORM_WINDOW_SEC = 600.0
SWITCH_STORM_THRESHOLD = 6
SWITCH_HISTORY_LIMIT = 120


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


@dataclass
class VergeEgressStatus:
    group: str
    present: bool
    current_group: str = "-"
    leaf_node: str = "-"
    delay_ms: int | None = None
    alive: bool | None = None
    is_iepl: bool = False
    active_connections: int = 0
    last_verdict: str = "-"


@dataclass
class VergeStatusReport:
    overall_effective: bool
    controller: str
    mixed_port: int
    controller_ok: bool
    kernel_version: str = "-"
    topology_ok: bool = False
    groups_present: list[str] = field(default_factory=list)
    missing_groups: list[str] = field(default_factory=list)
    rules_ok: bool = False
    ai_manual_rules_count: int = 0
    ai_gemini_rules_count: int = 0
    daemon_ok: bool = False
    daemon_pid: int | None = None
    daemon_pid_alive: bool = False
    heartbeat_age_sec: float | None = None
    watch_ok: bool = False
    last_watch_age_sec: float | None = None
    watch_runs: int = 0
    rebalance_runs: int = 0
    total_switches: int = 0
    switches_last_10m: int = 0
    switch_storm: bool = False
    total_reaped_connections: int = 0
    egress: list[VergeEgressStatus] = field(default_factory=list)
    remedies: list[str] = field(default_factory=list)


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
    detected = discover_clash_verge_config(verge_dir)
    target_dir = verge_dir or detected.source_dir
    if target_dir is None or not target_dir.is_dir():
        raise RuntimeError("未检测到 Clash Verge 配置目录，请先启动 Clash Verge，或通过 --verge-dir 显式指定目录")

    cfg_path, detected = init_verge_config(
        paths,
        controller=controller,
        secret=secret,
        mixed_port=mixed_port,
        verge_dir=verge_dir,
    )
    fallback_js = paths.config_dir / "clash-verge-script.js"
    export_verge_script(fallback_js)

    try:
        injected_js, chain_enabled = inject_verge_global_script(target_dir)
    except OSError as exc:
        raise RuntimeError(f"无法注入 Clash Verge 全局扩展脚本: {target_dir}: {exc}") from exc

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


def _load_daemon_state(paths: AppPaths) -> dict[str, Any]:
    path = verge_daemon_state_file(paths)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_daemon_state(paths: AppPaths, state: dict[str, Any]) -> None:
    path = verge_daemon_state_file(paths)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass


def _is_pid_alive(pid: int | None) -> bool:
    if not pid or pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        handle = kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        try:
            exit_code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return False
            return exit_code.value == 259
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def _record_daemon_heartbeat(
    paths: AppPaths,
    *,
    now: float,
    sampled_connections: int,
    tick_report: VergeTickReport | None = None,
) -> None:
    state = _load_daemon_state(paths)
    state["pid"] = os.getpid()
    state["updated_at"] = now
    state["last_sample_ts"] = now
    state["sampled_connections"] = sampled_connections

    watch_runs = int(state.get("watch_runs") or 0)
    rebalance_runs = int(state.get("rebalance_runs") or 0)
    total_switches = int(state.get("total_switches") or 0)
    total_reaped = int(state.get("total_reaped_connections") or 0)
    verdicts = dict(state.get("last_verdicts") or {})
    switch_history = [
        float(value)
        for value in state.get("switch_timestamps", [])
        if isinstance(value, (int, float)) and now - float(value) <= 86400.0
    ]

    if tick_report is not None:
        if tick_report.watch_report is not None:
            watch_runs += 1
            state["last_watch_ts"] = now
            total_reaped += len(tick_report.watch_report.reaped_connections or [])
            for gr in tick_report.watch_report.results:
                verdicts[gr.group] = gr.verdict
                if gr.switched:
                    total_switches += 1
                    switch_history.append(now)
        if tick_report.rebalance_report is not None:
            rebalance_runs += 1
            state["last_rebalance_ts"] = now

    state["watch_runs"] = watch_runs
    state["rebalance_runs"] = rebalance_runs
    state["total_switches"] = total_switches
    state["switch_timestamps"] = switch_history[-SWITCH_HISTORY_LIMIT:]
    state["total_reaped_connections"] = total_reaped
    state["last_verdicts"] = verdicts
    _save_daemon_state(paths, state)


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
        _record_daemon_heartbeat(
            self.paths,
            now=now,
            sampled_connections=report.sampled_connections,
            tick_report=report,
        )
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
                idle_map = sample_connections_activity(self.paths, api, now=now)

                due_watch = (now - last_watch_ts) >= watch_sec
                due_rebalance = (now - last_rebalance_ts) >= rebalance_sec
                tick_report: VergeTickReport | None = None
                if due_watch or due_rebalance:
                    tick_report = VergeTickReport(timestamp=now, sampled_connections=len(idle_map))
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
                _record_daemon_heartbeat(
                    self.paths,
                    now=now,
                    sampled_connections=len(idle_map),
                    tick_report=tick_report,
                )
                if tick_report is not None and on_tick is not None:
                    try:
                        on_tick(tick_report)
                    except Exception:
                        pass

                ticks += 1
                if max_ticks is not None and ticks >= max_ticks:
                    break
                sleep_fn(sample_sec)
        except KeyboardInterrupt:
            return 0
        return 0


def check_verge_effectiveness(
    paths: AppPaths,
    *,
    max_heartbeat_age_sec: float = 90.0,
    now: float | None = None,
) -> VergeStatusReport:
    """核验 Clash Verge 控制面配置、守护状态、AI 探测健康与近期切流情况。"""
    ts = time.time() if now is None else now
    cfg = read_config(paths)
    controller = str(cfg.get("external-controller") or DEFAULT_VERGE_CONTROLLER)
    try:
        mixed_port = int(cfg.get("mixed-port") or DEFAULT_VERGE_MIXED_PORT)
    except (TypeError, ValueError):
        mixed_port = DEFAULT_VERGE_MIXED_PORT

    api = APIBackend(paths)
    controller_ok = False
    kernel_version = "-"
    try:
        ver_payload = api.version()
        if isinstance(ver_payload, dict):
            controller_ok = True
            kernel_version = str(ver_payload.get("version") or ver_payload.get("meta") or "mihomo")
    except Exception:
        controller_ok = False

    groups_present: list[str] = []
    missing_groups: list[str] = list(REQUIRED_VERGE_GROUPS)
    topology_ok = False
    rules_ok = False
    ai_manual_rules_count = 0
    ai_gemini_rules_count = 0
    egress_list: list[VergeEgressStatus] = []

    daemon_state = _load_daemon_state(paths)
    last_verdicts = dict(daemon_state.get("last_verdicts") or {})

    if controller_ok:
        try:
            groups_map = api.get_groups()
            groups_present = [g for g in REQUIRED_VERGE_GROUPS if g in groups_map]
            missing_groups = [g for g in REQUIRED_VERGE_GROUPS if g not in groups_map]
            topology_ok = len(missing_groups) == 0

            # 统计当前活跃连接数
            conn_counts: dict[str, int] = {"AI-MANUAL": 0, "AI-GEMINI": 0}
            try:
                conns_payload = api.get_connections()
                raw_conns = conns_payload.get("connections") if isinstance(conns_payload, dict) else []
                for c in raw_conns if isinstance(raw_conns, list) else []:
                    if not isinstance(c, dict):
                        continue
                    chains = [str(x) for x in (c.get("chains") or [])]
                    for gname in ("AI-MANUAL", "AI-GEMINI"):
                        if gname in chains:
                            conn_counts[gname] = conn_counts.get(gname, 0) + 1
            except Exception:
                pass

            for gname in ("AI-MANUAL", "AI-GEMINI"):
                gobj = groups_map.get(gname)
                if gobj is None:
                    egress_list.append(
                        VergeEgressStatus(
                            group=gname,
                            present=False,
                            last_verdict=str(last_verdicts.get(gname) or "missing_group"),
                        )
                    )
                    continue
                cur = gobj.current or "-"
                leaf = resolve_current_leaf(groups_map, gname) or cur
                leaf_obj = groups_map.get(leaf)
                delay_ms = leaf_obj.delay if leaf_obj is not None else gobj.delay
                alive = leaf_obj.alive if leaf_obj is not None else gobj.alive
                egress_list.append(
                    VergeEgressStatus(
                        group=gname,
                        present=True,
                        current_group=cur,
                        leaf_node=leaf,
                        delay_ms=delay_ms,
                        alive=alive,
                        is_iepl=is_iepl_node(leaf) if leaf and leaf != "-" else False,
                        active_connections=conn_counts.get(gname, 0),
                        last_verdict=str(last_verdicts.get(gname) or "-"),
                    )
                )
        except Exception:
            pass

        try:
            rules_payload = api.get_rules()
            raw_rules = rules_payload.get("rules") if isinstance(rules_payload, dict) else []
            for r in raw_rules if isinstance(raw_rules, list) else []:
                if not isinstance(r, dict):
                    continue
                proxy_target = str(r.get("proxy") or "")
                if proxy_target == "AI-MANUAL":
                    ai_manual_rules_count += 1
                elif proxy_target == "AI-GEMINI":
                    ai_gemini_rules_count += 1
            rules_ok = ai_manual_rules_count > 0 and ai_gemini_rules_count > 0
        except Exception:
            rules_ok = False

    raw_pid = daemon_state.get("pid")
    daemon_pid = int(raw_pid) if isinstance(raw_pid, int) and raw_pid > 0 else None
    updated_at = daemon_state.get("updated_at")
    heartbeat_age_sec: float | None = None
    if isinstance(updated_at, (int, float)) and updated_at > 0:
        heartbeat_age_sec = max(0.0, ts - float(updated_at))
    last_watch_at = daemon_state.get("last_watch_ts")
    last_watch_age_sec: float | None = None
    if isinstance(last_watch_at, (int, float)) and last_watch_at > 0:
        last_watch_age_sec = max(0.0, ts - float(last_watch_at))
    pid_alive = _is_pid_alive(daemon_pid)
    heartbeat_fresh = heartbeat_age_sec is not None and heartbeat_age_sec <= max_heartbeat_age_sec
    daemon_ok = bool(heartbeat_fresh and (pid_alive or heartbeat_age_sec <= 30.0))
    watch_ok = bool(
        last_watch_age_sec is not None
        and last_watch_age_sec <= max_heartbeat_age_sec
        and all(last_verdicts.get(group) == "ok" for group in AI_WATCH_GROUPS)
    )
    switches_last_10m = sum(
        1
        for value in daemon_state.get("switch_timestamps", [])
        if isinstance(value, (int, float)) and ts - SWITCH_STORM_WINDOW_SEC <= float(value) <= ts + 5.0
    )
    switch_storm = switches_last_10m >= SWITCH_STORM_THRESHOLD

    remedies: list[str] = []
    if not controller_ok:
        remedies.append(
            f"无法连接 Clash Verge 控制器 ({controller})：请确认 Clash Verge 已启动，"
            "且 External Controller 地址/密钥一致（可运行 `cproxy verge install` 重新同步）。"
        )
    else:
        if not topology_ok or not rules_ok:
            remedies.append(
                "内核内存中尚未挂载 AI-MANUAL / AI-GEMINI 策略组或分流规则："
                "请在 Clash Verge 界面点击一次「订阅 (Profiles) -> 刷新」，或确认「脚本 (Script)」已启用。"
            )
        for eg in egress_list:
            if eg.present and eg.is_iepl:
                remedies.append(
                    f"{eg.group} 当前叶子节点 ({eg.leaf_node}) 为专线/IEPL，可执行 `cproxy verge once` 切换至非专线独立 IP。"
                )
    if not daemon_ok:
        remedies.append(
            "旁路守护进程未运行或心跳超时：Windows 下可双击 `setup-windows-verge.bat` 或运行 `cproxy verge daemon` 启动后台看护。"
        )
    elif not watch_ok:
        remedies.append(
            "AI 看护未通过或结果已过期：请运行 `cproxy verge once --no-rebalance --dry-run` 检查探测链路；"
            "若所有节点同时失败，优先排查本机探测流量是否被 DIRECT 规则绕过代理。"
        )
    if switch_storm:
        remedies.append(
            f"近 10 分钟已切流 {switches_last_10m} 次，疑似切流风暴：请暂停自动调度并检查探测链路与节点健康，"
            "避免持续中断现有 AI 会话。"
        )

    overall_effective = bool(
        controller_ok and topology_ok and rules_ok and daemon_ok and watch_ok and not switch_storm
    )
    return VergeStatusReport(
        overall_effective=overall_effective,
        controller=controller,
        mixed_port=mixed_port,
        controller_ok=controller_ok,
        kernel_version=kernel_version,
        topology_ok=topology_ok,
        groups_present=groups_present,
        missing_groups=missing_groups,
        rules_ok=rules_ok,
        ai_manual_rules_count=ai_manual_rules_count,
        ai_gemini_rules_count=ai_gemini_rules_count,
        daemon_ok=daemon_ok,
        daemon_pid=daemon_pid,
        daemon_pid_alive=pid_alive,
        heartbeat_age_sec=round(heartbeat_age_sec, 1) if heartbeat_age_sec is not None else None,
        watch_ok=watch_ok,
        last_watch_age_sec=round(last_watch_age_sec, 1) if last_watch_age_sec is not None else None,
        watch_runs=int(daemon_state.get("watch_runs") or 0),
        rebalance_runs=int(daemon_state.get("rebalance_runs") or 0),
        total_switches=int(daemon_state.get("total_switches") or 0),
        switches_last_10m=switches_last_10m,
        switch_storm=switch_storm,
        total_reaped_connections=int(daemon_state.get("total_reaped_connections") or 0),
        egress=egress_list,
        remedies=remedies,
    )
