from __future__ import annotations

import os
import subprocess

from .config import AppPaths, read_config

DEFAULT_NO_PROXY = "127.0.0.1,localhost"


def _strip_remainder_separator(argv: list[str]) -> list[str]:
    if argv[:1] == ["--"]:
        return argv[1:]
    return argv


def proxy_port(paths: AppPaths) -> int:
    config = read_config(paths)
    return int(config.get("mixed-port", 7890))


def proxy_http_url(paths: AppPaths) -> str:
    return f"http://127.0.0.1:{proxy_port(paths)}"


def proxy_all_url(paths: AppPaths) -> str:
    return f"socks5h://127.0.0.1:{proxy_port(paths)}"


def proxy_no_proxy() -> str:
    return os.environ.get("PROXY_NO_PROXY", DEFAULT_NO_PROXY)


def proxy_env_map(paths: AppPaths) -> dict[str, str]:
    http_url = proxy_http_url(paths)
    all_url = proxy_all_url(paths)
    no_proxy = proxy_no_proxy()
    return {
        "HTTP_PROXY": http_url,
        "HTTPS_PROXY": http_url,
        "ALL_PROXY": all_url,
        "http_proxy": http_url,
        "https_proxy": http_url,
        "all_proxy": all_url,
        "NO_PROXY": no_proxy,
        "no_proxy": no_proxy,
    }


def proxy_env_lines(paths: AppPaths) -> list[str]:
    env_map = proxy_env_map(paths)
    return [
        f"HTTP_PROXY={env_map['HTTP_PROXY']}",
        f"HTTPS_PROXY={env_map['HTTPS_PROXY']}",
        f"ALL_PROXY={env_map['ALL_PROXY']}",
        f"NO_PROXY={env_map['NO_PROXY']}",
    ]


def run_with_proxy(paths: AppPaths, command: list[str]) -> int:
    command = _strip_remainder_separator(command)
    if not command:
        raise SystemExit("错误: 用法: cproxy with-proxy <command> [args...]")

    env = os.environ.copy()
    env.update(proxy_env_map(paths))
    result = subprocess.run(command, env=env, check=False)
    return result.returncode


def run_proxy_shell(paths: AppPaths, shell_args: list[str]) -> int:
    shell_args = _strip_remainder_separator(shell_args)
    shell_bin = os.environ.get("SHELL", "/bin/bash")
    env = os.environ.copy()
    env.update(proxy_env_map(paths))
    argv = [shell_bin, *(shell_args or ["-l"])]
    result = subprocess.run(argv, env=env, check=False)
    return result.returncode
