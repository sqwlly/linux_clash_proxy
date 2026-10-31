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
from ..config import (
    AppPaths,
    ai_gemini_order_file,
    ai_manual_order_file,
    ai_watch_state_file,
    config_file,
    default_paths,
    load_yaml_file,
    read_config,
    verge_daemon_state_file,
)
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
USER_SYSTEMD_TIMERS = (
    "cproxy-ai-watch.timer",
    "cproxy-ai-rebalance.timer",
    "cproxy-refresh.timer",
    "cproxy-subscription.timer",
)


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
    scheduler_mode: str = "none"
    active_timers: list[str] = field(default_factory=list)
    installed_timers: list[str] = field(default_factory=list)
    rebalance_age_sec: float | None = None
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


def try_hot_reload_verge_runtime(
    paths: AppPaths,
    verge_dir: Path | None = None,
    *,
    force: bool = False,
) -> bool:
    """将 Clash Verge 当前生效配置重新应用 `Script.js`（及订阅局部 `option.script`）并通过
    `PUT /configs?force=true` 内存热加载到运行中的 `verge-mihomo`，无需等待用户手工点「订阅 -> 刷新」。
    """
    import shutil
    import subprocess
    import tempfile

    node_bin = shutil.which("node")
    if not node_bin:
        return False

    target_dir = verge_dir
    if target_dir is None:
        target_dir = discover_clash_verge_config().source_dir
    if target_dir is None or not target_dir.is_dir():
        return False

    verge_runtime_yaml = target_dir / "clash-verge.yaml"
    global_script_js = target_dir / "profiles" / "Script.js"
    if not verge_runtime_yaml.is_file() or not global_script_js.is_file():
        return False

    api = APIBackend(paths)
    try:
        groups_map = api.get_groups()
    except Exception:
        return False

    if (
        not force
        and "AI-MANUAL-KEEPALIVE" in groups_map
        and "AI-GEMINI-KEEPALIVE" in groups_map
    ):
        return True

    raw_base = load_yaml_file(verge_runtime_yaml)
    if not isinstance(raw_base, dict) or not raw_base.get("proxies"):
        return False

    script_paths: list[Path] = [global_script_js.resolve()]
    profile_name = "VergeProfile"
    profiles_yaml = target_dir / "profiles.yaml"
    if profiles_yaml.is_file():
        raw_profiles = load_yaml_file(profiles_yaml)
        if isinstance(raw_profiles, dict):
            current_uid = str(raw_profiles.get("current") or "")
            items = [it for it in (raw_profiles.get("items") or []) if isinstance(it, dict)]
            uid_map = {str(it.get("uid") or ""): it for it in items if it.get("uid")}
            cur_item = uid_map.get(current_uid)
            if isinstance(cur_item, dict):
                profile_name = str(cur_item.get("name") or cur_item.get("file") or profile_name)
                opt = cur_item.get("option")
                prof_script_uid = str(opt.get("script") or "") if isinstance(opt, dict) else ""
                prof_script_item = uid_map.get(prof_script_uid)
                if isinstance(prof_script_item, dict) and prof_script_item.get("file"):
                    cand_js = (target_dir / "profiles" / str(prof_script_item["file"])).resolve()
                    if cand_js.is_file() and cand_js not in script_paths:
                        script_paths.append(cand_js)

    try:
        selectors = api.selector_now()
    except Exception:
        selectors = {}
    cur_manual = groups_map["AI-MANUAL"].current if "AI-MANUAL" in groups_map else None
    cur_gemini = groups_map["AI-GEMINI"].current if "AI-GEMINI" in groups_map else None

    try:
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            in_json = td_path / "in.json"
            out_json = td_path / "out.json"
            runner_js = td_path / "run.js"
            in_json.write_text(json.dumps(raw_base, ensure_ascii=False), encoding="utf-8")
            js_list_literal = json.dumps([str(p) for p in script_paths])
            runner_js.write_text(
                "const fs = require('fs');\n"
                "const vm = require('vm');\n"
                f"let cfg = JSON.parse(fs.readFileSync({json.dumps(str(in_json))}, 'utf8'));\n"
                f"for (const p of {js_list_literal}) {{\n"
                "  const code = fs.readFileSync(p, 'utf8') + '\\n;globalThis.__main = main;';\n"
                "  const ctx = { console, globalThis: {} };\n"
                "  ctx.globalThis = ctx;\n"
                "  vm.createContext(ctx);\n"
                "  vm.runInContext(code, ctx);\n"
                f"  cfg = ctx.__main(cfg, {json.dumps(profile_name)});\n"
                "}\n"
                f"fs.writeFileSync({json.dumps(str(out_json))}, JSON.stringify(cfg));\n",
                encoding="utf-8",
            )
            subprocess.run(
                [node_bin, str(runner_js)],
                check=True,
                capture_output=True,
                timeout=15.0,
            )
            new_cfg = json.loads(out_json.read_text(encoding="utf-8"))
    except Exception:
        return False

    if not isinstance(new_cfg, dict):
        return False

    rp_map = new_cfg.get("rule-providers")
    if isinstance(rp_map, dict):
        for rp in rp_map.values():
            if isinstance(rp, dict) and isinstance(rp.get("path"), str):
                if rp["path"].startswith("./"):
                    rp["path"] = rp["path"][2:]

    for grp in new_cfg.get("proxy-groups") or []:
        if not isinstance(grp, dict) or not isinstance(grp.get("proxies"), list):
            continue
        if grp.get("name") == "AI-MANUAL" and cur_manual and cur_manual in grp["proxies"]:
            grp["proxies"] = [cur_manual] + [x for x in grp["proxies"] if x != cur_manual]
        elif grp.get("name") == "AI-GEMINI" and cur_gemini and cur_gemini in grp["proxies"]:
            grp["proxies"] = [cur_gemini] + [x for x in grp["proxies"] if x != cur_gemini]

    try:
        payload_yaml = yaml.safe_dump(new_cfg, allow_unicode=True, sort_keys=False)
        api.request("PUT", "/configs?force=true", {"path": "", "payload": payload_yaml})
    except Exception:
        return False

    if selectors:
        try:
            api.restore_selectors(selectors)
        except Exception:
            pass
    try:
        api.delay_test("H2-KEEPALIVE", "http://cp.cloudflare.com/generate_204", 2000)
    except Exception:
        pass
    return True


def install_verge_oneclick(
    paths: AppPaths,
    *,
    controller: str | None = None,
    secret: str | None = None,
    mixed_port: int | None = None,
    verge_dir: Path | None = None,
    install_ca_trust: bool | None = None,
) -> VergeInstallReport:
    """一键完成 Clash Verge 旁路配置初始化 + 全局扩展脚本自动注入 + 本地 H2/WS 保活证书初始化。"""
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

    eff_ca_trust = (verge_dir is None) if install_ca_trust is None else bool(install_ca_trust)
    if eff_ca_trust:
        try:
            from .h2_keepalive import ensure_local_ca_and_leaf_cert

            ensure_local_ca_and_leaf_cert(
                paths.data_dir / "tls",
                install_system_trust=True,
                allow_user_prompt=True,
            )
        except Exception:
            pass

    try:
        injected_js, chain_enabled = inject_verge_global_script(target_dir)
    except OSError as exc:
        raise RuntimeError(f"无法注入 Clash Verge 全局扩展脚本: {target_dir}: {exc}") from exc

    if verge_dir is None:
        try:
            from .ai_watch import has_any_inflight_ai_connection

            if not has_any_inflight_ai_connection(paths, APIBackend(paths)):
                try_hot_reload_verge_runtime(paths, target_dir)
        except Exception:
            pass

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
            now=time.time(),
            sampled_connections=report.sampled_connections,
            tick_report=report,
        )
        return report

    def _start_embedded_keepalive_thread(self) -> Any:
        import asyncio
        import threading

        from .h2_keepalive import (
            DEFAULT_KEEPALIVE_PORT,
            h2_keepalive_pid_file,
            is_h2_keepalive_running,
            is_windows_admin,
            run_h2_keepalive_server,
        )

        if is_h2_keepalive_running(self.paths.state_dir, port=DEFAULT_KEEPALIVE_PORT):
            return None

        cfg = read_config(self.paths)
        try:
            mixed_port = int(cfg.get("mixed-port") or DEFAULT_VERGE_MIXED_PORT)
        except (TypeError, ValueError):
            mixed_port = DEFAULT_VERGE_MIXED_PORT

        tls_dir = self.paths.data_dir / "tls"
        pid_path = h2_keepalive_pid_file(self.paths.state_dir)

        def _worker() -> None:
            try:
                asyncio.run(
                    run_h2_keepalive_server(
                        tls_dir,
                        listen_port=DEFAULT_KEEPALIVE_PORT,
                        upstream_port=mixed_port,
                        pid_path=pid_path,
                        install_system_trust=is_windows_admin(),
                        allow_user_prompt=False,
                        require_system_trust=True,
                    )
                )
            except Exception:
                pass

        thread = threading.Thread(target=_worker, name="cproxy-keepalive-gw", daemon=True)
        thread.start()
        return thread

    def run_daemon(
        self,
        *,
        watch_interval: float = 20.0,
        rebalance_interval: float = 1800.0,
        sample_interval: float = 3.0,
        dry_run: bool = False,
        max_ticks: int | None = None,
        enable_keepalive: bool | None = None,
        sleep_fn: Callable[[float], None] = time.sleep,
        on_tick: Callable[[VergeTickReport], None] | None = None,
    ) -> int:
        watch_sec = max(5.0, float(watch_interval))
        rebalance_sec = max(60.0, float(rebalance_interval))
        sample_sec = max(1.0, min(float(sample_interval), watch_sec))

        eff_keepalive = (max_ticks is None and not dry_run) if enable_keepalive is None else bool(enable_keepalive)
        keepalive_thread = self._start_embedded_keepalive_thread() if eff_keepalive else None

        last_watch_ts = 0.0
        last_rebalance_ts = 0.0
        last_keepalive_mount_check_ts = 0.0
        ticks = 0
        api = APIBackend(self.paths)

        try:
            while max_ticks is None or ticks < max_ticks:
                now = time.time()
                if eff_keepalive and (keepalive_thread is None or not keepalive_thread.is_alive()):
                    keepalive_thread = self._start_embedded_keepalive_thread()
                idle_map = sample_connections_activity(self.paths, api, now=now)
                if eff_keepalive and (now - last_keepalive_mount_check_ts) >= 60.0:
                    last_keepalive_mount_check_ts = now
                    try:
                        from .ai_watch import has_any_inflight_ai_connection

                        gmap = api.get_groups()
                        if (
                            "AI-MANUAL" in gmap
                            and "AI-MANUAL-KEEPALIVE" not in gmap
                            and not has_any_inflight_ai_connection(self.paths, api)
                        ):
                            try_hot_reload_verge_runtime(self.paths)
                    except Exception:
                        pass

                due_watch = (now - last_watch_ts) >= watch_sec
                due_rebalance = (now - last_rebalance_ts) >= rebalance_sec
                tick_report: VergeTickReport | None = None
                if due_watch or due_rebalance:
                    tick_report = VergeTickReport(timestamp=now, sampled_connections=len(idle_map))
                    if due_watch:
                        tick_report.watch_report = AIWatchService(self.paths).watch(dry_run=dry_run)
                        last_watch_ts = time.time()
                    if due_rebalance:
                        _record_daemon_heartbeat(
                            self.paths,
                            now=time.time(),
                            sampled_connections=len(idle_map),
                        )
                        try:
                            tick_report.rebalance_report = RebalanceService(self.paths).rebalance(
                                dry_run=dry_run,
                                external_only=True,
                            )
                        except Exception as exc:
                            tick_report.warnings.append(str(exc))
                        last_rebalance_ts = time.time()
                _record_daemon_heartbeat(
                    self.paths,
                    now=time.time(),
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


def _load_watch_telemetry_state(paths: AppPaths) -> tuple[dict[str, Any], float | None]:
    path = ai_watch_state_file(paths)
    if not path.exists():
        return {}, None
    try:
        mtime = path.stat().st_mtime
        data = json.loads(path.read_text(encoding="utf-8"))
        return (data if isinstance(data, dict) else {}), mtime
    except Exception:
        return {}, None


def _parse_list_timers_output(stdout: str, now: float) -> dict[str, dict[str, Any]]:
    import re
    from datetime import datetime

    ts_re = re.compile(r"(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})")
    known_units = set(USER_SYSTEMD_TIMERS) | {"clash-proxy-traffic-collector.timer"}
    parsed: dict[str, dict[str, Any]] = {}
    for raw_line in (stdout or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        matched_unit = next((u for u in known_units if u in line), None)
        if not matched_unit:
            continue
        timestamps = ts_re.findall(line)
        next_in_sec: float | None = None
        last_ago_sec: float | None = None
        if len(timestamps) >= 2:
            try:
                next_dt = datetime.strptime(timestamps[0], "%Y-%m-%d %H:%M:%S")
                next_in_sec = max(0.0, next_dt.timestamp() - now)
            except ValueError:
                pass
            try:
                last_dt = datetime.strptime(timestamps[1], "%Y-%m-%d %H:%M:%S")
                last_ago_sec = max(0.0, now - last_dt.timestamp())
            except ValueError:
                pass
        elif len(timestamps) == 1:
            try:
                dt = datetime.strptime(timestamps[0], "%Y-%m-%d %H:%M:%S")
                if "left" in line and not line.startswith("n/a"):
                    next_in_sec = max(0.0, dt.timestamp() - now)
                else:
                    last_ago_sec = max(0.0, now - dt.timestamp())
            except ValueError:
                pass
        parsed[matched_unit] = {
            "active": next_in_sec is not None or ("left" in line and not line.startswith("n/a")),
            "next_in_sec": round(next_in_sec, 1) if next_in_sec is not None else None,
            "last_ago_sec": round(last_ago_sec, 1) if last_ago_sec is not None else None,
        }
    return parsed


def _probe_user_systemd_timers(paths: AppPaths) -> tuple[list[str], list[str]]:
    """检查当前 paths 所属用户的 systemd user 目录下是否安装了 cproxy 定时器及其 active 状态。"""
    import subprocess

    user_systemd_dir = paths.config_dir.parent / "systemd" / "user"
    installed = [t for t in USER_SYSTEMD_TIMERS if (user_systemd_dir / t).is_file()]
    if not installed:
        return [], []
    if paths.config_dir != default_paths().config_dir:
        return installed, []
    try:
        proc = subprocess.run(
            ["systemctl", "--user", "is-active", *installed],
            capture_output=True,
            text=True,
            timeout=2.0,
            check=False,
        )
        lines = [ln.strip() for ln in (proc.stdout or "").splitlines()]
        active = [
            unit
            for idx, unit in enumerate(installed)
            if idx < len(lines) and lines[idx] == "active"
        ]
        return installed, active
    except Exception:
        return installed, []


def _probe_systemd_timer_schedule(paths: AppPaths, now: float) -> dict[str, dict[str, Any]]:
    """查询用户级与系统级 systemd timer 的上次触发与下次倒计时。"""
    import subprocess

    user_systemd_dir = paths.config_dir.parent / "systemd" / "user"
    if not any((user_systemd_dir / t).is_file() for t in USER_SYSTEMD_TIMERS):
        return {}
    if paths.config_dir != default_paths().config_dir:
        return {}
    schedule: dict[str, dict[str, Any]] = {}
    try:
        proc = subprocess.run(
            ["systemctl", "--user", "list-timers", "--all", "--no-pager"],
            capture_output=True,
            text=True,
            timeout=2.0,
            check=False,
        )
        if proc.returncode == 0 and proc.stdout:
            schedule.update(_parse_list_timers_output(proc.stdout, now))
    except Exception:
        pass
    sys_timer = Path("/etc/systemd/system/clash-proxy-traffic-collector.timer")
    if sys_timer.is_file():
        try:
            proc_sys = subprocess.run(
                ["systemctl", "list-timers", "clash-proxy-traffic-collector.timer", "--all", "--no-pager"],
                capture_output=True,
                text=True,
                timeout=2.0,
                check=False,
            )
            if proc_sys.returncode == 0 and proc_sys.stdout:
                schedule.update(_parse_list_timers_output(proc_sys.stdout, now))
        except Exception:
            pass
    return schedule


def collect_scheduler_telemetry(
    paths: AppPaths,
    *,
    max_heartbeat_age_sec: float = 90.0,
    now: float | None = None,
) -> dict[str, Any]:
    """汇总调度与看护遥测：同时兼容 Windows `verge daemon` 与 Linux `systemd --user` 定时器 / `ai-watch`。"""
    ts = time.time() if now is None else now
    daemon_state = _load_daemon_state(paths)
    watch_state, watch_mtime = _load_watch_telemetry_state(paths)
    installed_timers, active_timers = _probe_user_systemd_timers(paths)

    raw_pid = daemon_state.get("pid")
    daemon_pid = int(raw_pid) if isinstance(raw_pid, int) and raw_pid > 0 else None
    pid_alive = _is_pid_alive(daemon_pid)

    daemon_updated = daemon_state.get("updated_at")
    daemon_ts = float(daemon_updated) if isinstance(daemon_updated, (int, float)) and daemon_updated > 0 else None

    watch_updated = watch_state.get("updated_at")
    if isinstance(watch_updated, (int, float)) and watch_updated > 0:
        watch_ts: float | None = float(watch_updated)
    elif watch_mtime is not None and any(isinstance(watch_state.get(g), dict) for g in ("AI-MANUAL", "AI-GEMINI")):
        watch_ts = float(watch_mtime)
    else:
        watch_ts = None

    ts_candidates = [x for x in (daemon_ts, watch_ts) if x is not None]
    updated_at = max(ts_candidates) if ts_candidates else None
    heartbeat_age_sec: float | None = None
    if updated_at is not None and updated_at > 0:
        heartbeat_age_sec = max(0.0, ts - updated_at)

    # 汇总各出口最近判定
    last_verdicts: dict[str, str] = {}
    for gname in ("AI-MANUAL", "AI-GEMINI"):
        gs = watch_state.get(gname)
        if isinstance(gs, dict):
            gv = gs.get("last_verdict")
            if isinstance(gv, str) and gv:
                last_verdicts[gname] = gv
            elif gs.get("last_active"):
                last_verdicts[gname] = "ok" if int(gs.get("fail_count") or 0) == 0 else "dead"
    w_verdicts = watch_state.get("last_verdicts")
    if isinstance(w_verdicts, dict):
        for k, v in w_verdicts.items():
            if isinstance(v, str) and v:
                last_verdicts[str(k)] = v
    d_verdicts = daemon_state.get("last_verdicts")
    if isinstance(d_verdicts, dict) and (watch_ts is None or (daemon_ts is not None and daemon_ts >= watch_ts)):
        for k, v in d_verdicts.items():
            if isinstance(v, str) and v:
                last_verdicts[str(k)] = v
    elif isinstance(d_verdicts, dict):
        for k, v in d_verdicts.items():
            if isinstance(v, str) and v and str(k) not in last_verdicts:
                last_verdicts[str(k)] = v

    # 汇总最近重排时间
    reb_candidates: list[float] = []
    for raw_reb in (daemon_state.get("last_rebalance_ts"), watch_state.get("last_rebalance_ts")):
        if isinstance(raw_reb, (int, float)) and raw_reb > 0:
            reb_candidates.append(float(raw_reb))
    for order_fn in (ai_manual_order_file, ai_gemini_order_file):
        p = order_fn(paths)
        if p.exists():
            try:
                reb_candidates.append(float(p.stat().st_mtime))
            except OSError:
                pass
    last_rebalance_ts = max(reb_candidates) if reb_candidates else None
    rebalance_age_sec = max(0.0, ts - last_rebalance_ts) if last_rebalance_ts is not None else None

    watch_runs = max(
        int(daemon_state.get("watch_runs") or 0),
        int(watch_state.get("watch_runs") or 0),
        1 if watch_ts is not None else 0,
    )
    rebalance_runs = max(
        int(daemon_state.get("rebalance_runs") or 0),
        int(watch_state.get("rebalance_runs") or 0),
        1 if reb_candidates else 0,
    )
    total_switches = max(
        int(daemon_state.get("total_switches") or 0),
        int(watch_state.get("total_switches") or 0),
    )
    total_reaped = max(
        int(daemon_state.get("total_reaped_connections") or 0),
        int(watch_state.get("total_reaped_connections") or 0),
    )

    if pid_alive or (daemon_ts is not None and not installed_timers):
        scheduler_mode = "verge-daemon"
    elif installed_timers:
        scheduler_mode = "systemd-timer"
    elif watch_ts is not None:
        scheduler_mode = "ai-watch"
    else:
        scheduler_mode = "none"

    timers_ok = bool(installed_timers and "cproxy-ai-watch.timer" in active_timers)
    heartbeat_fresh = heartbeat_age_sec is not None and heartbeat_age_sec <= max_heartbeat_age_sec
    daemon_ok = bool(heartbeat_fresh and (pid_alive or timers_ok or heartbeat_age_sec <= 60.0))
    configured = bool(
        scheduler_mode != "none"
        or bool(read_config(paths).get("verge-mode"))
        or bool(reb_candidates)
    )

    timer_schedule = _probe_systemd_timer_schedule(paths, ts) if configured else {}
    manual_order_count = 0
    gemini_order_count = 0
    try:
        m_path = ai_manual_order_file(paths)
        if m_path.exists():
            m_list = json.loads(m_path.read_text(encoding="utf-8"))
            if isinstance(m_list, list):
                manual_order_count = len(m_list)
        g_path = ai_gemini_order_file(paths)
        if g_path.exists():
            g_list = json.loads(g_path.read_text(encoding="utf-8"))
            if isinstance(g_list, list):
                gemini_order_count = len(g_list)
    except Exception:
        pass

    v_parts = [
        f"{'通用' if g == 'AI-MANUAL' else 'Gemini'}={last_verdicts[g]}"
        for g in ("AI-MANUAL", "AI-GEMINI")
        if g in last_verdicts
    ]
    watch_summary = ", ".join(v_parts) if v_parts else "就绪"
    if total_switches or total_reaped:
        watch_summary += f" · 切流 {total_switches} / 回收 {total_reaped}"

    rebalance_summary = (
        f"通用 {manual_order_count} 节点 · Gemini {gemini_order_count} 节点"
        if (manual_order_count or gemini_order_count)
        else "等待首轮测速"
    )

    def _task_entry(
        name: str,
        unit: str,
        interval: str,
        fallback_active: bool,
        fallback_last_sec: float | None,
        fallback_period_sec: float | None,
        summary_text: str,
    ) -> dict[str, Any]:
        sched_item = timer_schedule.get(unit, {})
        is_act = bool(sched_item.get("active") or (unit in active_timers) or fallback_active)
        last_sec = sched_item.get("last_ago_sec")
        if last_sec is None or (
            fallback_last_sec is not None and fallback_last_sec < float(last_sec)
        ):
            last_sec = round(fallback_last_sec, 1) if fallback_last_sec is not None else None
        next_sec = sched_item.get("next_in_sec")
        if next_sec is None and is_act and last_sec is not None and fallback_period_sec is not None:
            next_sec = round(max(0.0, fallback_period_sec - float(last_sec)), 1)
        return {
            "name": name,
            "unit": unit,
            "interval": interval,
            "active": is_act,
            "last_ago_sec": last_sec,
            "next_in_sec": next_sec,
            "summary": summary_text,
        }

    rt_age: float | None = None
    try:
        from ..config import runtime_file as _rt_f

        rt_p = _rt_f(paths)
        if rt_p.exists():
            rt_age = max(0.0, ts - rt_p.stat().st_mtime)
    except Exception:
        pass

    sub_age: float | None = None
    try:
        from ..config import subscription_info_file as _sub_f

        sub_p = _sub_f(paths)
        if sub_p.exists():
            sub_age = max(0.0, ts - sub_p.stat().st_mtime)
    except Exception:
        pass

    traffic_age: float | None = None
    try:
        from ..config import traffic_db_file as _tdb_f

        tdb_p = _tdb_f(paths)
        if tdb_p.exists():
            traffic_age = max(0.0, ts - tdb_p.stat().st_mtime)
    except Exception:
        pass

    sys_collector_installed = Path("/etc/systemd/system/clash-proxy-traffic-collector.timer").is_file()
    sys_collector_active = bool(
        timer_schedule.get("clash-proxy-traffic-collector.timer", {}).get("active")
        or (scheduler_mode == "verge-daemon" and daemon_ok)
    )

    tasks: list[dict[str, Any]] = [
        _task_entry(
            "ai-watch",
            "cproxy-ai-watch.timer",
            "20s",
            daemon_ok,
            heartbeat_age_sec,
            20.0,
            watch_summary,
        ),
        _task_entry(
            "ai-rebalance",
            "cproxy-ai-rebalance.timer",
            "30m",
            scheduler_mode == "verge-daemon" and daemon_ok,
            rebalance_age_sec,
            1800.0,
            rebalance_summary,
        ),
    ]
    if installed_timers or scheduler_mode == "systemd-timer":
        tasks.extend(
            [
                _task_entry(
                    "refresh",
                    "cproxy-refresh.timer",
                    "15m",
                    False,
                    rt_age,
                    900.0,
                    "运行配置定时同步",
                ),
                _task_entry(
                    "subscription",
                    "cproxy-subscription.timer",
                    "每日 04:00",
                    False,
                    sub_age,
                    None,
                    "订阅与套餐余量更新",
                ),
            ]
        )
        if sys_collector_installed or "clash-proxy-traffic-collector.timer" in timer_schedule:
            tasks.append(
                _task_entry(
                    "traffic-collector",
                    "clash-proxy-traffic-collector.timer",
                    "30s",
                    sys_collector_active,
                    traffic_age,
                    30.0,
                    "进程与域名流量入库",
                )
            )
    elif scheduler_mode == "verge-daemon":
        tasks.append(
            _task_entry(
                "conn-sampler",
                "verge-daemon-sampler",
                "3s",
                daemon_ok,
                heartbeat_age_sec,
                3.0,
                f"跟踪活跃连接 {int(daemon_state.get('sampled_connections') or 0)} 条",
            )
        )
        try:
            from .h2_keepalive import (
                DEFAULT_KEEPALIVE_PORT,
                is_h2_keepalive_running,
                is_local_ca_trusted,
                load_h2_keepalive_stats,
            )

            if is_h2_keepalive_running(paths.state_dir, port=DEFAULT_KEEPALIVE_PORT):
                ca_ok = is_local_ca_trusted(paths.data_dir / "tls" / "cproxy-h2-ca.crt")
                gw_stats = load_h2_keepalive_stats(paths.state_dir)
                gw_summary = (
                    format_h2_keepalive_summary(DEFAULT_KEEPALIVE_PORT, gw_stats)
                    if ca_ok
                    else f"127.0.0.1:{DEFAULT_KEEPALIVE_PORT} · TCP 直通 (待信任 CA)"
                )
                tasks.append(
                    _task_entry(
                        "keepalive-gw",
                        "verge-keepalive-gateway",
                        "4.5s/35s",
                        True,
                        heartbeat_age_sec,
                        4.5,
                        gw_summary,
                    )
                )
        except Exception:
            pass

    from .h2_keepalive import DEFAULT_KEEPALIVE_PORT, is_h2_keepalive_running, load_h2_keepalive_stats

    h2_alive = is_h2_keepalive_running(paths.state_dir)
    if h2_alive and not any(t.get("name") == "keepalive-gw" for t in tasks):
        h2_stats = load_h2_keepalive_stats(paths.state_dir)
        h2_updated = float(h2_stats.get("updated_at") or 0.0)
        h2_age = max(0.0, ts - h2_updated) if h2_updated > 0 else None
        tasks.append(
            {
                "name": "h2-keepalive",
                "unit": "cproxy-h2-keepalive.service",
                "interval": "4.5s",
                "active": True,
                "last_ago_sec": round(h2_age, 1) if h2_age is not None else None,
                "next_in_sec": 4.5,
                "summary": format_h2_keepalive_summary(DEFAULT_KEEPALIVE_PORT, h2_stats),
            }
        )

    return {
        "configured": configured,
        "scheduler_mode": scheduler_mode,
        "daemon_ok": daemon_ok,
        "daemon_pid": daemon_pid,
        "daemon_pid_alive": pid_alive,
        "installed_timers": installed_timers,
        "active_timers": active_timers,
        "updated_at": updated_at,
        "heartbeat_age_sec": round(heartbeat_age_sec, 1) if heartbeat_age_sec is not None else None,
        "rebalance_age_sec": round(rebalance_age_sec, 1) if rebalance_age_sec is not None else None,
        "watch_runs": watch_runs,
        "rebalance_runs": rebalance_runs,
        "total_switches": total_switches,
        "total_reaped_connections": total_reaped,
        "last_verdicts": last_verdicts,
        "tasks": tasks,
    }


def format_h2_keepalive_summary(port: int, stats: dict[str, int | float]) -> str:
    """将保活网关多协议接管与旧进程直通统计格式化为清晰的活跃/累计摘要。"""
    h2_s = int(stats.get("h2_sessions") or 0)
    ws_s = int(stats.get("ws_sessions") or 0)
    h1_s = int(stats.get("http1_sessions") or 0)
    tls_total = h2_s + ws_s + h1_s
    pings = int(stats.get("pings_sent") or 0)
    act_s = int(stats.get("active_sessions") or 0)
    act_p = int(stats.get("active_passthrough") or 0)
    tot_p = int(stats.get("passthrough_sessions") or 0)

    proto_parts = [
        *([f"H2:{h2_s}"] if h2_s > 0 else []),
        *([f"WS:{ws_s}"] if ws_s > 0 else []),
        *([f"H1:{h1_s}"] if h1_s > 0 else []),
    ]
    if proto_parts:
        act_prefix = f"{act_s}活跃/" if act_s > 0 else ""
        tls_part = f"TLS接管 {act_prefix}累计{tls_total} ({'/'.join(proto_parts)})"
    elif act_s > 0:
        tls_part = f"TLS接管 {act_s}活跃"
    else:
        tls_part = "TLS接管 0"

    if act_p > 0:
        pass_tag = f" · 旧进程直通 当前{act_p}/累计{tot_p}"
    elif tot_p > 0:
        pass_tag = f" · 旧进程历史直通 {tot_p}"
    else:
        pass_tag = ""

    return f"127.0.0.1:{port} · {tls_part} · 已发 PING {pings}{pass_tag}"


def collect_ai_egress_deep_status(
    paths: AppPaths,
    groups_map: dict[str, Any] | None = None,
    scheduler: dict[str, Any] | None = None,
    now: float | None = None,
) -> dict[str, dict[str, Any]]:
    """收集 AI-MANUAL 与 AI-GEMINI 的深度状态：看护判定、线路安全性、活跃连接数、顺位备选与冷却数。"""
    from .ai_watch import COOLDOWN_SECONDS
    from .h2_keepalive import DEFAULT_KEEPALIVE_PORT, is_h2_keepalive_running, load_h2_keepalive_stats

    ts = time.time() if now is None else now
    sched = scheduler if scheduler is not None else collect_scheduler_telemetry(paths, now=ts)
    last_verdicts = dict(sched.get("last_verdicts") or {})
    watch_state, _ = _load_watch_telemetry_state(paths)

    conn_counts: dict[str, int] = {"AI-MANUAL": 0, "AI-GEMINI": 0}
    try:
        conns_payload = APIBackend(paths).get_connections()
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

    details: dict[str, dict[str, Any]] = {}
    for gname in ("AI-MANUAL", "AI-GEMINI"):
        gobj = (groups_map or {}).get(gname)
        if gobj is None:
            continue
        leaf = resolve_current_leaf(groups_map or {}, gname) or str(getattr(gobj, "current", "") or "-")
        iepl = is_iepl_node(leaf, paths) if leaf and leaf != "-" else False
        gs = watch_state.get(gname) if isinstance(watch_state.get(gname), dict) else {}
        cooldown_map = gs.get("cooldown") if isinstance(gs, dict) and isinstance(gs.get("cooldown"), dict) else {}
        active_cooldowns = sum(
            1
            for k, v in cooldown_map.items()
            if isinstance(v, (int, float)) and (ts - float(v)) < COOLDOWN_SECONDS
        )
        cooled_set = {
            str(k)
            for k, v in cooldown_map.items()
            if isinstance(v, (int, float)) and (ts - float(v)) < COOLDOWN_SECONDS
        }
        raw_cands = list(getattr(gobj, "candidates", None) or [])
        leaf_cands = [
            str(c)
            for c in raw_cands
            if c
            and str(c) != "AI-AUTO"
            and not (groups_map and str(c) in groups_map and getattr(groups_map[str(c)], "candidates", None))
        ]
        next_standby = [
            c
            for c in leaf_cands
            if c != leaf and c not in cooled_set and not is_iepl_node(c, paths)
        ]
        if len(next_standby) < 2:
            for c in leaf_cands:
                if c != leaf and c not in next_standby:
                    next_standby.append(c)
                if len(next_standby) >= 2:
                    break

        entry: dict[str, Any] = {
            "group": gname,
            "leaf": leaf,
            "verdict": str(last_verdicts.get(gname) or "ok" if sched.get("configured") else "-"),
            "is_iepl": iepl,
            "line_label": "专线/IEPL [风险]" if iepl else "1X 独立 IP [安全]",
            "active_connections": conn_counts.get(gname, 0),
            "next_candidates": next_standby[:2],
            "pool_size": len(leaf_cands),
            "cooldown_count": active_cooldowns,
        }
        if gname == "AI-GEMINI" and is_h2_keepalive_running(paths.state_dir):
            h2_stats = load_h2_keepalive_stats(paths.state_dir)
            h2_s = int(h2_stats.get("h2_sessions") or 0)
            ws_s = int(h2_stats.get("ws_sessions") or 0)
            h1_s = int(h2_stats.get("http1_sessions") or 0)
            entry["h2_keepalive"] = {
                "active": True,
                "port": DEFAULT_KEEPALIVE_PORT,
                "pings_sent": int(h2_stats.get("pings_sent") or 0),
                "ws_pings_sent": int(h2_stats.get("ws_pings_sent") or 0),
                "h2_sessions": h2_s,
                "ws_sessions": ws_s,
                "http1_sessions": h1_s,
                "tls_sessions": h2_s + ws_s + h1_s,
                "active_sessions": int(h2_stats.get("active_sessions") or 0),
                "active_passthrough": int(h2_stats.get("active_passthrough") or 0),
                "passthrough_sessions": int(h2_stats.get("passthrough_sessions") or 0),
            }
        details[gname] = entry
    return details


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
    keepalive_groups_mounted = False
    rules_ok = False
    python_direct_preempts_ai = False
    ai_manual_rules_count = 0
    ai_gemini_rules_count = 0
    egress_list: list[VergeEgressStatus] = []

    telemetry = collect_scheduler_telemetry(
        paths,
        max_heartbeat_age_sec=max_heartbeat_age_sec,
        now=ts,
    )
    last_verdicts = dict(telemetry.get("last_verdicts") or {})

    if controller_ok:
        try:
            groups_map = api.get_groups()
            groups_present = [g for g in REQUIRED_VERGE_GROUPS if g in groups_map]
            missing_groups = [g for g in REQUIRED_VERGE_GROUPS if g not in groups_map]
            topology_ok = len(missing_groups) == 0
            keepalive_groups_mounted = (
                "AI-MANUAL-KEEPALIVE" in groups_map and "AI-GEMINI-KEEPALIVE" in groups_map
            )

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
            seen_ai_rule = False
            for r in raw_rules if isinstance(raw_rules, list) else []:
                if not isinstance(r, dict):
                    continue
                proxy_target = str(r.get("proxy") or "")
                rtype = str(r.get("type") or "").lower().replace("-", "")
                payload = str(r.get("payload") or "").strip().lower()
                if proxy_target in ("AI-MANUAL", "AI-MANUAL-KEEPALIVE"):
                    ai_manual_rules_count += 1
                    seen_ai_rule = True
                elif proxy_target in ("AI-GEMINI", "AI-GEMINI-KEEPALIVE"):
                    ai_gemini_rules_count += 1
                    seen_ai_rule = True
                elif (
                    not seen_ai_rule
                    and proxy_target.upper() == "DIRECT"
                    and rtype == "processname"
                    and payload in ("python.exe", "python", "python3")
                ):
                    python_direct_preempts_ai = True
            rules_ok = ai_manual_rules_count > 0 and ai_gemini_rules_count > 0
        except Exception:
            rules_ok = False

    daemon_state = _load_daemon_state(paths)
    daemon_ok = bool(telemetry["daemon_ok"])
    daemon_pid = telemetry["daemon_pid"]
    pid_alive = bool(telemetry["daemon_pid_alive"])
    heartbeat_age_sec = telemetry["heartbeat_age_sec"]
    scheduler_mode = str(telemetry["scheduler_mode"])
    installed_timers = list(telemetry["installed_timers"])
    active_timers = list(telemetry["active_timers"])
    rebalance_age_sec = telemetry["rebalance_age_sec"]

    last_watch_at = daemon_state.get("last_watch_ts") or telemetry.get("updated_at")
    last_watch_age_sec: float | None = None
    if isinstance(last_watch_at, (int, float)) and last_watch_at > 0:
        last_watch_age_sec = max(0.0, ts - float(last_watch_at))
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
        else:
            keepalive_gw_active = any(
                isinstance(t, dict) and t.get("name") == "keepalive-gw"
                for t in (telemetry.get("tasks") or [])
            )
            if keepalive_gw_active and not keepalive_groups_mounted:
                remedies.append(
                    "本地 H2+WebSocket 保活网关 (127.0.0.1:7895) 已就绪，但内核尚未加载新版保活策略组 "
                    "(AI-MANUAL-KEEPALIVE)：请在 Clash Verge 界面点击一次「订阅 -> 刷新」以激活防 10054 保活链路。"
                )
            if python_direct_preempts_ai:
                remedies.append(
                    "检测到订阅局部脚本在 AI 规则前注入了 `PROCESS-NAME,python.exe,DIRECT`："
                    "后台 `cproxy-daemon.exe` 不受影响，但终端直接执行 `cproxy verge once` / `ai-watch` 探测会被强制直连超时。"
                )
        for eg in egress_list:
            if eg.present and eg.is_iepl:
                remedies.append(
                    f"{eg.group} 当前叶子节点 ({eg.leaf_node}) 为专线/IEPL，可执行 `cproxy verge once` 切换至非专线独立 IP。"
                )
    if not daemon_ok:
        if installed_timers:
            remedies.append(
                "调度定时器未运行或看护心跳超时：请执行 "
                "`systemctl --user enable --now cproxy-ai-watch.timer cproxy-ai-rebalance.timer`。"
            )
        else:
            remedies.append(
                "旁路守护进程未运行或心跳超时：Windows 下可双击 `setup-windows-verge.bat` "
                "或运行 `cproxy verge daemon` 启动后台看护。"
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
        heartbeat_age_sec=heartbeat_age_sec,
        watch_ok=watch_ok,
        last_watch_age_sec=round(last_watch_age_sec, 1) if last_watch_age_sec is not None else None,
        watch_runs=int(telemetry["watch_runs"]),
        rebalance_runs=int(telemetry["rebalance_runs"]),
        total_switches=int(telemetry["total_switches"]),
        switches_last_10m=switches_last_10m,
        switch_storm=switch_storm,
        total_reaped_connections=int(telemetry["total_reaped_connections"]),
        scheduler_mode=scheduler_mode,
        active_timers=active_timers,
        installed_timers=installed_timers,
        rebalance_age_sec=rebalance_age_sec,
        egress=egress_list,
        remedies=remedies,
    )
