from __future__ import annotations

from argparse import ArgumentParser, REMAINDER


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


def build_root_parser() -> ArgumentParser:
    parser = ArgumentParser(prog="cproxy", description="User-level Mihomo proxy CLI")
    parser.add_argument("--version", action="store_true", help="Show version and exit")
    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser("init", help="Initialize user config directories")
    subparsers.add_parser("bootstrap", help="One-click bootstrap without arguments")

    current_parser = subparsers.add_parser("current", help="Show current proxy selection")
    current_parser.add_argument("group")
    current_parser.add_argument("--raw", action="store_true")

    groups_parser = subparsers.add_parser("list-groups", help="List switchable proxy groups")
    groups_parser.add_argument("--raw", action="store_true")

    nodes_parser = subparsers.add_parser("list-nodes", help="List group candidates")
    nodes_parser.add_argument("group")
    nodes_parser.add_argument("--raw", action="store_true")

    switch_parser = subparsers.add_parser("switch", help="Switch selector group target")
    switch_parser.add_argument("group")
    switch_parser.add_argument("target")

    ai_status_parser = subparsers.add_parser("ai-status", help="Show AI routing status")
    ai_status_parser.add_argument("--raw", action="store_true")

    migrate_parser = subparsers.add_parser("migrate-from-legacy", help="Import config from legacy repo")
    migrate_parser.add_argument("legacy_root")

    subparsers.add_parser("render", help="Render runtime config")
    status_parser = subparsers.add_parser("status", help="Show current status")
    status_parser.add_argument("--raw", action="store_true")
    subparsers.add_parser("start", help="Start proxy process")
    subparsers.add_parser("stop", help="Stop proxy process")
    subparsers.add_parser("restart", help="Restart proxy process")
    logs_parser = subparsers.add_parser("logs", help="Show cproxy log output")
    logs_parser.add_argument("--lines", type=int, default=50)
    logs_parser.add_argument("--follow", action="store_true")

    subparsers.add_parser("test", help="Test proxy connectivity")

    test_group_parser = subparsers.add_parser("test-group", help="Test group or node health")
    test_group_parser.add_argument("group")
    test_group_parser.add_argument("--raw", action="store_true")

    subparsers.add_parser("proxy-env", help="Print proxy environment variables")

    with_proxy_parser = subparsers.add_parser("with-proxy", help="Run one command with proxy env")
    with_proxy_parser.add_argument("command_args", nargs=REMAINDER)

    proxy_shell_parser = subparsers.add_parser("proxy-shell", help="Open a temporary proxy shell")
    proxy_shell_parser.add_argument("shell_args", nargs=REMAINDER)
    return parser
