"""OpenAI Responses API provider for Enterprise-project API keys.

The limiter is intentionally conservative: one request at a time, with a
minimum 30-second gap by default. It is process-local, so deployments that
need a tenant-wide ceiling should keep one worker replica until a distributed
limiter is introduced.
"""

import asyncio
import json
import logging
import time

import httpx
from pydantic import ValidationError

from app.config import Settings
from app.models import Insight
from app.observability import response_diagnostics
from app.prompts.openai_meeting_summary import (
    OPENAI_MEETING_SUMMARY_SCHEMA,
    OPENAI_SYSTEM_PROMPT,
    openai_user_prompt,
)

log = logging.getLogger(__name__)

API = "https://api.openai.com/v1/responses"
MAX_OUTPUT_TOKENS = 10_000
MEETING_SUMMARY_FORMAT = {
    "type": "json_schema",
    "name": "meeting_summary",
    "strict": True,
    "schema": OPENAI_MEETING_SUMMARY_SCHEMA,
}
_request_lock = asyncio.Lock()
_next_request_at = 0.0


class OpenAIProviderError(ValueError):
    """A safe, classified OpenAI failure suitable for logs and API responses."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class OpenAI:
    """Generate a meeting insight through the OpenAI Responses API."""

    def __init__(self, config: Settings):
        self.config = config

    async def summarize(self, key: str, subject: str, transcript_text: str) -> Insight:
        payload = {
            "model": self.config.openai_model,
            "input": [
                {"role": "system", "content": OPENAI_SYSTEM_PROMPT},
                {"role": "user", "content": openai_user_prompt(subject, transcript_text)},
            ],
            "max_output_tokens": MAX_OUTPUT_TOKENS,
            # Give the Enterprise path enough room for detailed notes and a
            # complete action register; the schema and prompt still prevent
            # unsupported filler.
            "text": {"format": MEETING_SUMMARY_FORMAT, "verbosity": "high"},
            "reasoning": {"effort": "medium"},
            # Store the response as requested so it can be inspected in the
            # Enterprise project. Do not add web-search tools: transcript
            # summarisation should rely only on the supplied meeting content.
            "store": True,
        }
        response = await self.request(**payload)
        content = response_text(response)
        status = response.get("status")
        incomplete = response.get("incomplete_details") or {}
        incomplete_reason = incomplete.get("reason")
        if status == "incomplete":
            code = (
                "OPENAI_RESPONSE_INCOMPLETE_MAX_OUTPUT_TOKENS"
                if incomplete_reason == "max_output_tokens"
                else "OPENAI_RESPONSE_CONTENT_FILTERED"
                if incomplete_reason == "content_filter"
                else "OPENAI_RESPONSE_INCOMPLETE"
            )
            self._log_unusable_response(response, content, code, incomplete_reason)
            raise OpenAIProviderError(code, "OpenAI returned an incomplete response.")
        if _has_refusal(response):
            code = "OPENAI_RESPONSE_REFUSED"
            self._log_unusable_response(response, content, code)
            raise OpenAIProviderError(code, "OpenAI declined to summarize this transcript.")
        if status not in {None, "completed"}:
            code = "OPENAI_RESPONSE_FAILED"
            self._log_unusable_response(response, content, code)
            raise OpenAIProviderError(code, "OpenAI did not complete the response.")
        if not content:
            code = "OPENAI_RESPONSE_EMPTY"
            self._log_unusable_response(response, content, code)
            raise OpenAIProviderError(code, "OpenAI returned an empty response.")
        try:
            data = json.loads(content)
        except (TypeError, json.JSONDecodeError) as error:
            code = "OPENAI_RESPONSE_INVALID_JSON"
            self._log_unusable_response(
                response,
                content,
                code,
                json_line=getattr(error, "lineno", None),
                json_column=getattr(error, "colno", None),
            )
            raise OpenAIProviderError(code, "OpenAI returned incomplete or invalid JSON.") from error
        try:
            return Insight.model_validate({**data, "id": f"openai:{key}"})
        except (TypeError, ValueError, ValidationError) as error:
            code = "OPENAI_RESPONSE_SCHEMA_INVALID"
            log.warning(
                "OpenAI response validation failed error_code=%s model=%s error_type=%s",
                code,
                self.config.openai_model,
                type(error).__name__,
                exc_info=True,
            )
            self._log_unusable_response(response, content, code)
            raise OpenAIProviderError(code, "OpenAI returned an invalid summary structure.") from error

    def _log_unusable_response(
        self,
        response: dict,
        content: str,
        code: str,
        incomplete_reason: str | None = None,
        *,
        json_line: int | None = None,
        json_column: int | None = None,
    ) -> None:
        """Record actionable metadata without logging response or transcript text."""
        usage = response.get("usage") or {}
        output_details = usage.get("output_tokens_details") or {}
        log.warning(
            "OpenAI response unusable error_code=%s model=%s response_status=%s "
            "incomplete_reason=%s response_id=%s output_chars=%s max_output_tokens=%s "
            "input_tokens=%s output_tokens=%s reasoning_tokens=%s json_line=%s json_column=%s",
            code,
            self.config.openai_model,
            response.get("status") or "unknown",
            incomplete_reason or "-",
            response.get("id") or "-",
            len(content),
            response.get("max_output_tokens") or MAX_OUTPUT_TOKENS,
            usage.get("input_tokens", "-"),
            usage.get("output_tokens", "-"),
            output_details.get("reasoning_tokens", "-"),
            json_line or "-",
            json_column or "-",
        )

    async def request(self, **payload: object) -> dict:
        global _next_request_at
        model = str(payload.get("model", "unknown"))
        async with _request_lock:
            delay = _next_request_at - time.monotonic()
            if delay > 0:
                log.info("OpenAI request delayed model=%s delay_s=%.1f", model, delay)
                await asyncio.sleep(delay)
            # Reserve the next slot before the network call, including when it
            # fails, to avoid a retry storm after a quota response.
            _next_request_at = time.monotonic() + self.config.openai_min_request_interval_seconds
            started = time.monotonic()
            try:
                async with httpx.AsyncClient(timeout=60) as client:
                    response = await client.post(
                        API,
                        headers={
                            "Authorization": f"Bearer {self.config.openai_api_key.get_secret_value()}",
                            "Content-Type": "application/json",
                        },
                        json=payload,
                    )
                log.info(
                    "OpenAI request completed model=%s status=%s duration_ms=%d request_id=%s",
                    model,
                    response.status_code,
                    (time.monotonic() - started) * 1000,
                    response.headers.get("x-request-id", "-"),
                )
                response.raise_for_status()
                return response.json()
            except httpx.HTTPStatusError as error:
                log.warning(
                    "OpenAI request rejected error_code=%s model=%s diagnostic=%s",
                    "OPENAI_HTTP_AUTH_REJECTED"
                    if error.response.status_code in {401, 403}
                    else "OPENAI_HTTP_RATE_LIMITED"
                    if error.response.status_code == 429
                    else "OPENAI_HTTP_UPSTREAM_ERROR",
                    model,
                    response_diagnostics(error.response),
                    exc_info=True,
                )
                if error.response.status_code in {401, 403}:
                    raise OpenAIProviderError(
                        "OPENAI_HTTP_AUTH_REJECTED", "OpenAI rejected this API key."
                    ) from None
                if error.response.status_code == 429:
                    raise OpenAIProviderError(
                        "OPENAI_HTTP_RATE_LIMITED",
                        "OpenAI is rate-limited; the next request is delayed.",
                    ) from None
                raise OpenAIProviderError(
                    "OPENAI_HTTP_UPSTREAM_ERROR", "OpenAI could not complete this request."
                ) from None
            except httpx.HTTPError as error:
                log.warning(
                    "OpenAI transport failure error_code=OPENAI_TRANSPORT_ERROR "
                    "model=%s error_type=%s",
                    model,
                    type(error).__name__,
                    exc_info=True,
                )
                raise OpenAIProviderError(
                    "OPENAI_TRANSPORT_ERROR", "Unable to reach OpenAI."
                ) from None


def response_text(response: dict) -> str:
    """Extract generated text from a REST response or an SDK-shaped test reply."""
    if isinstance(response.get("output_text"), str):
        return response["output_text"]
    return "".join(
        part.get("text", "")
        for item in response.get("output", [])
        if item.get("type") == "message"
        for part in item.get("content", [])
        if part.get("type") == "output_text" and isinstance(part.get("text"), str)
    )


def _has_refusal(response: dict) -> bool:
    return any(
        part.get("type") == "refusal"
        for item in response.get("output", [])
        if item.get("type") == "message"
        for part in item.get("content", [])
    )
