from __future__ import annotations

import re
from typing import Any

SENSITIVE_KEY_RE = re.compile(
    r"(?:"
    r"secret|token|password|credential|authorization|uuid|"
    r"private[-_]?key|preshared[-_]?key|api[-_]?key|cookie|webhook|short[-_]?id|"
    r"(?<![a-z0-9])psk(?![a-z0-9])|"
    r"(?<![a-z0-9])auth(?:entication|[-_]?str)?(?![a-z0-9])"
    r")",
    re.IGNORECASE,
)

_SENSITIVE_KEY_TOKEN = (
    r"(?:[A-Za-z0-9_-]*(?:secret|token|password|credential|authorization|"
    r"private[-_]?key|preshared[-_]?key|api[-_]?key|cookie|webhook)"
    r"|(?:[A-Za-z0-9_-]*[-_])?(?:uuid|psk|short[-_]?id|auth(?:entication|[-_]?str)?))"
)

AUTH_HEADER_RE = re.compile(
    r"(?i)\b(Authorization\s*:\s*(?:Bearer|Basic|Token)\s+)(?!\[REDACTED\])\S+"
)
BEARER_RE = re.compile(r"(?i)\bBearer\s+(?!\[REDACTED\])[A-Za-z0-9._~+/-]+=*")

SENSITIVE_ASSIGNMENT_RE = re.compile(
    r"(?i)"
    r"(?<![A-Za-z0-9_-])"
    r"(?P<key>" + _SENSITIVE_KEY_TOKEN + r")"
    r"(?P<sep>[\"']?\s*[:=]\s*)"
    r"(?!(?:Bearer|Basic|Token)\s+\[REDACTED\])"
    r"(?P<val>\"(?:[^\"\\]|\\.)*\"|'(?:[^'\\]|\\.)*'|[^,\s}\];\"']+)"
)

SENSITIVE_FLAG_RE = re.compile(
    r"(?i)"
    r"(?P<key>--?" + _SENSITIVE_KEY_TOKEN + r")"
    r"(?P<sep>\s+)"
    r"(?P<val>\"(?:[^\"\\]|\\.)*\"|'(?:[^'\\]|\\.)*'|[^,\s}\];\"']+)"
)

URL_USERINFO_RE = re.compile(r"([A-Za-z][A-Za-z0-9+.-]*://)([^\s/@?#]+)@")
URL_QUERY_RE = re.compile(r"([A-Za-z][A-Za-z0-9+.-]*://[^\s?#]+\?)[^\s#]+")
URL_TELEGRAM_BOT_RE = re.compile(
    r"(https?://[^\s/?#]*(?:telegram|tg)[^\s/?#]*/bot)[^/\s?#]+|"
    r"(https?://[^\s/?#]+(?:/[^\s/?#]*)*/bot)(?=\d+:|\d+[A-Za-z0-9_-]|[^/\s?#]*:)[^/\s?#]+",
    re.IGNORECASE,
)
URL_SLACK_WEBHOOK_RE = re.compile(
    r"(https?://hooks\.slack\.com/services/)[^\s?#]+",
    re.IGNORECASE,
)
URL_BARK_KEY_RE = re.compile(
    r"(https?://api\.day\.app/)[^/\s?#]+",
    re.IGNORECASE,
)
URL_SENSITIVE_PATH_RE = re.compile(
    r"(https?://[^\s/?#]+(?:/[^\s/?#]*)*/(?:webhooks?|hooks?|bark|sub|subscribe|subscription|link)/)"
    r"(?:[^/\s?#]+(?:/[^/\s?#]+)?(?=/?(?:\?|\s|#|$))|[^/\s?#]+)",
    re.IGNORECASE,
)


def _redact_assignment_match(match: re.Match[str]) -> str:
    key = match.group("key")
    sep = match.group("sep")
    val = match.group("val")
    if val == "[REDACTED]" or val in {'"[REDACTED]"', "'[REDACTED]'"}:
        return match.group(0)
    if len(val) >= 2 and val[0] == '"' and val[-1] == '"':
        return f'{key}{sep}"[REDACTED]"'
    if len(val) >= 2 and val[0] == "'" and val[-1] == "'":
        return f"{key}{sep}'[REDACTED]'"
    return f"{key}{sep}[REDACTED]"


def _redact_telegram_bot(match: re.Match[str]) -> str:
    prefix = match.group(1) or match.group(2) or ""
    return f"{prefix}[REDACTED]"


def redact_text(text: str) -> str:
    redacted = URL_USERINFO_RE.sub(r"\1[REDACTED]@", text)
    redacted = URL_TELEGRAM_BOT_RE.sub(_redact_telegram_bot, redacted)
    redacted = URL_SLACK_WEBHOOK_RE.sub(r"\1[REDACTED]", redacted)
    redacted = URL_BARK_KEY_RE.sub(r"\1[REDACTED]", redacted)
    redacted = URL_SENSITIVE_PATH_RE.sub(r"\1[REDACTED]", redacted)
    redacted = URL_QUERY_RE.sub(r"\1...", redacted)
    redacted = AUTH_HEADER_RE.sub(r"\1[REDACTED]", redacted)
    redacted = BEARER_RE.sub("Bearer [REDACTED]", redacted)
    redacted = SENSITIVE_FLAG_RE.sub(_redact_assignment_match, redacted)
    return SENSITIVE_ASSIGNMENT_RE.sub(_redact_assignment_match, redacted)


def redact_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): "[REDACTED]" if SENSITIVE_KEY_RE.search(str(key)) else redact_value(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_value(item) for item in value]
    if isinstance(value, str):
        return redact_text(value)
    return value
