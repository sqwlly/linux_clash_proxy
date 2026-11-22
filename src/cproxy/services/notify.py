"""`cproxy notify` —— 主动告警与通知钩子（Webhook / 桌面通知）。

支持将「AI 出口故障切换」「订阅刷新失败回滚」「套餐流量/到期预警」推送至
飞书、Telegram Bot、Bark、Discord、Slack、通用 JSON Webhook 或本地 `notify-send`。
配置可存放于 `state/notify.json`、`config.yaml` (`notify-webhook` / `notify-desktop`)
或环境变量 (`CPROXY_NOTIFY_WEBHOOK` / `CPROXY_NOTIFY_DESKTOP`)。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlparse
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from ..config import AppPaths, read_config
from ..redaction import redact_text


def notify_state_file(paths: AppPaths) -> Path:
    return paths.state_dir / "notify.json"


def _atomic_write_private(path: Path, content: str, *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    tmp_path = Path(tmp_name)
    try:
        if os.name != "nt":
            os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fd = -1
            fh.write(content)
        if os.name != "nt":
            os.chmod(tmp_path, mode)
        os.replace(tmp_path, path)
    except BaseException:
        if fd >= 0:
            os.close(fd)
        tmp_path.unlink(missing_ok=True)
        raise


def _validate_webhook_url(url: str) -> str:
    cleaned = str(url or "").strip()
    parsed = urlparse(cleaned)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise ValueError("错误: Webhook URL 必须使用 http/https 协议且包含合法主机名")
    return cleaned


class _NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(
        self,
        req: Any,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> Any:
        raise HTTPError(req.full_url, code, f"Webhook redirects are disabled (HTTP {code})", headers, fp)

    def http_error_302(
        self,
        req: Any,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
    ) -> Any:
        raise HTTPError(req.full_url, code, f"Webhook redirects are disabled (HTTP {code})", headers, fp)

    http_error_301 = http_error_303 = http_error_307 = http_error_308 = http_error_302


def _format_redacted_netloc(parsed: Any) -> str:
    host = parsed.hostname or ""
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    try:
        port = parsed.port
    except ValueError:
        port = None
    hostport = f"{host}:{port}" if port is not None else host
    if parsed.username is not None or parsed.password is not None or "@" in (parsed.netloc or ""):
        return f"[REDACTED]@{hostport}"
    return hostport or str(parsed.netloc or "")


def _mask_webhook_url(url: str) -> str:
    raw = str(url or "").strip()
    if not raw:
        return ""
    parsed = urlparse(raw)
    if not parsed.scheme or not parsed.netloc:
        return redact_text(raw)

    netloc = _format_redacted_netloc(parsed)
    host_lower = (parsed.hostname or "").lower()
    segments = [seg for seg in parsed.path.split("/") if seg]
    masked_telegram = False

    for idx, seg in enumerate(segments):
        seg_lower = seg.lower()
        if seg_lower.startswith("bot") and len(seg) > 3 and (
            "telegram" in host_lower or ":" in seg or seg[3:4].isdigit()
        ):
            segments[idx] = "bot[REDACTED]"
            masked_telegram = True

    if len(segments) == 1:
        if not masked_telegram:
            segments[0] = "[REDACTED]"
    elif len(segments) >= 2 and not masked_telegram:
        lower_segs = [s.lower() for s in segments]
        if "services" in lower_segs and "slack" in host_lower:
            pos = lower_segs.index("services")
            segments = segments[: pos + 1] + ["[REDACTED]"]
        elif "webhooks" in lower_segs:
            pos = lower_segs.index("webhooks")
            segments = segments[: pos + 1] + ["[REDACTED]"]
        elif len(segments[-1]) > 0:
            segments[-1] = "[REDACTED]"

    masked_path = ("/" + "/".join(segments)) if segments else ""
    query_suffix = "?..." if parsed.query else ""
    return redact_text(f"{parsed.scheme}://{netloc}{masked_path}{query_suffix}")


@dataclass(frozen=True)
class NotifyConfig:
    webhook_url: str = ""
    desktop: bool = False
    source: str = "none"

    @property
    def enabled(self) -> bool:
        return bool(self.webhook_url or self.desktop)

    @property
    def channel_kind(self) -> str:
        return detect_webhook_kind(self.webhook_url) if self.webhook_url else ("desktop" if self.desktop else "none")

    def to_dict(self, *, redact: bool = True) -> dict[str, Any]:
        url = _mask_webhook_url(self.webhook_url) if (redact and self.webhook_url) else self.webhook_url
        return {
            "enabled": self.enabled,
            "channel_kind": self.channel_kind,
            "webhook_url": url,
            "desktop": self.desktop,
            "source": self.source,
        }


def detect_webhook_kind(url: str) -> str:
    lower = str(url or "").strip().lower()
    if not lower:
        return "none"
    if "open.feishu.cn" in lower or "open.larksuite.com" in lower:
        return "feishu"
    if "api.telegram.org/bot" in lower:
        return "telegram"
    if "discord.com/api/webhooks" in lower or "discordapp.com/api/webhooks" in lower:
        return "discord"
    if "hooks.slack.com" in lower:
        return "slack"
    if "api.day.app" in lower or "/bark/" in lower:
        return "bark"
    return "webhook"


def load_notify_config(paths: AppPaths) -> NotifyConfig:
    """按 环境变量 → state/notify.json → config.yaml 优先级读取通知配置。"""
    env_url = os.environ.get("CPROXY_NOTIFY_WEBHOOK", "").strip()
    env_desktop = os.environ.get("CPROXY_NOTIFY_DESKTOP", "").strip().lower() in {"1", "true", "yes", "on"}
    if env_url or env_desktop:
        return NotifyConfig(webhook_url=env_url, desktop=env_desktop, source="env")

    state_path = notify_state_file(paths)
    try:
        raw = json.loads(state_path.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            url = str(raw.get("webhook_url") or "").strip()
            desktop = bool(raw.get("desktop", False))
            if url or desktop:
                return NotifyConfig(webhook_url=url, desktop=desktop, source="state")
    except (OSError, ValueError):
        pass

    try:
        cfg = read_config(paths)
        url = str(cfg.get("notify-webhook") or "").strip()
        desktop = bool(cfg.get("notify-desktop", False))
        if url or desktop:
            return NotifyConfig(webhook_url=url, desktop=desktop, source="config")
    except Exception:
        pass

    return NotifyConfig()


def save_notify_config(
    paths: AppPaths,
    *,
    webhook_url: str | None = None,
    desktop: bool | None = None,
) -> NotifyConfig:
    current = load_notify_config(paths)
    new_url = current.webhook_url if webhook_url is None else webhook_url.strip()
    if webhook_url is not None and new_url:
        new_url = _validate_webhook_url(new_url)
    new_desktop = current.desktop if desktop is None else bool(desktop)
    path = notify_state_file(paths)
    payload = {"webhook_url": new_url, "desktop": new_desktop}
    _atomic_write_private(path, json.dumps(payload, ensure_ascii=False, indent=2), mode=0o600)
    return load_notify_config(paths)


def clear_notify_config(paths: AppPaths) -> None:
    path = notify_state_file(paths)
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def _build_webhook_payload(kind: str, url: str, title: str, message: str, event: str) -> dict[str, Any]:
    full_text = f"【{title}】\n{message}" if title else message
    if kind == "feishu":
        return {"msg_type": "text", "content": {"text": full_text}}
    if kind == "discord":
        return {"content": full_text}
    if kind == "slack":
        return {"text": full_text}
    if kind == "bark":
        return {"title": title or "cproxy", "body": message, "group": "cproxy"}
    if kind == "telegram":
        parsed = urlparse(url)
        qs = parse_qs(parsed.query)
        chat_id = (qs.get("chat_id") or [""])[0]
        return {"chat_id": chat_id, "text": full_text}
    return {
        "title": title,
        "message": message,
        "text": full_text,
        "event": event,
        "timestamp": datetime.now().isoformat(timespec="seconds"),
    }


def send_notification(
    paths: AppPaths,
    title: str,
    message: str,
    *,
    event: str = "info",
    timeout: float = 3.0,
    raise_on_error: bool = False,
) -> bool:
    """发送一条告警通知。默认静默容错（绝不阻塞主流程），`test` 命令可开启 `raise_on_error`。"""
    cfg = load_notify_config(paths)
    if not cfg.enabled:
        if raise_on_error:
            raise RuntimeError("错误: 尚未配置通知渠道（请先运行 cproxy notify set --webhook <URL> 或 --desktop）")
        return False

    sent = False
    if cfg.desktop:
        notify_bin = shutil.which("notify-send")
        if notify_bin:
            try:
                subprocess.run(
                    [notify_bin, title or "cproxy", message],
                    check=True,
                    capture_output=True,
                    timeout=timeout,
                )
                sent = True
            except Exception as exc:
                if raise_on_error and not cfg.webhook_url:
                    raise RuntimeError(f"错误: 桌面通知发送失败: {redact_text(str(exc))}") from exc
        elif raise_on_error and not cfg.webhook_url:
            raise RuntimeError("错误: 未找到 notify-send 命令，无法发送桌面通知")

    if cfg.webhook_url:
        try:
            validated_url = _validate_webhook_url(cfg.webhook_url)
        except ValueError as exc:
            if raise_on_error:
                raise RuntimeError(str(exc)) from exc
            return sent

        kind = detect_webhook_kind(validated_url)
        payload = _build_webhook_payload(kind, validated_url, title, message, event)
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = Request(
            validated_url,
            data=body,
            headers={"Content-Type": "application/json; charset=utf-8", "User-Agent": "cproxy-notify/1.1"},
            method="POST",
        )
        opener = build_opener(ProxyHandler({}), _NoRedirectHandler())
        try:
            with opener.open(req, timeout=timeout) as resp:
                status = getattr(resp, "status", 200)
                if 200 <= int(status) < 300:
                    sent = True
                elif raise_on_error:
                    raise RuntimeError(f"错误: Webhook 返回异常状态码 HTTP {status}")
        except Exception as exc:
            if raise_on_error:
                raise RuntimeError(f"错误: Webhook 推送失败: {redact_text(str(exc))}") from exc

    return sent
