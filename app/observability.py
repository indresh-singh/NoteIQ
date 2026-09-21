"""Shared, secret-safe diagnostic logging for web requests and background work."""

import contextlib
import contextvars
import json
import logging
import os
import re
import sys
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit

_request_id = contextvars.ContextVar("request_id", default="-")
_job_id = contextvars.ContextVar("job_id", default="-")
_SAFE_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_REDACTED_KEYS = {
    "access_token",
    "authorization",
    "client_secret",
    "clientstate",
    "client_state",
    "code",
    "cookie",
    "id_token",
    "password",
    "refresh_token",
    "secret",
    "set-cookie",
    "token",
}


def safe_correlation_id(value: str | None, fallback: str) -> str:
    """Accept only log-safe caller IDs; otherwise use our generated value."""
    return value if value and _SAFE_ID.fullmatch(value) else fallback


@contextlib.contextmanager
def log_context(*, request_id: str | None = None, job_id: str | int | None = None):
    tokens = []
    if request_id is not None:
        tokens.append((_request_id, _request_id.set(str(request_id))))
    if job_id is not None:
        tokens.append((_job_id, _job_id.set(str(job_id))))
    try:
        yield
    finally:
        for variable, token in reversed(tokens):
            variable.reset(token)


class ContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = _request_id.get()
        record.job_id = _job_id.get()
        return True


class UTCFormatter(logging.Formatter):
    converter = datetime.fromtimestamp

    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
        instant = datetime.fromtimestamp(record.created, timezone.utc)
        return instant.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def configure_logging() -> None:
    """Install one verbose format without enabling noisy dependency internals."""
    raw_level = os.getenv("NOTEIQ_LOG_LEVEL", "INFO").upper()
    level = getattr(logging, raw_level, None)
    if not isinstance(level, int):
        raise ValueError(f"Invalid NOTEIQ_LOG_LEVEL={raw_level!r}")
    handler = logging.StreamHandler(sys.stdout)
    handler.addFilter(ContextFilter())
    handler.setFormatter(
        UTCFormatter(
            "%(asctime)s level=%(levelname)s logger=%(name)s process=%(process)d "
            "thread=%(threadName)s request_id=%(request_id)s job_id=%(job_id)s %(message)s"
        )
    )
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)
    for name in ("httpx", "httpcore", "msal", "urllib3"):
        logging.getLogger(name).setLevel(logging.WARNING)


def safe_url(value: str) -> str:
    """Return a URL without credentials, query parameters, or fragments."""
    parsed = urlsplit(value)
    if parsed.scheme and parsed.netloc:
        host = parsed.hostname or ""
        port = f":{parsed.port}" if parsed.port else ""
        return f"{parsed.scheme}://{host}{port}{parsed.path}"
    return parsed.path


def _redact(value: Any, key: str = "", depth: int = 0) -> Any:
    if depth > 5:
        return "<max-depth>"
    normalized = key.lower().replace("-", "_")
    if normalized in _REDACTED_KEYS or any(
        marker in normalized for marker in ("secret", "password", "authorization")
    ):
        return "<redacted>"
    if isinstance(value, dict):
        return {str(k): _redact(v, str(k), depth + 1) for k, v in list(value.items())[:50]}
    if isinstance(value, list):
        return [_redact(item, depth=depth + 1) for item in value[:20]]
    if isinstance(value, str):
        return value[:1000] + ("<truncated>" if len(value) > 1000 else "")
    return value


def response_diagnostics(response: Any) -> str:
    """Extract an upstream error without leaking auth material or large content."""
    identifiers = {
        name: response.headers.get(name)
        for name in (
            "request-id",
            "client-request-id",
            "x-request-id",
            "cf-ray",
            "retry-after",
        )
        if response.headers.get(name)
    }
    content_type = response.headers.get("content-type", "")
    body: Any = None
    try:
        if "json" in content_type.lower() or response.content[:1] in (b"{", b"["):
            body = _redact(response.json())
        elif response.content:
            body = f"<{len(response.content)} bytes {content_type or 'unknown content type'}>"
    except Exception as error:  # Diagnostics must never hide the original failure.
        body = f"<unreadable response: {type(error).__name__}: {error}>"
    return json.dumps(
        {
            "status": response.status_code,
            "url": safe_url(str(response.url)),
            "headers": identifiers,
            "body": body,
        },
        ensure_ascii=True,
        default=str,
        separators=(",", ":"),
    )
