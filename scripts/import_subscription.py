#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import copy
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, unquote, urlsplit
from urllib.request import Request, urlopen

import yaml


DEFAULT_MAX_BYTES = 4 * 1024 * 1024
DEFAULT_TIMEOUT_SECONDS = 20
DEFAULT_USER_AGENT = "clash-proxy-subscription-import/0.1"
TEST_URL = "https://cp.cloudflare.com/generate_204"
INFO_NAME_PATTERNS = (
    "剩余流量",
    "距离下次",
    "套餐到期",
    "Expire",
    "Traffic",
    "Reset",
)


@dataclass(frozen=True)
class SubscriptionContent:
    text: str
    status: int
    content_type: str
    byte_count: int


@dataclass(frozen=True)
class CandidateConfig:
    text: str
    data: dict
    source: str


@dataclass(frozen=True)
class CandidateDownload:
    content: SubscriptionContent | None
    error: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Download and validate a Clash/Mihomo subscription config.")
    parser.add_argument("url")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", default=True, help="Validate only. This is the default.")
    mode.add_argument("--apply", action="store_true", help="Apply the downloaded config through update_config.sh.")
    parser.add_argument("--group", default="", help="Group name for generated VLESS subscription configs.")
    parser.add_argument("--attach-to", default="", help="Add the imported group to an existing selector without switching.")
    parser.add_argument("--config-file", default="")
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


def download_subscription_with_curl(url: str, max_bytes: int, timeout: int) -> SubscriptionContent:
    command = [
        "curl",
        "-fsSL",
        "--connect-timeout",
        str(min(timeout, 10)),
        "--max-time",
        str(timeout),
        url,
    ]
    try:
        result = subprocess.run(command, check=False, capture_output=True)
    except OSError as exc:
        raise RuntimeError(f"curl fallback unavailable: {exc}") from exc
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"curl fallback failed: {detail or result.returncode}")
    body = result.stdout
    if len(body) > max_bytes:
        raise RuntimeError(f"subscription exceeds safety limit: {max_bytes} bytes")
    if not body:
        raise RuntimeError("subscription response is empty")
    text = body.decode("utf-8-sig", errors="replace").strip()
    if not text:
        raise RuntimeError("subscription response is blank")
    return SubscriptionContent(text=text, status=200, content_type="", byte_count=len(body))


def download_candidates(url: str, max_bytes: int, timeout: int) -> list[CandidateDownload]:
    downloads: list[CandidateDownload] = []
    first_text = ""
    try:
        content = download_subscription(url, max_bytes, timeout)
        first_text = content.text
        downloads.append(CandidateDownload(content=content, error=""))
    except RuntimeError as exc:
        downloads.append(CandidateDownload(content=None, error=str(exc)))

    try:
        content = download_subscription_with_curl(url, max_bytes, timeout)
        if content.text != first_text:
            downloads.append(CandidateDownload(content=content, error=""))
    except RuntimeError as exc:
        downloads.append(CandidateDownload(content=None, error=str(exc)))
    return downloads


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


def decode_base64_subscription(text: str) -> str:
    compact = "".join(text.split())
    if not compact:
        raise RuntimeError("subscription response is blank")
    if not re.fullmatch(r"[A-Za-z0-9+/=_-]+", compact):
        raise RuntimeError("subscription is not full YAML or Base64 node list")
    padded = compact.replace("-", "+").replace("_", "/")
    padded += "=" * (-len(padded) % 4)
    try:
        return base64.b64decode(padded, validate=False).decode("utf-8-sig", errors="replace")
    except Exception as exc:
        raise RuntimeError(f"Base64 decode failed: {exc}") from exc


def first_query_value(query: dict[str, list[str]], key: str) -> str:
    values = query.get(key) or []
    return values[0] if values else ""


def is_info_name(name: str) -> bool:
    return any(pattern in name for pattern in INFO_NAME_PATTERNS)


def unique_name(name: str, used_names: set[str]) -> str:
    candidate = name or "Node"
    if candidate not in used_names:
        used_names.add(candidate)
        return candidate
    index = 2
    while f"{candidate} #{index}" in used_names:
        index += 1
    final_name = f"{candidate} #{index}"
    used_names.add(final_name)
    return final_name


def parse_vless_uri(uri: str, used_names: set[str]) -> dict | None:
    parsed = urlsplit(uri)
    if parsed.scheme != "vless":
        raise RuntimeError(f"unsupported node URI scheme: {parsed.scheme}")
    if not parsed.username or not parsed.hostname or parsed.port is None:
        raise RuntimeError("invalid vless URI: missing uuid, server, or port")

    query = parse_qs(parsed.query, keep_blank_values=True)
    name = unquote(parsed.fragment or "").strip()
    if is_info_name(name):
        return None

    proxy = {
        "name": unique_name(name, used_names),
        "type": "vless",
        "server": parsed.hostname,
        "port": int(parsed.port),
        "uuid": unquote(parsed.username),
        "udp": True,
    }

    network = first_query_value(query, "type")
    if network:
        proxy["network"] = network

    security = first_query_value(query, "security")
    if security in {"tls", "reality"}:
        proxy["tls"] = True
        servername = first_query_value(query, "sni")
        if servername:
            proxy["servername"] = servername
        fingerprint = first_query_value(query, "fp")
        if fingerprint:
            proxy["client-fingerprint"] = fingerprint

    flow = first_query_value(query, "flow")
    if flow:
        proxy["flow"] = flow

    if network == "ws":
        ws_opts: dict[str, object] = {}
        path = first_query_value(query, "path")
        host = first_query_value(query, "host")
        if path:
            ws_opts["path"] = path
        if host:
            ws_opts["headers"] = {"Host": host}
        if ws_opts:
            proxy["ws-opts"] = ws_opts

    if security == "reality":
        reality_opts = {}
        public_key = first_query_value(query, "pbk")
        short_id = first_query_value(query, "sid")
        if public_key:
            reality_opts["public-key"] = public_key
        if short_id:
            reality_opts["short-id"] = short_id
        if reality_opts:
            proxy["reality-opts"] = reality_opts

    return proxy


def parse_node_uri_list(text: str) -> list[dict]:
    proxies: list[dict] = []
    used_names: set[str] = set()
    for line in text.splitlines():
        uri = line.strip()
        if not uri:
            continue
        parsed = urlsplit(uri)
        if parsed.scheme == "vless":
            proxy = parse_vless_uri(uri, used_names)
            if proxy is not None:
                proxies.append(proxy)
            continue
        raise RuntimeError(f"unsupported node URI scheme: {parsed.scheme or '-'}")
    if not proxies:
        raise RuntimeError("node URI subscription contains no importable proxies")
    return proxies


def converted_config_from_proxies(proxies: list[dict], group_name: str = "") -> dict:
    names = [str(proxy["name"]) for proxy in proxies]
    main_group = group_name.strip() or "PROXY"
    auto_group = f"{main_group}-Auto" if group_name.strip() else "Auto"
    return {
        "mixed-port": 7890,
        "allow-lan": True,
        "bind-address": "*",
        "mode": "rule",
        "log-level": "info",
        "ipv6": False,
        "external-controller": "127.0.0.1:9090",
        "secret": "",
        "profile": {"store-selected": True},
        "unified-delay": True,
        "proxies": proxies,
        "proxy-groups": [
            {"name": main_group, "type": "select", "proxies": [auto_group, *names, "DIRECT"]},
            {"name": auto_group, "type": "fallback", "proxies": names, "url": TEST_URL, "interval": 300},
        ],
        "rules": [f"MATCH,{main_group}"],
    }


def load_yaml_config(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
    except Exception as exc:
        raise RuntimeError(f"failed to read current config: {exc}") from exc
    if not isinstance(data, dict):
        raise RuntimeError("current config top-level YAML must be a mapping")
    for key in ("proxies", "proxy-groups", "rules"):
        if not isinstance(data.get(key), list):
            raise RuntimeError(f"current config {key} must be a list")
    return data


def subscription_group_config(proxies: list[dict], group_name: str) -> dict:
    name = group_name.strip()
    if not name:
        raise RuntimeError("--group is required with --attach-to")

    used_names: set[str] = set()
    renamed = []
    for proxy in proxies:
        item = copy.deepcopy(proxy)
        item["name"] = unique_name(f"{name}/{proxy.get('name') or 'Node'}", used_names)
        renamed.append(item)

    node_names = [str(proxy["name"]) for proxy in renamed]
    return {
        "proxies": renamed,
        "proxy-groups": [
            {"name": name, "type": "select", "proxies": [f"{name}-Auto", *node_names, "DIRECT"]},
            {"name": f"{name}-Auto", "type": "fallback", "proxies": node_names, "url": TEST_URL, "interval": 300},
        ],
    }


def attach_subscription_group(current: dict, group_config: dict, group_name: str, attach_to: str) -> dict:
    attach_group_name = attach_to.strip()
    if not attach_group_name:
        raise RuntimeError("--attach-to must not be blank")

    managed_group_names = {str(group["name"]) for group in group_config["proxy-groups"]}
    if attach_group_name in managed_group_names:
        raise RuntimeError("--attach-to must reference an existing parent group, not the imported group")

    proxy_prefix = f"{group_name}/"
    merged = copy.deepcopy(current)
    attach_group = None

    for group in merged["proxy-groups"]:
        if not isinstance(group, dict):
            continue
        if str(group.get("name")) == attach_group_name:
            attach_group = group
            break
    if attach_group is None:
        raise RuntimeError(f"attach target group not found: {attach_group_name}")
    if not isinstance(attach_group.get("proxies"), list):
        raise RuntimeError(f"attach target group proxies must be a list: {attach_group_name}")

    merged["proxy-groups"] = [
        group
        for group in merged["proxy-groups"]
        if not (isinstance(group, dict) and str(group.get("name")) in managed_group_names)
    ]
    merged["proxies"] = [
        proxy
        for proxy in merged["proxies"]
        if not (isinstance(proxy, dict) and str(proxy.get("name", "")).startswith(proxy_prefix))
    ]

    existing_proxy_names = {str(proxy.get("name")) for proxy in merged["proxies"] if isinstance(proxy, dict)}
    new_proxy_names = {str(proxy["name"]) for proxy in group_config["proxies"]}
    collisions = sorted(existing_proxy_names & new_proxy_names)
    if collisions:
        raise RuntimeError(f"proxy name collision: {', '.join(collisions)}")

    attach_candidates = [str(item) for item in attach_group["proxies"]]
    if group_name not in attach_candidates:
        attach_group["proxies"].append(group_name)

    merged["proxies"].extend(group_config["proxies"])
    merged["proxy-groups"].extend(group_config["proxy-groups"])
    return merged


def candidate_config_from_subscription(text: str, group_name: str = "") -> CandidateConfig:
    try:
        data = validate_full_yaml_config(text)
        return CandidateConfig(text=text, data=data, source="yaml")
    except RuntimeError as yaml_error:
        decoded = decode_base64_subscription(text)
        proxies = parse_node_uri_list(decoded)
        data = converted_config_from_proxies(proxies, group_name)
        candidate_text = yaml.safe_dump(data, allow_unicode=True, sort_keys=False)
        try:
            validate_full_yaml_config(candidate_text)
        except RuntimeError as exc:
            raise RuntimeError(f"converted config is invalid: {exc}") from exc
        return CandidateConfig(text=candidate_text, data=data, source=f"base64-uri-list (YAML fallback: {yaml_error})")


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
    project_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    mode = "--apply" if args.apply else "--dry-run"
    update_script = args.update_script or os.path.join(project_dir, "update_config.sh")
    config_file = args.config_file or os.path.join(project_dir, "config.yaml")
    if not os.path.isfile(update_script):
        print(f"错误: update_config.sh 不存在: {update_script}", file=sys.stderr)
        return 1

    errors = []
    content = None
    candidate = None
    for download in download_candidates(args.url, args.max_bytes, args.timeout):
        if download.content is None:
            errors.append(download.error)
            continue
        try:
            candidate = candidate_config_from_subscription(download.content.text, args.group)
            content = download.content
            break
        except RuntimeError as exc:
            errors.append(str(exc))
    if content is None or candidate is None:
        print(f"错误: 订阅导入校验失败: {'; '.join(error for error in errors if error)}", file=sys.stderr)
        return 1

    attach_to = args.attach_to.strip()
    try:
        if attach_to:
            current = load_yaml_config(config_file)
            group_config = subscription_group_config(candidate.data["proxies"], args.group)
            attached = attach_subscription_group(current, group_config, args.group.strip(), attach_to)
            candidate_text = yaml.safe_dump(attached, allow_unicode=True, sort_keys=False)
            validate_full_yaml_config(candidate_text)
            candidate_file = write_temp_config(candidate_text)
        else:
            candidate_file = write_temp_config(candidate.text)
    except RuntimeError as exc:
        print(f"错误: 订阅导入校验失败: {exc}", file=sys.stderr)
        return 1

    try:
        summary = (
            f"HTTP {content.status}, {content.byte_count} bytes, "
            f"source={candidate.source}, proxies={len(candidate.data['proxies'])}"
        )
        if attach_to:
            print(f"订阅挂载完成: {summary}, group={args.group.strip()}, attach_to={attach_to}", flush=True)
            return run_update_script(update_script, mode, candidate_file)
        print(
            "订阅下载完成: "
            f"{summary}, groups={len(candidate.data['proxy-groups'])}, rules={len(candidate.data['rules'])}",
            flush=True,
        )
        return run_update_script(update_script, mode, candidate_file)
    finally:
        try:
            os.unlink(candidate_file)
        except OSError:
            pass


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\n已取消", file=sys.stderr)
        raise SystemExit(130)
