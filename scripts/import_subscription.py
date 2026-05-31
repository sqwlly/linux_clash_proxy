#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import yaml


DEFAULT_MAX_BYTES = 4 * 1024 * 1024
DEFAULT_TIMEOUT_SECONDS = 20
DEFAULT_USER_AGENT = "clash-proxy-subscription-import/0.1"


@dataclass(frozen=True)
class SubscriptionContent:
    text: str
    status: int
    content_type: str
    byte_count: int


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Download and validate a Clash/Mihomo subscription config.")
    parser.add_argument("url")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", default=True, help="Validate only. This is the default.")
    mode.add_argument("--apply", action="store_true", help="Apply the downloaded config through update_config.sh.")
    parser.add_argument("--update-script", default="")
    parser.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES)
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_SECONDS)
    return parser.parse_args()


def download_subscription(url: str, max_bytes: int, timeout: int) -> SubscriptionContent:
    if max_bytes < 1024:
        raise RuntimeError("--max-bytes must be at least 1024")
    if timeout < 1:
        raise RuntimeError("--timeout must be at least 1")

    request = Request(url, headers={"User-Agent": DEFAULT_USER_AGENT})
    try:
        with urlopen(request, timeout=timeout) as response:
            body = response.read(max_bytes + 1)
            status = int(getattr(response, "status", 200))
            content_type = response.headers.get("Content-Type", "")
    except (HTTPError, URLError, TimeoutError, OSError) as exc:
        raise RuntimeError(f"download failed: {exc}") from exc

    if len(body) > max_bytes:
        raise RuntimeError(f"subscription exceeds safety limit: {max_bytes} bytes")
    if not body:
        raise RuntimeError("subscription response is empty")

    text = body.decode("utf-8-sig", errors="replace").strip()
    if not text:
        raise RuntimeError("subscription response is blank")
    return SubscriptionContent(text=text, status=status, content_type=content_type, byte_count=len(body))


def validate_full_yaml_config(text: str) -> dict:
    try:
        data = yaml.safe_load(text)
    except Exception as exc:
        raise RuntimeError(f"YAML parse failed: {exc}") from exc

    if not isinstance(data, dict):
        raise RuntimeError("top-level YAML must be a mapping")

    for key in ("proxies", "proxy-groups", "rules"):
        if key not in data:
            raise RuntimeError(f"missing required field: {key}")
        if not isinstance(data[key], list):
            raise RuntimeError(f"{key} must be a list")

    if not data["proxies"]:
        raise RuntimeError("proxies must not be empty")
    return data


def write_temp_config(text: str) -> str:
    handle = tempfile.NamedTemporaryFile("w", encoding="utf-8", prefix="clash-subscription-", suffix=".yaml", delete=False)
    try:
        handle.write(text)
        handle.write("\n")
        return handle.name
    finally:
        handle.close()


def run_update_script(update_script: str, mode: str, candidate_file: str) -> int:
    command = [update_script, mode, candidate_file]
    return subprocess.run(command, check=False).returncode


def main() -> int:
    args = parse_args()
    mode = "--apply" if args.apply else "--dry-run"
    update_script = args.update_script or os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "update_config.sh")
    if not os.path.isfile(update_script):
        print(f"错误: update_config.sh 不存在: {update_script}", file=sys.stderr)
        return 1

    try:
        content = download_subscription(args.url, args.max_bytes, args.timeout)
        data = validate_full_yaml_config(content.text)
        candidate_file = write_temp_config(content.text)
    except RuntimeError as exc:
        print(f"错误: 订阅导入校验失败: {exc}", file=sys.stderr)
        return 1

    try:
        print(
            "订阅下载完成: "
            f"HTTP {content.status}, {content.byte_count} bytes, "
            f"proxies={len(data['proxies'])}, groups={len(data['proxy-groups'])}, rules={len(data['rules'])}",
            flush=True,
        )
        return run_update_script(update_script, mode, candidate_file)
    finally:
        try:
            os.unlink(candidate_file)
        except OSError:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
