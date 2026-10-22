"""Structured JSON logging with request correlation and secret redaction."""

import json
import logging
import re
import sys
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)
tenant_id_var: ContextVar[str | None] = ContextVar("tenant_id", default=None)

_SENSITIVE_KEYS = re.compile(r"(api[_-]?key|authorization|password|secret|token|cookie)", re.I)
_SENSITIVE_VALUES = [
    re.compile(r"(?i)bearer\s+[a-z0-9._\-]+"),
    re.compile(r"\bsk-[A-Za-z0-9_\-]{10,}"),
    re.compile(r"(postgres(?:ql)?(?:\+\w+)?://[^:/\s]+:)[^@\s]+(@)"),
]
_RESERVED = set(logging.makeLogRecord({}).__dict__) | {"message", "asctime"}


def redact(value: Any) -> Any:
    """Recursively redact secrets from dicts/lists/strings. Keys win over values."""
    if isinstance(value, dict):
        return {
            k: "[REDACTED]" if _SENSITIVE_KEYS.search(str(k)) else redact(v)
            for k, v in value.items()
        }
    if isinstance(value, list | tuple):
        return [redact(v) for v in value]
    if isinstance(value, str):
        for pattern in _SENSITIVE_VALUES:
            value = pattern.sub(
                lambda m: f"{m.group(1)}[REDACTED]{m.group(2)}" if m.lastindex else "[REDACTED]",
                value,
            )
        return value
    return value


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": redact(record.getMessage()),
            "request_id": request_id_var.get(),
            "tenant_id": tenant_id_var.get(),
        }
        extra = {k: v for k, v in record.__dict__.items() if k not in _RESERVED}
        payload.update(redact(extra))
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
    for noisy in ("httpx", "httpcore", "uvicorn.access"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
