from __future__ import annotations

from argparse import REMAINDER, ArgumentParser
from collections.abc import Callable

from .cli_args import CliArgumentParser, CliHelpFormatter
from .services.probe import ProbeReport, format_delay, stable_score

# `cproxy status` 按进程明细的默认行数。定义在此处是因为它属于 CLI 契约，
# argparse 默认值与渲染层共用同一来源（渲染层不能反向 import 本模块之外的定义）。
STATUS_PROCESS_TOP_DEFAULT = 5


def normalize_name(value: object) -> str:
    if value in ("-", None):
        return "-"

    text = str(value).strip()
    parts = text.split(maxsplit=1)
    if len(parts) == 2 and parts[0] and all(not ch.isalnum() for ch in parts[0]):
        text = parts[1].strip()

    text = text.replace("丨", " ")
    text = text.replace("|", " ")
    return " ".join(text.split())


def build_probe_output(
    report: ProbeReport,
    raw: bool,
    section_title: Callable[[str], str] = str,
) -> tuple[list[str], int]:
    if raw:
        lines = [
            f"GROUP\t{report.group}",
            f"PROFILE\t{report.profile}",
            f"STRATEGY\t{report.strategy_name}",
            f"ROUNDS\t{report.rounds}",
            f"URL\t{report.url}",
            f"CURRENT\t{report.current or '-'}",
            f"CURRENT_STABLE\t{'true' if report.current_verdict.stable else 'false'}\t{report.current_verdict.reason}",
            f"STABLE\t{'true' if report.verdict.stable else 'false'}\t{report.verdict.reason}",
        ]
        if report.best:
            score = stable_score(report.best, report.rounds, report.strategy)
            lines.append(
                f"BEST\t{report.best.name}\tsuccess={report.best.success_count}/{report.rounds}"
                f"\tfailures={report.best.failures}\tavg={format_delay(report.best.avg_delay)}"
                f"\tmax={format_delay(report.best.max_delay)}\tscore={score}"
            )
        if report.switched:
            lines.append(f"SWITCH\t{report.group}\t{report.best.name if report.best else '-'}")
        if report.switch_requested and not report.switched:
            lines.append(f"SKIP_SWITCH\t{report.group}\t{report.skip_reason}")
        for summary in sorted(report.summaries, key=lambda item: item.rank_key()):
            score = stable_score(summary, report.rounds, report.strategy)
            lines.append(
                f"NODE\t{summary.name}\tsuccess={summary.success_count}/{report.rounds}"
                f"\tfailures={summary.failures}\tavg={format_delay(summary.avg_delay)}"
                f"\tmax={format_delay(summary.max_delay)}\tmin={format_delay(summary.min_delay)}\tscore={score}"
            )
    else:
        current_label = normalize_name(report.current) if report.current else "-"
        current_stable = "稳定" if report.current_verdict.stable else "不稳定"
        best_label = normalize_name(report.best.name) if report.best else "-"
        best_stable = "稳定" if report.verdict.stable else "不稳定"
        lines = [
            section_title("摘要"),
            f"目标组: {report.group}",
            f"配置: {report.profile} / {report.strategy_name}",
            f"探测: {report.rounds} 轮, {report.url}",
            f"当前: {current_label} ({current_stable})",
            f"推荐: {best_label} ({best_stable})",
        ]
        if report.best and report.switched:
            lines.append(f"切换: 已切换 {report.group} -> {best_label}")
        elif report.switch_requested:
            lines.append(f"切换: 未切换 ({report.skip_reason})")
        elif report.preview_reason:
            lines.append(f"切换预览: 不会切换 ({report.preview_reason})")
        elif report.best:
            lines.append(f"切换预览: 会切换到 {best_label}")
        lines.extend(["", section_title("结果"), *_probe_table_lines(report)])

    if report.switch_requested:
        request_satisfied = (
            report.switched
            or report.skip_reason == "当前已是推荐稳定节点"
            or (report.current_verdict.stable and report.skip_reason.startswith("当前节点也稳定"))
        )
        if not request_satisfied:
            return lines, 1
    return lines, 0 if report.best else 1


def _probe_table_lines(report: ProbeReport) -> list[str]:
    header = f"{'节点':<16} {'成功':>4}  {'失败':>4}  {'平均':>6}  {'最大':>6}  {'最小':>6}  {'score':>5}"
    lines = [header]
    for summary in sorted(report.summaries, key=lambda item: item.rank_key()):
        score = stable_score(summary, report.rounds, report.strategy)
        lines.append(
            f"{normalize_name(summary.name):<16} {summary.success_count}/{report.rounds:>3}  "
            f"{summary.failures:>4}  {format_delay(summary.avg_delay):>6}  "
            f"{format_delay(summary.max_delay):>6}  {format_delay(summary.min_delay):>6}  {score:>5}"
        )
    return lines


# 命令清单的**单一数据源**：分组名 → ((命令名, 一句话描述), ...)。
# 同时驱动两处，避免两处各维护一份而漂移：
#   1. 顶层 help 末尾的分组清单（`_command_list_epilog`）
#   2. 各子命令自身 `--help` 的 description（注册时查 `_COMMAND_HELP`）
# 新增命令只需在这里加一行。
#
# 分组名沿用 proxy.sh `usage()` 的既有词汇（配置与进程 / AI 路由控制 / 命令级代理 /
# 诊断与排障），与 cproxy 面板复刻 proxy.sh 视觉的做法一致；后两组为 cproxy 独有命令。
_COMMAND_GROUPS: tuple[tuple[str, tuple[tuple[str, str], ...]], ...] = (
    (
        "配置与进程",
        (
            ("init", "初始化用户配置目录"),
            ("bootstrap", "一键引导（无需参数）"),
            ("render", "由原始配置生成运行配置"),
            ("start", "启动代理进程"),
            ("stop", "停止代理进程"),
            ("restart", "重启代理进程"),
            ("logs", "查看 cproxy 日志输出"),
            ("status", "显示当前状态面板"),
            ("migrate-from-legacy", "从旧目录迁移配置"),
        ),
    ),
    (
        "AI 路由控制",
        (
            ("current", "查看代理组当前选择"),
            ("list-groups", "列出可切换的代理组"),
            ("list-nodes", "列出代理组候选项"),
            ("switch", "手动切换 Selector 代理组"),
            ("ai-status", "查看 AI 专用路由状态"),
            ("test-group", "测试代理组或节点健康情况"),
            ("ai-use", "探测并切换到稳定 AI 出口"),
            ("probe-stable-node", "多轮探测并推荐更稳定的节点"),
            ("shadow-probe", "只探测并记录历史，不切换"),
            ("shadow-history", "查看最近稳定探测历史"),
            ("guard", "先选稳定 AI 出口，再按需注入代理执行命令"),
            ("ai-connections", "查看 AI/GitHub 相关活动连接"),
            ("incident", "输出 AI 出口故障排查报告"),
        ),
    ),
    (
        "命令级代理",
        (
            ("proxy-env", "输出命令级代理环境变量"),
            ("with-proxy", "仅为单条命令注入代理环境"),
            ("proxy-shell", "打开临时代理 shell"),
        ),
    ),
    (
        "诊断与排障",
        (
            ("test", "测试代理连通性"),
            ("security-check", "校验本地 GA 安全配置"),
            ("support-bundle", "生成脱敏支持包"),
            ("ip-check", "检测出口 IP 纯净度"),
        ),
    ),
    (
        "流量与维护",
        (
            ("traffic", "流量统计报表或采集一次样本"),
            ("refresh", "更新订阅、渲染、重启并探测分组"),
            ("snapshots", "列出运行时/配置快照"),
            ("rollback", "回滚到历史快照"),
        ),
    ),
    (
        "交互界面",
        (
            ("tui", "启动终端 UI 面板"),
            ("completion", "输出或安装 shell 补全脚本"),
        ),
    ),
)

# 命令名 → 描述，由上面的分组表派生（注册子命令时按名取用）
_COMMAND_HELP: dict[str, str] = {
    name: summary for _, commands in _COMMAND_GROUPS for name, summary in commands
}

# 参数取值的单一来源：argparse 的 `choices=` 与 shell 补全（`completion.py`）
# 共用，避免同一条枚举在两处各写一份而漂移。
PROFILE_CHOICES = ("codex", "chatgpt", "github", "claude")
STRATEGY_CHOICES = ("conservative", "balanced", "aggressive")
TRAFFIC_ACTIONS = ("show", "collect", "audit")
TRAFFIC_DIMENSIONS = ("node", "rule", "host", "process")

# 命令名左对齐宽度：留出最长命令名（`migrate-from-legacy` 19 字符）的余量
_COMMAND_NAME_WIDTH = 24

_OVERVIEW_HINT = "提示: cproxy status 查看当前状态 · cproxy <命令> --help 查看详情"


def _command_list_lines() -> list[str]:
    """按分组渲染命令清单（分组标题 + `  命令名   - 描述`）。

    格式对齐 proxy.sh `usage()` 的既有语言：分组标题带冒号、命令两空格缩进、
    命令名与描述之间用 `-` 分隔。
    """
    lines: list[str] = []
    for group, commands in _COMMAND_GROUPS:
        if lines:
            lines.append("")
        lines.append(f"{group}：")
        lines.extend(f"  {name:<{_COMMAND_NAME_WIDTH}}- {summary}" for name, summary in commands)
    return lines


def _command_list_epilog() -> str:
    """顶层 `--help` 末尾的分组命令清单。"""
    return "\n".join([*_command_list_lines(), "", _OVERVIEW_HINT])


def render_command_overview() -> str:
    """不带参数运行 `cproxy` 时的引导输出：标题 + 分组命令清单 + 一行提示。

    相比 `--help` 省略 usage/options 段——用户敲空命令想看的是"能做什么"，
    而不是 argparse 的样板；逐参数帮助走 `cproxy <命令> --help`。
    """
    return "\n".join(["cproxy — 用户级 Mihomo 代理 CLI", "", *_command_list_lines(), "", _OVERVIEW_HINT])


def _localize_help_titles(parser: ArgumentParser) -> None:
    """把 argparse 的英文段落标题换成中文（`options` / `positional arguments`）。

    这两个是 argparse 的内部容器；用 getattr 取值，若未来改名则只是标题不变，
    不会抛异常。
    """
    optionals = getattr(parser, "_optionals", None)
    if optionals is not None:
        optionals.title = "选项"
    positionals = getattr(parser, "_positionals", None)
    if positionals is not None:
        positionals.title = "位置参数"


def _add_command(subparsers, name: str, *, raw: bool = False) -> ArgumentParser:
    """注册一个子命令。

    - `description` 自动取自 `_COMMAND_HELP`，命令表是唯一数据源
    - **不传 `help=`**：顶层 help 的命令清单改由 epilog 统一渲染，避免列两遍
    - `-h` 文案与段落标题统一为中文（argparse 默认输出英文）
    - `raw=True` 为该命令挂上 `--raw`；15 个子命令共用下面这一处定义

    注意 `--raw` **不能**改挂到根 parser 上：README 与脚本里一律是子命令后置写法
    （`cproxy status --raw`），提到根上后这些调用会被判成「子命令不认识 --raw」；
    而父子同时定义会触发 argparse 的 conflicting option string，整个 CLI 构造失败。
    """
    parser = subparsers.add_parser(name, description=_COMMAND_HELP[name], add_help=False, formatter_class=CliHelpFormatter)
    parser.add_argument("-h", "--help", action="help", help="显示本帮助并退出")
    if raw:
        parser.add_argument("--raw", action="store_true", help="输出机器可读格式")
    _localize_help_titles(parser)
    return parser


def build_root_parser() -> ArgumentParser:
    parser = CliArgumentParser(
        prog="cproxy",
        description="用户级 Mihomo 代理 CLI",
        epilog=_command_list_epilog(),
        formatter_class=CliHelpFormatter,
        add_help=False,
    )
    parser.add_argument("-h", "--help", action="help", help="显示本帮助并退出")
    parser.add_argument("--version", action="store_true", help="显示版本并退出")
    _localize_help_titles(parser)
    # metavar 抑制 usage 行把全部命令名罗列一遍（argparse 默认用 choices 展开）；
    # 子命令不传 help=（见 `_add_command`），命令清单只由 epilog 渲染一次。
    subparsers = parser.add_subparsers(dest="command", metavar="<命令>", title="命令")

    _add_command(subparsers, "init")
    _add_command(subparsers, "bootstrap")

    current_parser = _add_command(subparsers, "current", raw=True)
    current_parser.add_argument("group", help="代理组名")

    _add_command(subparsers, "list-groups", raw=True)

    nodes_parser = _add_command(subparsers, "list-nodes", raw=True)
    nodes_parser.add_argument("group", help="代理组名")

    switch_parser = _add_command(subparsers, "switch")
    switch_parser.add_argument("group", nargs="?", help="代理组名；与 target 一并省略时进入交互选择")
    switch_parser.add_argument("target", nargs="?", help="要切换到的节点或子组名")

    _add_command(subparsers, "ai-status", raw=True)

    migrate_parser = _add_command(subparsers, "migrate-from-legacy")
    migrate_parser.add_argument("legacy_root", help="旧仓库根目录")

    _add_command(subparsers, "render")
    _add_command(subparsers, "snapshots", raw=True)

    rollback_parser = _add_command(subparsers, "rollback")
    rollback_parser.add_argument("name", nargs="?", help="快照文件名（默认取最近一份运行时快照）")

    refresh_parser = _add_command(subparsers, "refresh", raw=True)
    refresh_parser.add_argument("--subscription-url", help="订阅地址（覆盖配置里的 subscription-url）")
    refresh_parser.add_argument("--group", action="append", default=[], help="要探测并自动切换的分组（可重复）")

    status_parser = _add_command(subparsers, "status", raw=True)
    status_parser.add_argument(
        "--top",
        type=int,
        default=STATUS_PROCESS_TOP_DEFAULT,
        help="按进程流量明细的行数（0 表示不显示，默认 5）",
    )
    status_parser.add_argument(
        "--no-process",
        action="store_true",
        help="不显示按进程流量明细（等价 --top 0）",
    )
    _add_command(subparsers, "start")
    _add_command(subparsers, "stop")
    _add_command(subparsers, "restart")
    logs_parser = _add_command(subparsers, "logs")
    logs_parser.add_argument("--lines", type=int, default=50, help="显示末尾行数（默认 50）")
    logs_parser.add_argument("--follow", action="store_true", help="持续跟踪新增日志")

    _add_command(subparsers, "test")

    security_parser = _add_command(subparsers, "security-check")
    security_parser.add_argument("--strict", action="store_true", help="将告警视为失败")

    support_parser = _add_command(subparsers, "support-bundle")
    support_parser.add_argument("--output", help="输出 tar.gz 路径")

    test_group_parser = _add_command(subparsers, "test-group", raw=True)
    test_group_parser.add_argument("group", help="代理组名")

    _add_command(subparsers, "proxy-env")

    with_proxy_parser = _add_command(subparsers, "with-proxy")
    with_proxy_parser.add_argument("command_args", nargs=REMAINDER, help="要执行的命令及其参数")

    proxy_shell_parser = _add_command(subparsers, "proxy-shell")
    proxy_shell_parser.add_argument("shell_args", nargs=REMAINDER, help="传给 shell 的参数")

    probe_parser = _add_command(subparsers, "probe-stable-node", raw=True)
    probe_parser.add_argument("group", nargs="?", default="AI-MANUAL", help="代理组名（默认 AI-MANUAL）")
    probe_parser.add_argument(
        "--profile", choices=PROFILE_CHOICES, default="codex", help="AI 场景（默认 codex）"
    )
    probe_parser.add_argument("--strategy", choices=STRATEGY_CHOICES, help="稳定性策略")
    probe_parser.add_argument("--url", help="探测目标 URL（默认取场景预设）")
    probe_parser.add_argument("--rounds", type=int, help="探测轮数（默认取场景预设）")
    probe_parser.add_argument("--timeout", type=int, default=8000, help="单次探测超时（毫秒，默认 8000）")
    probe_parser.add_argument("--switch", action="store_true", help="推荐节点稳定且明显更优时自动切换")
    probe_parser.add_argument("--record-history", action="store_true", help="记录本次探测历史")

    shadow_probe_parser = _add_command(subparsers, "shadow-probe", raw=True)
    shadow_probe_parser.add_argument(
        "profile", nargs="?", default="codex", choices=PROFILE_CHOICES, help="AI 场景（默认 codex）"
    )
    shadow_probe_parser.add_argument("--group", default="AI-MANUAL", help="代理组名（默认 AI-MANUAL）")
    shadow_probe_parser.add_argument("--strategy", choices=STRATEGY_CHOICES, help="稳定性策略")
    shadow_probe_parser.add_argument("--url", help="探测目标 URL（默认取场景预设）")
    shadow_probe_parser.add_argument("--rounds", type=int, help="探测轮数（默认取场景预设）")
    shadow_probe_parser.add_argument("--timeout", type=int, default=8000, help="单次探测超时（毫秒，默认 8000）")

    shadow_history_parser = _add_command(subparsers, "shadow-history", raw=True)
    shadow_history_parser.add_argument("--limit", type=int, default=5, help="显示条数（默认 5）")

    guard_parser = _add_command(subparsers, "guard")
    guard_parser.add_argument(
        "profile", nargs="?", default="codex", choices=PROFILE_CHOICES, help="AI 场景（默认 codex）"
    )
    guard_parser.add_argument("command_args", nargs=REMAINDER, help="要注入代理执行的命令（需以 -- 分隔）")

    _add_command(subparsers, "ai-connections", raw=True)

    incident_parser = _add_command(subparsers, "incident")
    incident_parser.add_argument(
        "profile", nargs="?", default="codex", choices=PROFILE_CHOICES, help="AI 场景（默认 codex）"
    )

    ai_use_parser = _add_command(subparsers, "ai-use", raw=True)
    ai_use_parser.add_argument(
        "profile", nargs="?", default="codex", choices=PROFILE_CHOICES, help="AI 场景（默认 codex）"
    )
    ai_use_parser.add_argument("--group", default="AI-MANUAL", help="代理组名（默认 AI-MANUAL）")

    traffic_parser = _add_command(subparsers, "traffic", raw=True)
    traffic_parser.add_argument(
        "action", nargs="?", choices=TRAFFIC_ACTIONS, default="show", help="报表动作（默认 show）"
    )
    traffic_parser.add_argument("--days", type=int, default=1, help="统计窗口天数（默认当天）")
    traffic_parser.add_argument("--by", choices=TRAFFIC_DIMENSIONS, help="只展示某个维度的明细")
    traffic_parser.add_argument("--top", type=int, default=15, help="每张明细表的行数（默认 15）")

    ipcheck_parser = _add_command(subparsers, "ip-check", raw=True)
    ipcheck_parser.add_argument("--ip", help="检测指定 IP，而非当前代理出口")
    ipcheck_parser.add_argument("--group", help="检测期间临时切换的分组（给了 --node 时默认 CyberGuard）")
    ipcheck_parser.add_argument("--node", help="检测期间走该节点出口（临时切换分组，结束后自动恢复）")
    ipcheck_parser.add_argument("--timeout", type=int, default=30, help="请求超时秒数（默认 30）")

    _add_command(subparsers, "tui")

    completion_parser = _add_command(subparsers, "completion")
    completion_parser.add_argument("shell", choices=("bash", "zsh"), help="目标 shell")
    completion_parser.add_argument("--install", action="store_true", help="写入该 shell 的标准补全目录，而非打印到 stdout")
    return parser
