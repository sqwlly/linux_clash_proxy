from __future__ import annotations

import sys
from argparse import Namespace
from pathlib import Path

from . import __version__
from .api import APIUnavailableError
from .cli_render import (
    _render_ai_connections,
    _render_ai_status,
    _render_connectivity_report,
    _render_current,
    _render_group_check,
    _render_incident,
    _render_ipcheck,
    _render_list_groups,
    _render_list_nodes,
    _render_logs,
    _render_refresh,
    _render_security_check,
    _render_shadow_history,
    _render_snapshots,
    _render_status,
    _render_traffic,
    _run_bootstrap,
    _run_rollback,
    _section_heading,
    _section_title,
    print_error,
)
from .config import default_paths
from .diagnostics import run_connectivity_test, test_group
from .install import init_user_layout, migrate_from_legacy
from .interactive import NotATerminalError, select_one
from .output import build_probe_output, build_root_parser, normalize_name, render_command_overview
from .process import ProcessOwnershipError, restart_process, start_process, stop_process
from .proxyenv import proxy_env_lines, run_proxy_shell, run_with_proxy
from .runtime import render_runtime
from .services.ops import guard
from .services.probe import ProbeService
from .services.query import QueryService
from .services.refresh import RefreshService
from .support import build_support_bundle


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
            return _run_bootstrap()
        if args.command == "migrate-from-legacy":
            config_file = migrate_from_legacy(default_paths(), Path(args.legacy_root))
            print(f"已迁移配置: {config_file}")
            return 0
        if args.command == "render":
            runtime_path = render_runtime(default_paths())
            print(f"已生成运行配置: {runtime_path}")
            return 0
        if args.command == "snapshots":
            return _render_snapshots(default_paths(), args.raw)
        if args.command == "rollback":
            return _run_rollback(default_paths(), args.name)
        if args.command == "refresh":
            report = RefreshService(default_paths()).refresh(
                subscription_url=args.subscription_url,
                groups=args.group,
            )
            return _render_refresh(report, args.raw)
        if args.command == "start":
            pid = start_process(default_paths())
            print(f"代理已启动 (PID: {pid})")
            return 0
        if args.command == "stop":
            stopped = stop_process(default_paths())
            if stopped:
                print("代理已停止")
            else:
                print("代理未运行")
            return 0
        if args.command == "restart":
            pid = restart_process(default_paths())
            print(f"代理已启动 (PID: {pid})")
            return 0
        if args.command == "logs":
            return _render_logs(args.lines, args.follow)
        if args.command == "status":
            return _render_status(args.raw, 0 if args.no_process else args.top)
        if args.command == "test":
            return _render_connectivity_report(run_connectivity_test(default_paths()))
        if args.command == "security-check":
            return _render_security_check(args.strict)
        if args.command == "support-bundle":
            output_path = Path(args.output) if args.output else None
            bundle_path = build_support_bundle(default_paths(), output_path)
            print(f"已生成支持包: {bundle_path}")
            return 0
        if args.command == "test-group":
            return _render_group_check(test_group(default_paths(), args.group), args.raw)
        if args.command == "proxy-env":
            for line in proxy_env_lines(default_paths()):
                print(line)
            return 0
        if args.command == "with-proxy":
            return run_with_proxy(default_paths(), args.command_args)
        if args.command == "proxy-shell":
            print(_section_title("进入临时代理 shell，退出后代理环境失效"))
            return run_proxy_shell(default_paths(), args.shell_args)
        if args.command in {"current", "list-groups", "list-nodes", "ai-status"}:
            service = QueryService(default_paths())
            if args.command == "current":
                return _render_current({args.group: service.get_group(args.group)}, args.group, args.raw)
            if args.command == "list-groups":
                return _render_list_groups(service.list_groups(), args.raw)
            if args.command == "list-nodes":
                return _render_list_nodes({args.group: service.get_group(args.group)}, args.group, args.raw)
            return _render_ai_status(service.get_ai_status_groups(), args.raw)
        if args.command == "switch":
            service = QueryService(default_paths())
            group_name, target_name = _resolve_switch(service, args)
            group = service.switch_group(group_name, target_name)
            print(_section_heading("结果"))
            print(f"代理组: {group_name}")
            print(f"当前选择: {normalize_name(group.current)}")
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
        if args.command == "traffic":
            return _render_traffic(
                default_paths(),
                action=args.action,
                days=args.days,
                by=args.by,
                top=args.top,
                raw=args.raw,
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
        if args.command == "completion":
            from .completion import run_completion

            return run_completion(args.shell, install=args.install)
        if args.command == "tui":
            from .tui.app import run_tui
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


def _resolve_switch(service: QueryService, args: Namespace) -> tuple[str, str]:
    """决定 switch 切到哪个组/节点；无法继续时以合适的退出码结束进程。

    三种情形：
      两个参数都给 → 原样返回（不碰 API、不进选择器，行为与改造前一致）
      只给了一个   → 维持改造前的「缺少必需参数」报错（退 2）
      一个都没给   → 进交互选择；非交互终端则给出用法与候选后退 2
    """
    if args.group is not None and args.target is not None:
        return args.group, args.target

    if args.group is not None:
        print("cproxy switch: 错误: 缺少必需参数: target", file=sys.stderr)
        print("用法: cproxy switch <代理组> <目标>", file=sys.stderr)
        raise SystemExit(2)

    # 需要**全部**组（不只可切换的那些）来解析候选延迟——候选很可能是组，
    # 得沿它当前出口往下钻才能拿到真实测速值
    all_groups = service.list_groups()
    groups = [g.name for g in all_groups if str(g.type).lower() in {"selector", "select"}]
    groups_by_name = {g.name: g for g in all_groups}
    if not groups:
        print("错误: 没有可手动切换的代理组", file=sys.stderr)
        raise SystemExit(1)

    try:
        group_name = select_one("选择代理组", groups, annotations=_group_roles(service, groups))
        if group_name is None:
            raise SystemExit(0)  # 用户主动取消，不是错误
        group = service.get_group(group_name)
        if not group.candidates:
            print(f"错误: 代理组 [{group_name}] 没有候选节点", file=sys.stderr)
            raise SystemExit(1)
        delays = service.node_delays()
        target_name = select_one(
            "选择节点",
            group.candidates,
            current=group.current,  # 高亮当前在用的那个
            annotations={
                name: _delay_label(_resolve_delay(name, groups_by_name, delays)) for name in group.candidates
            },
        )
    except NotATerminalError:
        _explain_switch_usage(groups)
        raise SystemExit(2) from None

    if target_name is None:
        raise SystemExit(0)
    return group_name, target_name


def _group_roles(service: QueryService, groups: list[str]) -> dict[str, str]:
    """选择器里每个组的角色标注。

    只标 **判据可靠** 的三个：`MATCH` 规则指向的组、`AI-MANUAL`、`GLOBAL`。
    其余一律不标——猜错的分类比没有分类更误导人（比如把某个「地区池」标成
    「订阅」会让人改错组，这正是这个标注要防的事）。
    """
    roles: dict[str, str] = {}
    match_group = service.match_rule_group()
    if match_group in groups:
        roles[match_group] = "默认路由 · 决定其余流量"
    if "AI-MANUAL" in groups:
        roles["AI-MANUAL"] = "AI 流量 · 决定 AI 出口"
    if "GLOBAL" in groups:
        roles["GLOBAL"] = "全局 · 绕过规则"
    return roles


def _delay_label(delay: int | None) -> str:
    """选择器里的延迟标注。

    `0` 是 mihomo 的**测速失败**标记，不是「极快」——直接显示 `0 ms` 会让人
    挑中实际不可用的节点。取不到记录则标 `-`，与「失败」区分开。
    """
    if delay is None:
        return "-"
    return "超时" if delay <= 0 else f"{delay} ms"


def _resolve_delay(name: str, groups: dict, delays: dict[str, int], depth: int = 0) -> int | None:
    """取某个候选的延迟。

    候选可能是节点，也可能是**组**——`AI-MANUAL` 这一层的候选大多是后者
    （`🇯🇵 Japan` 等 selector 组自己不测速，直接查只会得到空）。所以遇到组就
    沿它当前出口往下钻，直到拿到真实节点的测速值，这样各组的快慢才可比。
    """
    if name in delays:
        return delays[name]
    group = groups.get(name)
    # depth 兜底：配置异常时组之间可能互相引用成环
    if group is None or depth >= 8:
        return None
    current = getattr(group, "current", None)
    if not current or current == name:
        return None
    return _resolve_delay(str(current), groups, delays, depth + 1)


def _explain_switch_usage(groups: list[str]) -> None:
    """非交互终端下的降级说明：给出用法与可选分组，退出码沿用用法错误的 2。"""
    print("cproxy switch: 错误: 缺少参数，且当前不是交互终端", file=sys.stderr)
    print("用法: cproxy switch <代理组> <目标>", file=sys.stderr)
    print("可选的代理组:", file=sys.stderr)
    for name in groups:
        print(f"  {name}", file=sys.stderr)


def main() -> None:
    raise SystemExit(run(sys.argv[1:]))


if __name__ == "__main__":
    main()
