"""Additional AI provider: summarizes Copilot transcripts via OpenRouter.

Microsoft 365 Copilot remains the source of the transcript text (see
app/transcripts.py); this module sends that same text to a model hosted on
OpenRouter to produce an extra meeting summary and action item list,
alongside whatever Copilot's own aiInsights eventually deliver.

Requests OpenRouter's JSON-schema structured-output mode and restricts routing
to provider endpoints that support it. The prompt repeats the shape as a
defensive hint, while the permissive parser still handles harmless wrappers.
"""

import json
import logging
import re
import time

import httpx
from pydantic import ValidationError

from app.config import Settings
from app.models import Insight
from app.observability import response_diagnostics
from app.prompts.meeting_summary import MEETING_SUMMARY_SCHEMA, SYSTEM_PROMPT, user_prompt

log = logging.getLogger(__name__)

API = "https://openrouter.ai/api/v1/chat/completions"
MAX_TRANSCRIPT_CHARS = 60_000
MAX_OUTPUT_TOKENS = 1_200
JSON_OBJECT_PATTERN = re.compile(r"\{.*\}", re.DOTALL)
# OpenRouter's own router picks a healthy free model at request time.
FREE_ROUTER_MODEL = "openrouter/free"


class OpenRouterAuthError(ValueError):
    """The API key itself was rejected; retrying with a different model won't help."""


def extract_json_object(content: str) -> dict:
    """Pull a JSON object out of a chat reply that may not be pure JSON.

    Free-tier models frequently ignore formatting instructions and wrap the
    JSON in markdown fences or add a sentence of commentary before/after it,
    so this tries increasingly permissive fallbacks before giving up.
    """
    text = content.strip()
    if text.startswith("```"):
        text = text.removeprefix("```json").removeprefix("```").rstrip("`").strip()
    try:
        return json.loads(text)
    except ValueError:
        pass
    match = JSON_OBJECT_PATTERN.search(text)
    if not match:
        raise ValueError("No JSON object found in the model's reply.")
    return json.loads(match.group())


def normalize_null_strings(value):
    """Repair nullable schema fields returned as the literal string ``"null"``.

    Some routed models satisfy the JSON object shape but serialize a missing
    value as a string. Normalize recursively before Pydantic validation so the
    bad sentinel is never saved or rendered as user-facing content.
    """
    if isinstance(value, list):
        return [normalize_null_strings(item) for item in value]
    if isinstance(value, dict):
        return {key: normalize_null_strings(item) for key, item in value.items()}
    if isinstance(value, str) and value.strip().lower() == "null":
        return None
    return value


class OpenRouter:
    def __init__(self, config: Settings):
        self.config = config

    async def summarize(self, key: str, subject: str, transcript_text: str) -> Insight:
        text = transcript_text[:MAX_TRANSCRIPT_CHARS]
        models = [self.config.openrouter_model]
        if FREE_ROUTER_MODEL not in models:
            models.append(FREE_ROUTER_MODEL)
        error: ValueError | None = None
        for position, model in enumerate(models):
            try:
                return await self._summarize_with_model(model, key, subject, text)
            except OpenRouterAuthError:
                # Retrying with a different model can't fix a bad key.
                raise
            except ValueError as failure:
                error = failure
                if position < len(models) - 1:
                    log.warning(
                        "OpenRouter model=%s failed reason=%s; falling back to model=%s",
                        model,
                        failure,
                        models[position + 1],
                    )
        raise error

    async def _summarize_with_model(self, model: str, key: str, subject: str, text: str) -> Insight:
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt(subject, text)},
            ],
            "max_tokens": MAX_OUTPUT_TOKENS,
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "meeting_summary",
                    "strict": True,
                    "schema": MEETING_SUMMARY_SCHEMA,
                },
            },
            # A model can have several provider endpoints with different
            # capabilities. Do not silently route this request to an endpoint
            # that ignores the schema.
            "provider": {"require_parameters": True},
            # Reasoning models default to putting their answer in a separate
            # reasoning/reasoning_content field and leaving content empty; ask
            # for it to be turned off, and fall back to that field if a model
            # ignores the request (some reasoning-mandatory models do).
            "reasoning": {"enabled": False},
        }
        started = time.monotonic()
        log.info(
            "OpenRouter summary started model=%s transcript_chars=%s subject_chars=%s",
            model,
            len(text),
            len(subject),
        )
        response = await self.request(**payload)
        try:
            message = response["choices"][0]["message"]
            content = (
                message.get("content")
                or message.get("reasoning")
                or message.get("reasoning_content")
                or ""
            )
            data = normalize_null_strings(extract_json_object(content))
        except (KeyError, IndexError, TypeError, ValueError) as error:
            log.exception(
                "OpenRouter response parsing failed model=%s duration_ms=%d "
                "choice_count=%s response_keys=%s error_type=%s error=%s",
                model,
                (time.monotonic() - started) * 1000,
                len(response.get("choices") or []),
                sorted(response.keys()),
                type(error).__name__,
                error,
            )
            raise ValueError("OpenRouter did not return a usable summary.") from error
        try:
            insight = Insight.model_validate({**data, "id": f"openrouter:{key}"})
            choice = (response.get("choices") or [{}])[0]
            usage = response.get("usage") or {}
            log.info(
                "OpenRouter summary completed requested_model=%s actual_model=%s provider=%s "
                "finish_reason=%s duration_ms=%d prompt_tokens=%s completion_tokens=%s "
                "notes=%s actions=%s",
                model,
                response.get("model", "-"),
                response.get("provider", "-"),
                choice.get("finish_reason", "-"),
                (time.monotonic() - started) * 1000,
                usage.get("prompt_tokens", "-"),
                usage.get("completion_tokens", "-"),
                len(insight.meetingNotes),
                len(insight.actionItems),
            )
            return insight
        except ValidationError as error:
            log.warning(
                "OpenRouter summary validation failed model=%s duration_ms=%d errors=%s",
                model,
                (time.monotonic() - started) * 1000,
                error.errors(include_input=False),
                exc_info=True,
            )
            raise ValueError("OpenRouter returned an unexpected summary shape.") from error

    async def request(self, **payload) -> dict:
        headers = {
            "Authorization": f"Bearer {self.config.openrouter_api_key.get_secret_value()}",
            "HTTP-Referer": self.config.public_url,
            "X-Title": "NoteIQ",
        }
        started = time.monotonic()
        model = payload.get("model", "unknown")
        try:
            async with httpx.AsyncClient(timeout=60) as client:
                response = await client.post(API, headers=headers, json=payload)
            log.info(
                "OpenRouter request completed model=%s status=%s duration_ms=%d "
                "response_bytes=%s request_id=%s",
                model,
                response.status_code,
                (time.monotonic() - started) * 1000,
                len(response.content),
                response.headers.get("x-request-id", "-"),
            )
            response.raise_for_status()
            return response.json()
        except httpx.HTTPStatusError as error:
            log.warning(
                "OpenRouter request rejected model=%s diagnostic=%s",
                model,
                response_diagnostics(error.response),
                exc_info=True,
            )
            if error.response.status_code in {401, 403}:
                raise OpenRouterAuthError("OpenRouter rejected this API key.") from None
            if error.response.status_code == 429:
                raise ValueError("OpenRouter is rate-limited for this model right now.") from None
            raise ValueError("OpenRouter could not complete this request.") from None
        except httpx.HTTPError as error:
            log.exception(
                "OpenRouter transport failure model=%s duration_ms=%d error_type=%s error=%s",
                model,
                (time.monotonic() - started) * 1000,
                type(error).__name__,
                error,
            )
            raise ValueError("Unable to reach OpenRouter.") from None
