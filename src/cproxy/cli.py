from __future__ import annotations

import sys
import time
from argparse import Namespace
from pathlib import Path

from . import __version__
from .api import APIBackend, APIUnavailableError
from .cli_render import (
    _accent,
    _render_ai_connections,
    _render_ai_status,
    _render_benchmark,
    _render_connectivity_report,
    _render_current,
    _render_doctor,
    _render_group_check,
    _render_incident,
    _render_ipcheck,
    _render_list_groups,
    _render_list_nodes,
    _render_logs,
    _render_refresh,
    _render_security_check,
    _render_shadow_history,
    _render_snapshot_diff,
    _render_snapshots,
    _render_status,
    _render_traffic,
    _render_why,
    _run_ban,
    _run_bootstrap,
    _run_compact_snapshots,
    _run_export,
    _run_flush,
    _run_import,
    _run_notify,
    _run_prompt,
    _run_rollback,
    _run_rule,
    _run_unban,
    _section_heading,
    _section_title,
    print_error,
)
from .config import default_paths
from .diagnostics import run_connectivity_test, test_group
from .feedback import Spinner
from .install import init_user_layout, migrate_from_legacy
from .interactive import NotATerminalError, select_one
from .output import build_probe_output, build_root_parser, normalize_name, render_command_overview
from .process import ProcessBackend, ProcessOwnershipError, restart_process, start_process, stop_process
from .proxyenv import proxy_env_lines, run_proxy_shell, run_with_proxy
from .runtime import render_runtime
from .services.ai_watch import VERDICT_LABELS, AIWatchService
from .services.ops import guard
from .services.probe import ProbeService, resolve_current_leaf
from .services.query import QueryService
from .services.rebalance import RebalanceService
from .services.refresh import RefreshService
from .services.speed import RateMeter, fetch_total_traffic, format_rate, measure_once
from .services.switch_tree import (
    build_switch_tree,
    extra_subscription_names,
    level_title,
    resolve_group_query,
)
from .services.switch_tree import (
    delay_label as _delay_label,
)
from .services.switch_tree import (
    resolve_delay as _resolve_delay,
)
from .services.tun import TunService
from .structured_output import emit_json
from .support import build_support_bundle


def _run_speed(args: Namespace) -> int:
    """实时网速：一次性测速，或 `--watch` 持续刷新。"""
    api = APIBackend(default_paths())
    if not args.watch:
        try:
            rate = measure_once(api, window=1.0)
        except APIUnavailableError:
            print_error("错误: 无法连接 Mihomo API（代理可能未运行）")
            return 1
        if rate is None:
            print_error("错误: 采样失败")
            return 1
        if args.raw:
            print(f"{rate.download_bps:.1f} {rate.upload_bps:.1f}")
        else:
            print(_section_heading("实时网速"))
            print(f"↓ 下载  {format_rate(rate.download_bps)}")
            print(f"↑ 上传  {format_rate(rate.upload_bps)}")
        return 0

    meter = RateMeter()
    interval = max(0.5, float(args.interval))
    try:
        while True:
            try:
                up, down = fetch_total_traffic(api)
            except APIUnavailableError:
                # API 暂不可用：跳过这一帧，不把 (0,0) 喂给 RateMeter，
                # 否则会污染速率基线、恢复后算出「累计流量÷几秒」的假尖峰
                time.sleep(interval)
                continue
            rate = meter.sample(up, down)
            if rate is not None:
                if args.raw:
                    print(f"{rate.download_bps:.1f} {rate.upload_bps:.1f}", flush=True)
                else:
                    print(
                        f"\r↓ {format_rate(rate.download_bps)}   ↑ {format_rate(rate.upload_bps)}  ",
                        end="",
                        flush=True,
                    )
            time.sleep(interval)
    except KeyboardInterrupt:
        if not args.raw:
            print()
        return 0


def run(argv: list[str] | None = None) -> int:
    args_list = list(sys.argv[1:] if argv is None else argv)
    # `__complete` 走早短路：不进 argparse，因此不会出现在任何 help/usage/choices 里，
    # 也省掉构建 34 个子解析器的开销（shell 每按一次 <Tab> 都会调它一次）。
    if args_list and args_list[0] == "__complete":
        from .completion import run_complete

        return run_complete(default_paths(), args_list[1:])
    parser = build_root_parser()
    args: Namespace = parser.parse_args(args_list)
    try:
        if args.version:
            print(__version__)
            return 0
        if args.command is None:
            # 不带参数时给引导而非静默退出——这是用户与新 CLI 的第一次接触
            print(render_command_overview())
            return 0
        if args.command == "init":
            config_file = init_user_layout(default_paths())
            print(f"已初始化配置: {config_file}")
            return 0
        if args.command == "bootstrap":
            return _run_bootstrap(args.subscription_url)
        if args.command == "migrate-from-legacy":
            config_file = migrate_from_legacy(default_paths(), Path(args.legacy_root))
            print(f"已迁移配置: {config_file}")
            return 0
        if args.command == "render":
            paths = default_paths()
            apply_requested = bool(getattr(args, "apply", False))
            old_text = ""
            was_stale_before_render = False
            process = ProcessBackend(paths) if apply_requested else None
            if apply_requested and process is not None:
                from .config import runtime_file as _rt_file

                prev_rt = _rt_file(paths)
                if prev_rt.exists():
                    try:
                        old_text = prev_rt.read_text(encoding="utf-8")
                    except OSError:
                        old_text = ""
                if process.is_running():
                    was_stale_before_render = process.is_runtime_stale(prev_rt)
            runtime_path = render_runtime(paths)
            print(f"已生成运行配置: {runtime_path}")
            if apply_requested and process is not None:
                if process.is_running():
                    from .backend.runtime import (
                        _extract_proxy_servers,
                        _mihomo_in_memory_using_stale_104,
                        is_only_ai_group_order_change,
                    )
                    from .services.ai_watch import has_any_inflight_ai_connection

                    new_text = ""
                    try:
                        new_text = Path(runtime_path).read_text(encoding="utf-8")
                    except OSError:
                        new_text = ""
                    drift_104 = _mihomo_in_memory_using_stale_104(paths, _extract_proxy_servers(new_text))
                    if process.is_runtime_stale(runtime_path) or drift_104:
                        api = APIBackend(paths)
                        if (
                            not drift_104
                            and not was_stale_before_render
                            and is_only_ai_group_order_change(old_text, new_text)
                        ):
                            process.mark_runtime_applied(runtime_path)
                        elif drift_104 or not has_any_inflight_ai_connection(paths, api):
                            api.reload_config(str(runtime_path))
                            process.mark_runtime_applied(runtime_path)
                            print("已热重载运行配置")
            return 0
        if args.command == "tun":
            if args.action == "status" and args.apply:
                print_error("错误: --apply 只能与 tun on/off 一起使用")
                return 1
            tun_service = TunService(default_paths())
            tun_report = (
                tun_service.status() if args.action == "status"
                else tun_service.configure(args.action == "on", apply=args.apply)
            )
            if args.json:
                emit_json("tun", tun_report, warnings=tun_report.warnings)
            else:
                labels = {True: "开启", False: "关闭", None: "未知"}
                print(f"配置 TUN: {labels[tun_report.configured_enabled]}")
                print(f"内核 TUN: {labels[tun_report.kernel_enabled]}")
                print(f"TUN 网卡: {tun_report.device or '未指定'}")
                interface_label = {True: "是", False: "否", None: "未知"}[tun_report.interface_present]
                print(f"网卡存在: {interface_label}")
                for issue in tun_report.prerequisites + tun_report.warnings:
                    print(f"提示: {issue}")
            return 0
        if args.command == "snapshots":
            if getattr(args, "action", "list") == "compact":
                return _run_compact_snapshots(
                    default_paths(),
                    kind=getattr(args, "kind", None),
                    dry_run=getattr(args, "dry_run", False),
                    raw=args.raw,
                    as_json=args.json,
                )
            if getattr(args, "action", "list") == "diff":
                return _render_snapshot_diff(
                    default_paths(),
                    target_name=getattr(args, "target", None),
                    raw=args.raw,
                    as_json=args.json,
                )
            return _render_snapshots(default_paths(), args.raw, as_json=args.json)
        if args.command == "rollback":
            return _run_rollback(default_paths(), args.name)
        if args.command == "refresh":
            with Spinner("正在刷新订阅并应用配置…", enabled=not (args.raw or args.json)) as spinner:
                report = RefreshService(default_paths()).refresh(
                    subscription_url=args.subscription_url,
                    groups=args.group,
                    on_stage=spinner.update,
                )
            return _render_refresh(report, args.raw, args.json)
        if args.command == "start":
            with Spinner("正在启动代理…", delay=0.3):
                pid = start_process(default_paths())
            print(f"代理已启动 (PID: {pid})")
            return 0
        if args.command == "stop":
            with Spinner("正在停止代理…", delay=0.3):
                stopped = stop_process(default_paths())
            if stopped:
                print("代理已停止")
            else:
                print("代理未运行")
            return 0
        if args.command == "restart":
            with Spinner("正在重启代理…", delay=0.3):
                pid = restart_process(default_paths())
            print(f"代理已启动 (PID: {pid})")
            return 0
        if args.command == "logs":
            return _render_logs(args.lines, args.follow)
        if args.command == "status":
            return _render_status(args.raw, 0 if args.no_process else args.top, args.json)
        if args.command == "test":
            with Spinner("正在测试连通性…", enabled=not args.json):
                connectivity_report = run_connectivity_test(default_paths())
            return _render_connectivity_report(connectivity_report, args.json)
        if args.command == "doctor":
            return _render_doctor(args.json)
        if args.command == "security-check":
            return _render_security_check(args.strict)
        if args.command == "support-bundle":
            output_path = Path(args.output) if args.output else None
            bundle_path = build_support_bundle(default_paths(), output_path)
            print(f"已生成支持包: {bundle_path}")
            return 0
        if args.command == "test-group":
            with Spinner(f"正在测试分组 {args.group}…", enabled=not (args.raw or args.json)):
                group_report = test_group(default_paths(), args.group)
            return _render_group_check(group_report, args.raw, args.json)
        if args.command == "proxy-env":
            for line in proxy_env_lines(default_paths()):
                print(line)
            return 0
        if args.command == "with-proxy":
            return run_with_proxy(default_paths(), args.command_args)
        if args.command == "proxy-shell":
            print(_section_title("进入临时代理 shell，退出后代理环境失效"))
            return run_proxy_shell(default_paths(), args.shell_args)
        if args.command in {"current", "list-groups", "list-nodes", "ai-status", "groups", "group"}:
            service = QueryService(default_paths())
            api_feedback = Spinner("正在连接 Mihomo API…", delay=0.4, enabled=not (args.raw or args.json))
            if args.command == "current":
                with api_feedback:
                    groups_by_name, tree = _switch_tree_for(service)
                group_name = resolve_group_query(args.group, groups_by_name, tree)
                return _render_current(groups_by_name, group_name, args.raw, args.json)
            if args.command in {"list-groups", "groups", "group"}:
                with api_feedback:
                    groups_by_name, tree = _switch_tree_for(service)
                return _render_list_groups(list(groups_by_name.values()), args.raw, args.json, tree=tree)
            if args.command == "list-nodes":
                with api_feedback:
                    groups_by_name, tree = _switch_tree_for(service)
                return _render_list_nodes(groups_by_name, args.group, args.raw, args.json, tree=tree)
            with api_feedback:
                ai_groups = service.get_ai_status_groups()
            return _render_ai_status(ai_groups, args.raw, args.json)
        if args.command == "switch":
            service = QueryService(default_paths())
            path = _resolve_switch(service, args)
            root_group = path[0][0]
            snapshot = service.groups_snapshot
            old_leaf = resolve_current_leaf(snapshot, root_group) if snapshot else None
            group = service.switch_path(path)
            if len(path) == 1:
                root_group = group.name
            if not old_leaf and service.last_switch_from and service.last_switch_from != "-":
                old_leaf = resolve_current_leaf(service.groups_snapshot, service.last_switch_from) or service.last_switch_from
            snapshot = service.groups_snapshot
            new_leaf = resolve_current_leaf(snapshot, root_group)
            old_selection = normalize_name(old_leaf) if old_leaf else None
            new_selection = normalize_name(new_leaf or group.current)
            delays = {name: item.delay for name, item in snapshot.items() if item.delay is not None}
            new_delay = _resolve_delay(new_leaf or group.current, snapshot, delays)

            print(_section_heading("结果"))
            print(f"代理组: {root_group}")
            if old_selection and old_selection != new_selection:
                print(f"切换: {old_selection} → {_accent(new_selection)}", end="")
            else:
                print(f"当前选择: {_accent(new_selection)}", end="")
            if new_delay is not None and new_delay > 0:
                print(f"  ({_delay_label(new_delay)})")
            else:
                print()
            return 0
        if args.command == "probe-stable-node":
            probe_report = ProbeService(default_paths()).probe(
                group=args.group,
                profile=args.profile,
                strategy_name=args.strategy,
                url=args.url,
                rounds=args.rounds,
                timeout=args.timeout,
                switch=args.switch,
                record_history=args.record_history,
                show_progress=not args.raw,
            )
            output_lines, exit_code = build_probe_output(probe_report, args.raw, _section_heading)
            print("\n".join(output_lines))
            return exit_code
        if args.command == "shadow-probe":
            probe_report = ProbeService(default_paths()).probe(
                group=args.group,
                profile=args.profile,
                strategy_name=args.strategy,
                url=args.url,
                rounds=args.rounds,
                timeout=args.timeout,
                switch=False,
                record_history=True,
                show_progress=not args.raw,
            )
            output_lines, exit_code = build_probe_output(probe_report, args.raw, _section_heading)
            print("\n".join(output_lines))
            return exit_code
        if args.command == "shadow-history":
            return _render_shadow_history(default_paths(), args.limit, args.raw)
        if args.command == "ai-rebalance":
            try:
                # 各自独立的变量名：三种 report 类型不同，共用一个名字会让类型互相污染
                rebalance_report = RebalanceService(default_paths()).rebalance(dry_run=args.dry_run)
            except APIUnavailableError:
                # 定时任务在代理未运行时无需报错：跳过本轮，下一轮再排
                print("代理未运行，跳过稳定度重排")
                return 0
            print(_section_heading("结果"))
            print(f"稳定度排序（{len(rebalance_report.ordered)} 个节点）:")
            for index, name in enumerate(rebalance_report.ordered, start=1):
                print(f"  {index}. {name}")
            if rebalance_report.gemini_ordered:
                print(f"AI-GEMINI 稳定度排序（{len(rebalance_report.gemini_ordered)} 个节点）:")
                for index, name in enumerate(rebalance_report.gemini_ordered, start=1):
                    print(f"  {index}. {name}")
            if rebalance_report.rerendered:
                print(f"已写入稳定度排序: {rebalance_report.order_path}")
                if rebalance_report.gemini_order_path:
                    print(f"已写入 AI-GEMINI 排序: {rebalance_report.gemini_order_path}")
                print("运行配置: " + ("已热重载" if rebalance_report.hot_reloaded else "已渲染（代理未运行）"))
            if rebalance_report.switched_to:
                print(f"出口切换: {rebalance_report.switched_to}")
            if rebalance_report.gemini_switched_to:
                print(f"AI-GEMINI 出口切换: {rebalance_report.gemini_switched_to}")
            return 0
        if args.command == "ai-watch":
            watch_report = AIWatchService(default_paths()).watch(dry_run=args.dry_run)
            if not watch_report.results:
                print("代理未运行，跳过 AI 出口看护")
                return 0
            print(_section_heading("AI 出口看护"))
            for item in watch_report.results:
                verdict = VERDICT_LABELS.get(item.verdict, item.verdict)
                line = f"{item.group}: {verdict}"
                if item.current:
                    line += f"（{item.current}）"
                if item.switched and item.target:
                    line += f" → 已切换 {item.target}"
                print(line)
            if watch_report.reaped_pids:
                pids_str = ", ".join(str(pid) for pid in watch_report.reaped_pids)
                action_label = "发现可清理残留 agy 进程" if args.dry_run else "已清理残留 agy 进程"
                print(f"{action_label}: {pids_str}")
            return 0
        if args.command == "guard":
            command = args.command_args
            if command and command[0] == "--":
                command = command[1:]
            return guard(default_paths(), profile=args.profile, command=command or None)
        if args.command == "ai-connections":
            return _render_ai_connections(default_paths(), args.raw)
        if args.command == "incident":
            return _render_incident(default_paths(), args.profile)
        if args.command == "ai-use":
            probe_report = ProbeService(default_paths()).probe(
                group=args.group,
                profile=args.profile,
                switch=True,
                show_progress=not args.raw,
            )
            output_lines, exit_code = build_probe_output(probe_report, args.raw, _section_heading)
            print("\n".join(output_lines))
            return exit_code
        if args.command == "speed":
            return _run_speed(args)
        if args.command == "traffic":
            return _render_traffic(
                default_paths(),
                action=args.action,
                days=args.days,
                by=args.by,
                top=args.top,
                raw=args.raw,
                json_output=args.json,
            )
        if args.command == "ip-check":
            return _render_ipcheck(
                default_paths(),
                ip=args.ip,
                group=args.group,
                node=args.node,
                timeout=args.timeout,
                raw=args.raw,
            )
        if args.command == "why":
            return _render_why(
                default_paths(),
                args.target,
                process=args.process,
                raw=args.raw,
                as_json=args.json,
            )
        if args.command == "ban":
            return _run_ban(
                default_paths(),
                args.pattern,
                hours=args.hours,
                reason=args.reason,
                apply=args.apply,
                raw=args.raw,
                as_json=args.json,
            )
        if args.command == "unban":
            return _run_unban(
                default_paths(),
                args.pattern,
                clear_all=args.clear_all,
                apply=args.apply,
            )
        if args.command == "notify":
            return _run_notify(
                default_paths(),
                args.action,
                webhook=args.webhook,
                desktop=args.desktop,
                title=args.title,
                message=args.message,
                as_json=args.json,
            )
        if args.command == "prompt":
            return _run_prompt(default_paths(), fmt=args.format, raw=args.raw)
        if args.command == "rule":
            return _run_rule(
                default_paths(),
                args.action,
                args.target,
                args.policy,
                rule_type=args.rule_type,
                comment=args.comment,
                clear_all=args.clear_all,
                apply=args.apply,
                raw=args.raw,
                as_json=args.json,
            )
        if args.command == "flush":
            return _run_flush(
                default_paths(),
                args.target,
                stale=args.stale,
                raw=args.raw,
                as_json=args.json,
            )
        if args.command == "benchmark":
            return _render_benchmark(
                default_paths(),
                raw=args.raw,
                as_json=args.json,
            )
        if args.command == "export":
            return _run_export(
                default_paths(),
                output=args.output,
                redact=args.redact,
                as_json=args.json,
            )
        if args.command == "import":
            return _run_import(
                default_paths(),
                args.file,
                replace=args.replace,
                apply=args.apply,
                as_json=args.json,
            )
        if args.command == "completion":
            from .completion import run_completion

            return run_completion(args.shell, install=args.install)
        if args.command == "tui":
            from .tui_launcher import run_tui
            run_tui(default_paths())
            return 0
        return 0
    except SystemExit as exc:
        # 服务层大量用 `raise SystemExit("错误: ...")` 表达用户可修复的错误，
        # 那条路径由解释器直接打到 stderr、绕开这里的着色。字符串 code 统一走
        # `print_error`；int code（argparse 的 exit 2、以及 SystemExit(0)）原样
        # 上抛，退出码契约不变。
        if isinstance(exc.code, str):
            print_error(exc.code)
            return 1
        raise
    except (APIUnavailableError, ProcessOwnershipError, FileNotFoundError, RuntimeError, ValueError) as exc:
        print_error(str(exc))
        return 1


def _resolve_switch(service: QueryService, args: Namespace) -> list[tuple[str, str]]:
    """决定 switch 切到哪个组/节点；无法继续时以合适的退出码结束进程。

    三种情形：
      两个参数都给 → 原样返回（不碰 API、不进选择器，行为与改造前一致）
      只给了一个   → 维持改造前的「缺少必需参数」报错（退 2）
      一个都没给   → 进交互选择；非交互终端则给出用法与候选后退 2

    返回值是 (组, 目标) 的切换路径；嵌套组下选叶子节点时会含多级 selector。
    """
    if args.group is not None and args.target is not None:
        return [(args.group, args.target)]

    if args.group is not None:
        # 走 print_error 让整条上色，跟 run() 里 SystemExit(str) 的着色一致——
        # 之前裸 print 出来的错误在 TTY 下是普通黑字，跟其它错误的红字混在一起
        # 反而显得不严肃。
        print_error("cproxy switch: 错误: 缺少必需参数: target")
        print_error("用法: cproxy switch <代理组> <目标>")
        raise SystemExit(2)

    all_groups = service.list_groups()
    groups_by_name = {group.name: group for group in all_groups}
    delays = {group.name: group.delay for group in all_groups if group.delay is not None}
    entries = build_switch_tree(
        groups_by_name,
        match_group=service.match_rule_group(),
        extra_names=_extra_names_for_switch(service),
        delays=delays,
    )
    if not entries:
        print_error("错误: 没有可手动切换的代理组")
        raise SystemExit(1)

    last_display: str | None = None
    default_display = next((entry.display for entry in entries if entry.current), None)
    try:
        while True:
            entry_item = select_one(
                "选择入口",
                [entry.display for entry in entries],
                current=last_display or default_display,
                annotations={entry.display: entry.annotation for entry in entries if entry.annotation},
            )
            if entry_item is None:
                raise SystemExit(0)
            entry = next(item for item in entries if item.display == entry_item)
            last_display = entry.display
            children = entry.children
            if not children:
                print_error(f"错误: 代理组 [{entry.key}] 没有候选节点")
                raise SystemExit(1)
            while True:
                region_item = select_one(
                    level_title(children),
                    [child.display for child in children],
                    current=next((child.display for child in children if child.current), None),
                    annotations={child.display: child.annotation for child in children if child.annotation},
                    annotation_styles={
                        child.display: child.annotation_style for child in children if child.annotation_style
                    },
                    back_hint=True,
                )
                if region_item is None:
                    break
                child = next(item for item in children if item.display == region_item)
                if child.drillable:
                    node_item = select_one(
                        "选择节点",
                        [node.display for node in child.children],
                        current=next((node.display for node in child.children if node.current), None),
                        annotations={
                            node.display: node.annotation for node in child.children if node.annotation
                        },
                        annotation_styles={
                            node.display: node.annotation_style
                            for node in child.children
                            if node.annotation_style
                        },
                        back_hint=True,
                    )
                    if node_item is None:
                        continue
                    node = next(item for item in child.children if item.display == node_item)
                    return list(node.path)
                return list(child.path)
    except NotATerminalError:
        _explain_switch_usage(entries)
        raise SystemExit(2) from None


def _extra_names_for_switch(service: QueryService) -> list[str]:
    paths = getattr(service, "paths", None)
    if paths is None:
        return []
    try:
        from .config import read_config

        return extra_subscription_names(read_config(paths))
    except Exception:
        return []


def _switch_tree_for(service: QueryService):
    all_groups = service.list_groups()
    groups_by_name = {group.name: group for group in all_groups}
    delays = {group.name: group.delay for group in all_groups if group.delay is not None}
    entries = build_switch_tree(
        groups_by_name,
        match_group=service.match_rule_group(),
        extra_names=_extra_names_for_switch(service),
        delays=delays,
    )
    return groups_by_name, entries


def _explain_switch_usage(entries) -> None:
    """非交互终端下的降级说明：给出用法与可选入口，退出码沿用用法错误的 2。"""
    print_error("cproxy switch: 错误: 缺少参数，且当前不是交互终端")
    print_error("用法: cproxy switch <代理组> <目标>")
    print_error("可选入口:")
    for entry in entries:
        print_error(f"  {entry.display}  ({entry.key})")



def main() -> None:
    raise SystemExit(run(sys.argv[1:]))


if __name__ == "__main__":
    main()
