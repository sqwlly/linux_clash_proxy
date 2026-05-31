from __future__ import annotations

import re
from typing import Any

SENSITIVE_KEY_RE = re.compile(r"(secret|token|password|credential|authorization)", re.IGNORECASE)
SENSITIVE_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(secret|token|password|credential|authorization)\b([\"'=:\s]+)([^,\s}\]]+)"
)
BEARER_RE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]+=*")
URL_QUERY_RE = re.compile(r"(https?://[^\s?#]+[^\s?]*\?)[^\s]+")


def redact_text(text: str) -> str:
    redacted = URL_QUERY_RE.sub(r"\1...", text)
    redacted = BEARER_RE.sub("Bearer [REDACTED]", redacted)
    return SENSITIVE_ASSIGNMENT_RE.sub(r"\1\2[REDACTED]", redacted)


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
