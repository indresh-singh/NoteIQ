import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, TypeVar

import httpx

from app.auth import graph_token

log = logging.getLogger(__name__)
T = TypeVar("T")
GRAPH_ROOT = "https://graph.microsoft.com/v1.0"


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
                raise
            await asyncio.sleep(delay)
    raise RuntimeError("Retry exhausted")


class GraphClient:
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

        async def send() -> dict | str:
            token = await graph_token()
            headers = {**extra_headers, "Authorization": f"Bearer {token}"}
            async with httpx.AsyncClient(timeout=20) as client:
                response = await client.request(method, url, headers=headers, **kwargs)
            log.info("Graph method=%s status=%s", method, response.status_code)
            if response.is_error:
                try:
                    error = response.json().get("error", {})
                except ValueError:
                    error = {}
                log.warning(
                    "Graph failure code=%s inner_code=%s request_id=%s",
                    error.get("code"), (error.get("innerError") or {}).get("code"),
                    response.headers.get("request-id"),
                )
            response.raise_for_status()
            if text:
                return response.text
            return response.json() if response.content else {}

        return await retry(send) if retries else await send()

    async def list(self, path: str) -> list[dict]:
        items = []
        while path:
            page = await self.request("GET", path)
            items.extend(page.get("value", []))
            path = page.get("@odata.nextLink", "")
        return items
