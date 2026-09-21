"""One redaction function, applied by every writer to disk.

Everything written to artifacts/, evidence/, runs/ and intervention requests
passes through redact(). The logging Filter calls the same function, so there
is exactly one place where the policy lives.

Redaction applies at the disk boundary, not in memory. A replay still returns a
real balance to its caller -- that is the point of the capability. What it must
not do is leave that balance on disk.
"""

from __future__ import annotations

import logging
import re
from typing import Any

# Never written anywhere, under any key spelling.
SECRET_KEYS = frozenset({
    "anthropic_api_key", "api_key", "apikey", "authorization", "cookie",
    "set-cookie", "password", "secret", "token", "session", "sess",
})

# 1,240.55 / 8915.20 / $250.00
MONEY = re.compile(r"(?<![\w.])\$?\d{1,3}(?:,\d{3})*\.\d{2}(?![\w.])")

_sensitive: dict[str, str] = {}


def register_sensitive(name: str, value: str) -> str:
    """Register an input value that must never appear verbatim on disk.

    Returns the mask, so a caller can show what the redacted form will look
    like. member_id=12345 becomes MEMB_****45: enough to correlate two runs of
    the same capability, not enough to identify the member.
    """
    value = str(value)
    if not value:
        return value
    mask = f"{re.sub(r'[^A-Za-z]', '', name).upper()[:4] or 'REDA'}_****{value[-2:]}"
    _sensitive[value] = mask
    return mask


def clear_sensitive() -> None:
    _sensitive.clear()


def redact(obj: Any) -> Any:
    """Recursively redact a JSON-shaped object."""
    if isinstance(obj, dict):
        out: dict[Any, Any] = {}
        for key, value in obj.items():
            if isinstance(key, str) and key.lower().replace("-", "_") in SECRET_KEYS:
                out[key] = "[REDACTED]"
            else:
                out[key] = redact(value)
        return out
    if isinstance(obj, (list, tuple)):
        return [redact(v) for v in obj]
    if isinstance(obj, str):
        return redact_text(obj)
    return obj


def redact_text(text: str) -> str:
    for raw, mask in _sensitive.items():
        if raw and raw in text:
            text = text.replace(raw, mask)
    return MONEY.sub("***", text)


class RedactionFilter(logging.Filter):
    """Applied to every handler, so no log record can bypass the policy."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = redact_text(record.msg)
        if record.args:
            if isinstance(record.args, dict):
                record.args = redact(record.args)
            else:
                record.args = tuple(redact(a) for a in record.args)
        return True
