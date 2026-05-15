#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen


DEFAULT_GROUP = "AI-SG"
DEFAULT_URL = "https://chatgpt.com/backend-api/codex/responses/compact"
DEFAULT_ROUNDS = 3
DEFAULT_TIMEOUT_MS = 8000
REQUEST_TIMEOUT_SECONDS = 10
MIN_SWITCH_ROUNDS = 3
MAX_STABLE_DELAY_MS = 3000
MAX_AVG_DELAY_MS = 1500


@dataclass(frozen=True)
class GroupState:
    current: str | None
    candidates: list[str]


@dataclass(frozen=True)
class ProbeSummary:
    name: str
    delays: list[int]
    failures: int

    @property
    def success_count(self) -> int:
        return len(self.delays)

    @property
    def total_count(self) -> int:
        return self.success_count + self.failures

    @property
    def avg_delay(self) -> int | None:
        if not self.delays:
            return None
        return round(sum(self.delays) / len(self.delays))

    @property
    def max_delay(self) -> int | None:
        if not self.delays:
            return None
        return max(self.delays)

    @property
    def min_delay(self) -> int | None:
        if not self.delays:
            return None
        return min(self.delays)

    def rank_key(self) -> tuple[int, int, int, int, str]:
        no_success_penalty = 1_000_000
        max_delay = self.max_delay if self.max_delay is not None else no_success_penalty
        avg_delay = self.avg_delay if self.avg_delay is not None else no_success_penalty
        return (-self.success_count, self.failures, max_delay, avg_delay, self.name)


@dataclass(frozen=True)
class StabilityVerdict:
    stable: bool
    reason: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Probe a Mihomo group and rank stable nodes.")
    parser.add_argument("--controller", required=True)
    parser.add_argument("--secret", default="")
    parser.add_argument("--group", default=DEFAULT_GROUP)
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--rounds", type=int, default=DEFAULT_ROUNDS)
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_MS)
    parser.add_argument("--switch", action="store_true", help="Switch the probed group to the best node.")
    parser.add_argument("--raw", action="store_true")
    return parser.parse_args()


def request_json(controller: str, secret: str, path: str, method: str = "GET", payload: dict | None = None) -> dict:
    url = f"{controller.rstrip('/')}{path}"
    headers = {"Authorization": f"Bearer {secret}"} if secret else {}
    body = None
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = Request(url, data=body, method=method, headers=headers)
    try:
        with urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            content = response.read().decode("utf-8").strip()
            return json.loads(content) if content else {}
    except (HTTPError, URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"API 请求失败: {path}: {exc}") from exc


def load_group_state(controller: str, secret: str, group_name: str) -> GroupState:
    payload = request_json(controller, secret, "/proxies")
    proxies = payload.get("proxies") if isinstance(payload, dict) else None
    if not isinstance(proxies, dict):
        raise RuntimeError("API 返回缺少 proxies 字段")

    group = proxies.get(group_name)
    if not isinstance(group, dict):
        raise RuntimeError(f"未找到代理组或节点: {group_name}")

    current = group.get("now")
    candidates = group.get("all") or group.get("proxies") or []
    if not candidates:
        candidates = [group_name]
    return GroupState(
        current=str(current) if current else None,
        candidates=[str(item) for item in candidates],
    )


def probe_delay(controller: str, secret: str, node: str, url: str, timeout: int) -> int | None:
    query = urlencode({"url": url, "timeout": timeout})
    path = f"/proxies/{quote(node, safe='')}/delay?{query}"
    try:
        payload = request_json(controller, secret, path)
    except RuntimeError:
        return None

    delay = payload.get("delay") if isinstance(payload, dict) else None
    try:
        return int(delay)
    except (TypeError, ValueError):
        return None


def switch_group(controller: str, secret: str, group: str, target: str) -> None:
    request_json(controller, secret, f"/proxies/{quote(group, safe='')}", method="PUT", payload={"name": target})


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


def format_delay(value: int | None) -> str:
    return f"{value}ms" if value is not None else "-"


def stability_verdict(summary: ProbeSummary | None, rounds: int) -> StabilityVerdict:
    if summary is None:
        return StabilityVerdict(False, "没有成功节点")
    if rounds < MIN_SWITCH_ROUNDS:
        return StabilityVerdict(False, f"切换要求至少 {MIN_SWITCH_ROUNDS} 轮探测，当前 {rounds} 轮")
    if summary.success_count != rounds:
        return StabilityVerdict(False, f"成功 {summary.success_count}/{rounds}，要求全成功")
    if summary.failures:
        return StabilityVerdict(False, f"失败 {summary.failures} 次，要求 0 失败")
    if summary.max_delay is None or summary.avg_delay is None:
        return StabilityVerdict(False, "缺少有效延迟数据")
    if summary.max_delay > MAX_STABLE_DELAY_MS:
        return StabilityVerdict(False, f"最大延迟 {summary.max_delay}ms 超过 {MAX_STABLE_DELAY_MS}ms")
    if summary.avg_delay > MAX_AVG_DELAY_MS:
        return StabilityVerdict(False, f"平均延迟 {summary.avg_delay}ms 超过 {MAX_AVG_DELAY_MS}ms")
    return StabilityVerdict(True, "满足稳定门槛")


def summarize(
    controller: str,
    secret: str,
    group: str,
    url: str,
    rounds: int,
    timeout: int,
) -> tuple[list[ProbeSummary], str | None]:
    if rounds < 1:
        raise RuntimeError("--rounds 必须大于等于 1")
    if timeout < 1000:
        raise RuntimeError("--timeout 必须大于等于 1000")

    group_state = load_group_state(controller, secret, group)
    results = {name: {"delays": [], "failures": 0} for name in group_state.candidates}

    for _ in range(rounds):
        for name in group_state.candidates:
            delay = probe_delay(controller, secret, name, url, timeout)
            if delay is None:
                results[name]["failures"] += 1
            else:
                results[name]["delays"].append(delay)

    summaries = [
        ProbeSummary(name=name, delays=list(item["delays"]), failures=int(item["failures"]))
        for name, item in results.items()
    ]
    return summaries, group_state.current


def render_raw(
    group: str,
    url: str,
    current: str | None,
    best: ProbeSummary | None,
    verdict: StabilityVerdict,
    summaries: list[ProbeSummary],
    switch_requested: bool,
    switched: bool,
    skip_reason: str,
) -> None:
    print(f"GROUP\t{group}")
    print(f"URL\t{url}")
    print(f"CURRENT\t{current or '-'}")
    print(f"STABLE\t{'true' if verdict.stable else 'false'}\t{verdict.reason}")
    if best:
        print(
            "BEST\t"
            f"{best.name}\tsuccess={best.success_count}/{best.total_count}"
            f"\tfailures={best.failures}\tavg={format_delay(best.avg_delay)}\tmax={format_delay(best.max_delay)}"
        )
        if switched:
            print(f"SWITCH\t{group}\t{best.name}")
    if switch_requested and not switched:
        print(f"SKIP_SWITCH\t{group}\t{skip_reason}")
    for item in sorted(summaries, key=lambda value: value.rank_key()):
        print(
            "NODE\t"
            f"{item.name}\tsuccess={item.success_count}/{item.total_count}"
            f"\tfailures={item.failures}\tavg={format_delay(item.avg_delay)}"
            f"\tmax={format_delay(item.max_delay)}\tmin={format_delay(item.min_delay)}"
        )


def render_human(
    group: str,
    url: str,
    current: str | None,
    best: ProbeSummary | None,
    verdict: StabilityVerdict,
    summaries: list[ProbeSummary],
    switch_requested: bool,
    switched: bool,
    skip_reason: str,
) -> None:
    print("摘要")
    print(f"目标组: {group}")
    print(f"目标 URL: {url}")
    print(f"当前: {normalize_name(current)}")
    if best:
        print(
            f"推荐: {normalize_name(best.name)} "
            f"(成功 {best.success_count}/{best.total_count}, "
            f"失败 {best.failures}, 平均 {format_delay(best.avg_delay)}, 最大 {format_delay(best.max_delay)})"
        )
    else:
        print("推荐: -")
    print(f"稳定: {'是' if verdict.stable else '否'} ({verdict.reason})")
    if best and switched:
        print(f"切换: 已切换 {group} -> {normalize_name(best.name)}")
    elif switch_requested:
        print(f"切换: 未切换 ({skip_reason})")
    print()
    print("结果")
    for item in sorted(summaries, key=lambda value: value.rank_key()):
        print(
            f"{normalize_name(item.name)}  "
            f"成功 {item.success_count}/{item.total_count}  "
            f"失败 {item.failures}  "
            f"平均 {format_delay(item.avg_delay)}  "
            f"最大 {format_delay(item.max_delay)}  "
            f"最小 {format_delay(item.min_delay)}"
        )


def main() -> int:
    args = parse_args()
    try:
        summaries, current = summarize(args.controller, args.secret, args.group, args.url, args.rounds, args.timeout)
    except RuntimeError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 1

    successful = [item for item in summaries if item.success_count > 0]
    best = min(successful, key=lambda value: value.rank_key()) if successful else None
    verdict = stability_verdict(best, args.rounds)
    switched = False
    skip_reason = ""

    if args.switch:
        if not verdict.stable:
            skip_reason = verdict.reason
        elif best is None:
            skip_reason = "没有可切换的成功节点"
        elif current == best.name:
            skip_reason = "当前已是推荐稳定节点"
        else:
            try:
                switch_group(args.controller, args.secret, args.group, best.name)
            except RuntimeError as exc:
                print(f"错误: 切换失败: {exc}", file=sys.stderr)
                return 1
            switched = True

    if args.raw:
        render_raw(args.group, args.url, current, best, verdict, summaries, args.switch, switched, skip_reason)
    else:
        render_human(args.group, args.url, current, best, verdict, summaries, args.switch, switched, skip_reason)

    if args.switch and not (switched or skip_reason == "当前已是推荐稳定节点"):
        return 1
    return 0 if best else 1


if __name__ == "__main__":
    raise SystemExit(main())
