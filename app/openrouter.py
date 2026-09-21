"""Additional AI provider: summarizes Copilot transcripts via OpenRouter.

Microsoft 365 Copilot remains the source of the transcript text (see
app/transcripts.py); this module sends that same text to a model hosted on
OpenRouter to produce an extra meeting summary and action item list,
alongside whatever Copilot's own aiInsights eventually deliver.

Deliberately doesn't request OpenRouter's structured-outputs / JSON-schema
mode: many free-tier models either ignore it or reject the request outright
(some don't even honor plain response_format json_object), so this relies
only on prompt instructions plus a permissive parser below.
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
from app.prompts.meeting_summary import SYSTEM_PROMPT, user_prompt

log = logging.getLogger(__name__)

API = "https://openrouter.ai/api/v1/chat/completions"
MAX_TRANSCRIPT_CHARS = 60_000
JSON_OBJECT_PATTERN = re.compile(r"\{.*\}", re.DOTALL)
# Used when the configured model fails (rate limit, outage, bad output) — a free
# model on a different upstream provider, so it draws from a separate quota.
FALLBACK_MODEL = "nvidia/nemotron-3-super-120b-a12b:free"
# Last resort: OpenRouter's own router that picks a healthy free model at
# request time, so it isn't tied to any single model's quota at all.
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


class OpenRouter:
    def __init__(self, config: Settings):
        self.config = config

    async def summarize(self, key: str, subject: str, transcript_text: str) -> Insight:
        text = transcript_text[:MAX_TRANSCRIPT_CHARS]
        models = [self.config.openrouter_model]
        for candidate in (FALLBACK_MODEL, FREE_ROUTER_MODEL):
            if candidate not in models:
                models.append(candidate)
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
            # Reasoning models default to putting their answer in a separate
            # reasoning/reasoning_content field and leaving content empty; ask
            # for it to be turned off, and fall back to that field if a model
            # ignores the request (some reasoning-mandatory models do).
            "reasoning": {"enabled": False},
        }
        if model == FALLBACK_MODEL:
            # We need the full response in one piece to parse it as JSON, and
            # streaming is opt-in on OpenRouter anyway; being explicit here
            # avoids any provider-side default that might stream regardless.
            payload["stream"] = False
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
            data = extract_json_object(content)
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
            log.info(
                "OpenRouter summary completed model=%s duration_ms=%d notes=%s actions=%s",
                model,
                (time.monotonic() - started) * 1000,
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
