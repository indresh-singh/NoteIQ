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
from app.prompts.meeting_summary import SYSTEM_PROMPT, user_prompt

log = logging.getLogger(__name__)

API = "https://api.openai.com/v1/responses"
MAX_TRANSCRIPT_CHARS = 60_000
MAX_OUTPUT_TOKENS = 1_200
MEETING_SUMMARY_FORMAT = {
    "type": "json_schema",
    "name": "meeting_summary",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "meetingNotes": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "title": {"type": ["string", "null"]},
                        "text": {"type": ["string", "null"]},
                    },
                    "required": ["title", "text"],
                },
            },
            "actionItems": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "title": {"type": ["string", "null"]},
                        "text": {"type": ["string", "null"]},
                        "ownerDisplayName": {"type": ["string", "null"]},
                        "dueDate": {"type": ["string", "null"]},
                    },
                    "required": ["title", "text", "ownerDisplayName", "dueDate"],
                },
            },
        },
        "required": ["meetingNotes", "actionItems"],
    },
}
_request_lock = asyncio.Lock()
_next_request_at = 0.0


class OpenAI:
    """Generate a meeting insight through the OpenAI Responses API."""

    def __init__(self, config: Settings):
        self.config = config

    async def summarize(self, key: str, subject: str, transcript_text: str) -> Insight:
        text = transcript_text[:MAX_TRANSCRIPT_CHARS]
        payload = {
            "model": self.config.openai_model,
            "input": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt(subject, text)},
            ],
            "max_output_tokens": MAX_OUTPUT_TOKENS,
            # Keep summaries concise and inexpensive, following the Responses
            # controls used by the Enterprise example.
            "text": {"format": MEETING_SUMMARY_FORMAT, "verbosity": "medium"},
            "reasoning": {"effort": "low"},
            # Store the response as requested so it can be inspected in the
            # Enterprise project. Do not add web-search tools: transcript
            # summarisation should rely only on the supplied meeting content.
            "store": True,
        }
        response = await self.request(**payload)
        try:
            content = response_text(response)
            data = json.loads(content)
            return Insight.model_validate({**data, "id": f"openai:{key}"})
        except (TypeError, ValueError, ValidationError) as error:
            log.warning(
                "OpenAI response parsing failed model=%s response_keys=%s error_type=%s",
                self.config.openai_model,
                sorted(response.keys()),
                type(error).__name__,
                exc_info=True,
            )
            raise ValueError("OpenAI did not return a usable summary.") from error

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
                    "OpenAI request rejected model=%s diagnostic=%s",
                    model,
                    response_diagnostics(error.response),
                    exc_info=True,
                )
                if error.response.status_code in {401, 403}:
                    raise ValueError("OpenAI rejected this API key.") from None
                if error.response.status_code == 429:
                    raise ValueError(
                        "OpenAI is rate-limited; the next request is delayed."
                    ) from None
                raise ValueError("OpenAI could not complete this request.") from None
            except httpx.HTTPError as error:
                log.warning(
                    "OpenAI transport failure model=%s error_type=%s",
                    model,
                    type(error).__name__,
                    exc_info=True,
                )
                raise ValueError("Unable to reach OpenAI.") from None


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
