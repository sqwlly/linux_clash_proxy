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


def main() -> int:
    args = parse_args()
    try:
        if args.command == "connections":
            return render_connections(args.controller, args.secret, args.raw)
    except RuntimeError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 1
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
