import asyncio
import hashlib
import logging
import re
import time
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, TypeVar
from uuid import uuid4

import httpx

from app.auth import graph_token
from app.observability import response_diagnostics, safe_url

log = logging.getLogger(__name__)
T = TypeVar("T")
GRAPH_ROOT = "https://graph.microsoft.com/v1.0"


def safe_graph_url(url: str) -> str:
    """Keep the Graph operation visible while hashing tenant object IDs."""
    value = safe_url(url)

    def tagged(kind: str, identifier: str) -> str:
        tag = hashlib.sha256(identifier.encode()).hexdigest()[:8]
        return f"<{kind}:{tag}>"

    value = re.sub(r"(?<=/users/)[^/]+", lambda match: tagged("user", match.group()), value)
    value = re.sub(
        r"(?<=/onlineMeetings/)(?!getAllTranscripts\()[^/]+",
        lambda match: tagged("meeting", match.group()),
        value,
    )
    value = re.sub(
        r"(?<=/transcripts/)(?!content(?:/|$))[^/]+",
        lambda match: tagged("transcript", match.group()),
        value,
    )
    value = re.sub(r"(?<=/aiInsights/)[^/]+", lambda match: tagged("insight", match.group()), value)
    value = re.sub(
        r"(?<=/subscriptions/)[^/]+",
        lambda match: tagged("subscription", match.group()),
        value,
    )
    value = re.sub(r"(?<=/groups/)[^/]+", lambda match: tagged("group", match.group()), value)
    value = re.sub(r"(?<=/planner/plans/)[^/]+", lambda match: tagged("plan", match.group()), value)
    value = re.sub(r"(?<=/planner/tasks/)[^/]+", lambda match: tagged("task", match.group()), value)
    value = re.sub(
        r"(?<=/planner/buckets/)[^/]+", lambda match: tagged("bucket", match.group()), value
    )
    # The organizer ID is repeated inside this OData function's path segment.
    value = re.sub(r"getAllTranscripts\([^)]*\)", "getAllTranscripts(<parameters>)", value)
    return value


def retryable(error: Exception) -> bool:
    return isinstance(error, httpx.TransportError) or (
        isinstance(error, httpx.HTTPStatusError)
        and (error.response.status_code == 429 or error.response.status_code >= 500)
    )


async def retry(operation: Callable[[], Awaitable[T]]) -> T:
    for attempt in range(3):
        try:
            return await operation()
        except (httpx.HTTPStatusError, httpx.TransportError) as error:
            if not retryable(error) or attempt == 2:
                log.warning(
                    "Graph retry stopped attempt=%s max_attempts=3 retryable=%s "
                    "error_type=%s http_status=%s",
                    attempt + 1,
                    retryable(error),
                    type(error).__name__,
                    error.response.status_code
                    if isinstance(error, httpx.HTTPStatusError)
                    else "transport",
                    exc_info=not isinstance(error, httpx.HTTPStatusError),
                )
                raise
            delay = float(2**attempt)
            if isinstance(error, httpx.HTTPStatusError):
                value = error.response.headers.get("Retry-After", "")
                try:
                    delay = max(delay, float(value))
                except ValueError:
                    try:
                        date = parsedate_to_datetime(value)
                        delay = max(delay, (date - datetime.now(timezone.utc)).total_seconds())
                    except (ValueError, TypeError):
                        pass
            # Leave long throttles to the queue's delayed retry instead of holding a worker.
            if delay > 60:
                log.warning(
                    "Graph retry deferred to job queue attempt=%s delay_s=%.3f error_type=%s",
                    attempt + 1,
                    delay,
                    type(error).__name__,
                    exc_info=not isinstance(error, httpx.HTTPStatusError),
                )
                raise
            log.warning(
                "Graph retry scheduled attempt=%s next_attempt=%s delay_s=%.3f "
                "error_type=%s http_status=%s",
                attempt + 1,
                attempt + 2,
                delay,
                type(error).__name__,
                error.response.status_code
                if isinstance(error, httpx.HTTPStatusError)
                else "transport",
                exc_info=not isinstance(error, httpx.HTTPStatusError),
            )
            await asyncio.sleep(delay)
    raise RuntimeError("Retry exhausted")


class GraphClient:
    """Talks to Graph over one reusable connection pool.

    A client per request meant a fresh TLS handshake on every call, and the
    polling sweep alone makes a few thousand an hour. Created on first use so it
    binds to the running loop, and closed with the application.
    """

    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None

    def client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=20,
                limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
        self._client = None

    async def request(
        self, method: str, path: str, *, text: bool = False, retries: bool = True, **kwargs: Any
    ) -> dict | str:
        if path.startswith(GRAPH_ROOT + "/"):
            url = path
        elif path.startswith("/") and not path.startswith("//"):
            url = GRAPH_ROOT + path
        else:
            raise ValueError("Unsupported Graph URL")

        extra_headers = kwargs.pop("headers", {})
        operation_id = str(uuid4())
        safe_target = safe_graph_url(url)

        async def send() -> dict | str:
            started = time.monotonic()
            log.info(
                "Graph request started operation_id=%s method=%s url=%s response_mode=%s",
                operation_id,
                method,
                safe_target,
                "text" if text else "json",
            )
            try:
                token = await graph_token()
                headers = {
                    **extra_headers,
                    "Authorization": f"Bearer {token}",
                    "client-request-id": operation_id,
                    "return-client-request-id": "true",
                }
                response = await self.client().request(method, url, headers=headers, **kwargs)
            except Exception as error:
                log.exception(
                    "Graph request transport failure operation_id=%s method=%s url=%s "
                    "duration_ms=%d error_type=%s error=%s",
                    operation_id,
                    method,
                    safe_target,
                    (time.monotonic() - started) * 1000,
                    type(error).__name__,
                    error,
                )
                raise
            log.info(
                "Graph request completed operation_id=%s method=%s url=%s status=%s "
                "duration_ms=%d response_bytes=%s request_id=%s client_request_id=%s",
                operation_id,
                method,
                safe_target,
                response.status_code,
                (time.monotonic() - started) * 1000,
                len(response.content),
                response.headers.get("request-id", "-"),
                response.headers.get("client-request-id", "-"),
            )
            if response.is_error:
                try:
                    error = response.json().get("error", {})
                except ValueError:
                    error = {}
                log.warning(
                    "Graph request rejected operation_id=%s method=%s url=%s code=%s "
                    "inner_code=%s diagnostic=%s",
                    operation_id,
                    method,
                    safe_target,
                    error.get("code"),
                    (error.get("innerError") or {}).get("code"),
                    response_diagnostics(response),
                )
            response.raise_for_status()
            if text:
                return response.text
            return response.json() if response.content else {}

        return await retry(send) if retries else await send()

    async def list(self, path: str) -> list[dict]:
        items = []
        pages = 0
        started = time.monotonic()
        while path:
            page = await self.request("GET", path)
            items.extend(page.get("value", []))
            path = page.get("@odata.nextLink", "")
            pages += 1
        log.info(
            "Graph list completed pages=%s items=%s duration_ms=%d",
            pages,
            len(items),
            (time.monotonic() - started) * 1000,
        )
        return items
