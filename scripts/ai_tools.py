#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


REQUEST_TIMEOUT_SECONDS = 10
AI_HOST_KEYWORDS = ("chatgpt", "openai", "claude", "anthropic", "github")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="AI route diagnostics for clash-proxy.")
    parser.add_argument("--controller", required=True)
    parser.add_argument("--secret", default="")
    subparsers = parser.add_subparsers(dest="command", required=True)

    connections = subparsers.add_parser("connections")
    connections.add_argument("--raw", action="store_true")

    history = subparsers.add_parser("history")
    history.add_argument("--history-file", required=True)
    history.add_argument("--limit", type=int, default=5)
    history.add_argument("--raw", action="store_true")

    return parser.parse_args()


def request_json(controller: str, secret: str, path: str) -> dict:
    headers = {"Authorization": f"Bearer {secret}"} if secret else {}
    request = Request(f"{controller.rstrip('/')}{path}", headers=headers)
    try:
        with urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            content = response.read().decode("utf-8").strip()
            return json.loads(content) if content else {}
    except (HTTPError, URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"API 请求失败: {path}: {exc}") from exc


def connection_host(item: dict) -> str:
    metadata = item.get("metadata") or {}
    return str(
        metadata.get("host")
        or metadata.get("sniffHost")
        or metadata.get("remoteDestination")
        or "-"
    ).lower()


def connection_route(item: dict) -> str:
    chains = item.get("chains") or []
    return " -> ".join(str(part) for part in chains[:3]) if chains else "-"


def render_connections(controller: str, secret: str, raw: bool) -> int:
    payload = request_json(controller, secret, "/connections")
    connections = payload.get("connections") if isinstance(payload, dict) else []
    if not isinstance(connections, list):
        raise RuntimeError("API 返回缺少 connections 字段")

    counts: Counter[str] = Counter()
    routes: defaultdict[str, Counter[str]] = defaultdict(Counter)
    for item in connections:
        if not isinstance(item, dict):
            continue
        host = connection_host(item)
        if not any(keyword in host for keyword in AI_HOST_KEYWORDS):
            continue
        route = connection_route(item)
        counts[host] += 1
        routes[host][route] += 1

    if raw:
        for host, count in counts.most_common():
            route = routes[host].most_common(1)[0][0]
            print(f"AI_CONNECTION\t{host}\tcount={count}\troute={route}")
        return 0

    print("AI 连接")
    if not counts:
        print("未发现 ChatGPT/OpenAI/Claude/GitHub 相关活动连接")
        return 0

    for host, count in counts.most_common(20):
        route = routes[host].most_common(1)[0][0]
        print(f"{host}  {count} active  {route}")
    return 0


def load_history(path: str, limit: int) -> list[dict]:
    if limit < 1:
        raise RuntimeError("--limit 必须大于等于 1")

    rows: list[dict] = []
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(item, dict):
                    rows.append(item)
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise RuntimeError(f"读取历史失败: {exc}") from exc
    return rows[-limit:]


def render_history(path: str, limit: int, raw: bool) -> int:
    rows = load_history(path, limit)
    if raw:
        for item in rows:
            print(
                "PROBE_HISTORY\t"
                f"ts={item.get('ts', '-')}\tprofile={item.get('profile', '-')}"
                f"\tstrategy={item.get('strategy', '-')}\tcurrent={item.get('current', '-')}"
                f"\tbest={item.get('best', '-')}\tstable={item.get('stable', '-')}"
                f"\tswitched={item.get('switched', '-')}\treason={item.get('skip_reason') or item.get('reason', '-')}"
            )
        return 0

    print("稳定探测历史")
    if not rows:
        print("-")
        return 0
    for item in rows:
        print(
            f"{item.get('ts', '-')}  "
            f"{item.get('profile', '-')}  "
            f"{item.get('strategy', '-')}  "
            f"current={item.get('current', '-')}  "
            f"best={item.get('best', '-')}  "
            f"stable={item.get('stable', '-')}  "
            f"switched={item.get('switched', '-')}  "
            f"reason={item.get('skip_reason') or item.get('reason', '-')}"
        )
    return 0


def main() -> int:
    args = parse_args()
    try:
        if args.command == "connections":
            return render_connections(args.controller, args.secret, args.raw)
        if args.command == "history":
            return render_history(args.history_file, args.limit, args.raw)
    except RuntimeError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 1
    return 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\n已取消", file=sys.stderr)
        raise SystemExit(130)
