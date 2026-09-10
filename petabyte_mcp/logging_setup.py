"""Structured, secret-redacting logging.

Logs go to STDERR (stdout is the MCP stdio transport and must stay clean). Every record passes
through a redaction filter so an API key, an `Authorization`/`X-API-KEY` header value, a Fernet
token (the shape of a Petabyte API key), a JWT or a `pk_...`-style key can never reach a log
line — even if some future code path formats one into a message by mistake. The configured API
key itself is registered as a known secret and replaced wherever it appears.
"""

from __future__ import annotations

import json
import logging
import re
import sys
import threading
from datetime import UTC, datetime
from typing import Any, TextIO

REDACTED = "[REDACTED]"

_HEADER_RE = re.compile(
    r"(?i)\b(x-api-key|authorization|api[_-]?key|bearer|token|secret|password)"
    r"(\s*[:=]\s*)(\"?)((?:bearer\s+)?[^\s\"',;]+)"
)
_FERNET_RE = re.compile(r"\bgAAAA[A-Za-z0-9_\-=]{20,}")
_PK_RE = re.compile(r"\bpk_[A-Za-z0-9_\-]{8,}")
_JWT_RE = re.compile(r"\bey[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}")

_known_secrets: set[str] = set()
_lock = threading.Lock()


def register_secret(value: str | None) -> None:
    """Register a literal secret (the configured API key) so it is always scrubbed."""
    if value and len(value) >= 8:
        with _lock:
            _known_secrets.add(value)


def redact(text: str) -> str:
    """Scrub secrets from free text. Safe on any string; idempotent."""
    if not text:
        return text
    with _lock:
        secrets = list(_known_secrets)
    for s in secrets:
        text = text.replace(s, REDACTED)
    text = _HEADER_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}{m.group(3)}{REDACTED}", text)
    text = _FERNET_RE.sub(REDACTED, text)
    text = _PK_RE.sub(REDACTED, text)
    text = _JWT_RE.sub(REDACTED, text)
    return text


class RedactingFilter(logging.Filter):
    """Redacts the formatted message AND any string-valued `extra` fields."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:  # noqa: BLE001 — never let a bad format string break logging
            msg = str(record.msg)
        record.msg = redact(msg)
        record.args = ()
        for key, val in list(record.__dict__.items()):
            if key.startswith("_") or key in _STD_ATTRS:
                continue
            if isinstance(val, str):
                record.__dict__[key] = redact(val)
        return True


_STD_ATTRS = frozenset(vars(logging.LogRecord("", 0, "", 0, "", (), None)).keys()) | {
    "message",
    "asctime",
}


class JsonFormatter(logging.Formatter):
    """One JSON object per line: timestamp, level, logger, message + safe extra fields."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, val in record.__dict__.items():
            if key.startswith("_") or key in _STD_ATTRS:
                continue
            if isinstance(val, (str, int, float, bool)) or val is None:
                payload[key] = val
            else:
                payload[key] = redact(str(val))
        if record.exc_info:
            payload["exception"] = redact(self.formatException(record.exc_info))
        return json.dumps(payload, ensure_ascii=False)


class TextFormatter(logging.Formatter):
    def __init__(self) -> None:
        super().__init__("%(asctime)s %(levelname)s %(name)s %(message)s")

    def format(self, record: logging.LogRecord) -> str:
        base = super().format(record)
        extras = {
            k: v
            for k, v in record.__dict__.items()
            if not k.startswith("_") and k not in _STD_ATTRS
        }
        if extras:
            base += " " + redact(json.dumps(extras, default=str, ensure_ascii=False))
        return base


def configure_logging(
    level: str = "INFO", fmt: str = "json", stream: TextIO | None = None
) -> logging.Logger:
    """Configure the `petabyte_mcp` logger hierarchy. Idempotent (replaces handlers)."""
    logger = logging.getLogger("petabyte_mcp")
    logger.setLevel(level.upper())
    logger.propagate = False
    for h in list(logger.handlers):
        logger.removeHandler(h)
    handler = logging.StreamHandler(stream or sys.stderr)
    handler.setFormatter(JsonFormatter() if fmt == "json" else TextFormatter())
    handler.addFilter(RedactingFilter())
    logger.addHandler(handler)
    return logger


def get_logger(name: str = "petabyte_mcp") -> logging.Logger:
    log = logging.getLogger(name)
    # Guarantee redaction even for loggers used before configure_logging() ran.
    if not any(isinstance(f, RedactingFilter) for f in log.filters):
        log.addFilter(RedactingFilter())
    return log
