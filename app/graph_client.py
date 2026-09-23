import asyncio
import hashlib
import logging
import math
import re
import time
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
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


# How long to pause when Graph throttles without saying for how long. Graph
# normally sends Retry-After; this only covers a response that omits it.
DEFAULT_THROTTLE_SECONDS = 30.0


def retry_after_seconds(response: httpx.Response) -> float | None:
    """Graph's Retry-After as seconds from now, in either of its two forms."""
    value = response.headers.get("Retry-After", "")
    try:
        return max(float(value), 0.0)
    except ValueError:
        pass
    try:
        date = parsedate_to_datetime(value)
    except (ValueError, TypeError):
        return None
    return max((date - datetime.now(timezone.utc)).total_seconds(), 0.0)


def graph_throttle_seconds(error: BaseException | None) -> float | None:
    """Seconds Graph asked us to back off, if a Graph 429 is behind this error.

    Follows the exception chain because jobs re-raise Graph failures under
    their own message (RuntimeError(...) from error). Only Graph's own 429s
    count: an AI provider's rate limit throttles that provider, not the tenant,
    and those are raised `from None`, which this respects.
    """
    seen = set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        if (
            isinstance(error, httpx.HTTPStatusError)
            and error.response.status_code == 429
            and error.request.url.host == "graph.microsoft.com"
        ):
            seconds = retry_after_seconds(error.response)
            return DEFAULT_THROTTLE_SECONDS if seconds is None else seconds
        error = error.__cause__ or (None if error.__suppress_context__ else error.__context__)
    return None


class GraphBusy(Exception):
    """Graph is throttling the tenant: tell the person when to try again.

    Deliberately not a ValueError, which the Planner and web layers turn into
    a generic "could not complete" message; this passes through them to the
    web app's 429 handler instead.
    """

    def __init__(self, retry_after: float, *, before: str = "", after: str = ""):
        self.retry_after = max(math.ceil(retry_after), 1)
        seconds = f"{self.retry_after} second{'' if self.retry_after == 1 else 's'}"
        super().__init__(
            f"{before}Microsoft 365 is limiting requests right now. "
            f"Try again in about {seconds}.{after}"
        )


def busy_from(error: BaseException) -> GraphBusy | None:
    """The GraphBusy to show for this error, if Graph throttling caused it."""
    seconds = graph_throttle_seconds(error)
    return None if seconds is None else GraphBusy(seconds)


# The longest a throttled call waits out Retry-After before giving up. The
# worker can afford a minute; a person waiting on a button cannot, so
# interactive requests lower it and get "try again in N seconds" instead.
INTERACTIVE_RETRY_WAIT_SECONDS = 5.0
_retry_wait_limit: ContextVar[float] = ContextVar("graph_retry_wait_limit", default=60.0)


@contextmanager
def interactive_requests() -> Iterator[None]:
    """Graph calls made inside this block wait at most a few seconds on a 429."""
    token = _retry_wait_limit.set(INTERACTIVE_RETRY_WAIT_SECONDS)
    try:
        yield
    finally:
        _retry_wait_limit.reset(token)


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
                delay = max(delay, retry_after_seconds(error.response) or 0.0)
            # Leave long throttles to the queue's delayed retry instead of
            # holding a worker, or to the person instead of holding a request.
            limit = _retry_wait_limit.get()
            if delay > limit:
                log.warning(
                    "Graph retry handed back to caller attempt=%s delay_s=%.3f limit_s=%.0f "
                    "error_type=%s",
                    attempt + 1,
                    delay,
                    limit,
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

    def __init__(self, on_throttle: Callable[[float], Any] | None = None) -> None:
        self._client: httpx.AsyncClient | None = None
        # Called with Retry-After seconds on every 429, so one throttled call
        # pauses all Graph work for the tenant rather than only itself.
        self.on_throttle = on_throttle

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
        self,
        method: str,
        path: str,
        *,
        text: bool = False,
        retries: bool = True,
        access_token: str | None = None,
        graph_version: str = "v1.0",
        **kwargs: Any,
    ) -> dict | str:
        if graph_version not in {"v1.0", "beta"}:
            raise ValueError("Unsupported Graph version")
        root = "https://graph.microsoft.com/" + graph_version
        if path.startswith(root + "/"):
            url = path
        elif path.startswith("/") and not path.startswith("//"):
            url = root + path
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
                token = access_token if access_token is not None else await graph_token()
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
            if response.status_code == 429:
                self._throttled(response, safe_target)
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

    def _throttled(self, response: httpx.Response, safe_target: str) -> None:
        seconds = retry_after_seconds(response)
        seconds = DEFAULT_THROTTLE_SECONDS if seconds is None else seconds
        log.warning(
            "Graph throttled url=%s retry_after_s=%.1f retry_after_header=%s",
            safe_target,
            seconds,
            "present" if response.headers.get("Retry-After") else "missing",
        )
        if self.on_throttle is None:
            return
        try:
            self.on_throttle(seconds)
        except Exception as error:
            # Recording the pause is best effort: failing to record it must
            # not turn a throttled call into a different error.
            log.exception(
                "Graph throttle pause could not be recorded error_type=%s error=%s",
                type(error).__name__,
                error,
            )

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
