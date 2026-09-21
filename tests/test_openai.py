import json

import httpx
import pytest

from app.config import settings
from app.openai import MAX_OUTPUT_TOKENS, OpenAI


def enable_openai(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-enterprise-test")
    monkeypatch.setenv("OPENAI_MODEL", "gpt-5-nano")
    monkeypatch.setenv("OPENAI_MIN_REQUEST_INTERVAL_SECONDS", "1")
    settings.cache_clear()


def mock_client(monkeypatch, handle):
    original = httpx.AsyncClient
    monkeypatch.setattr(
        "app.openai.httpx.AsyncClient",
        lambda **kwargs: original(transport=httpx.MockTransport(handle), **kwargs),
    )


async def test_openai_uses_responses_api_and_parses_output_text(monkeypatch):
    enable_openai(monkeypatch)
    payloads = []

    def handle(request):
        assert request.url == httpx.URL("https://api.openai.com/v1/responses")
        assert request.headers["Authorization"] == "Bearer sk-enterprise-test"
        payloads.append(json.loads(request.read()))
        return httpx.Response(
            200,
            json={
                "output_text": (
                    '{"meetingNotes": [{"text": "Scope agreed."}], '
                    '"actionItems": [{"text": "Send plan", "ownerDisplayName": "Ada"}]}'
                )
            },
        )

    mock_client(monkeypatch, handle)
    insight = await OpenAI(settings()).summarize("meeting-1", "Planning", "Hello")

    assert insight.id == "openai:meeting-1"
    assert insight.meetingNotes[0].text == "Scope agreed."
    assert insight.actionItems[0].ownerDisplayName == "Ada"
    assert payloads[0]["model"] == "gpt-5-nano"
    assert payloads[0]["max_output_tokens"] == MAX_OUTPUT_TOKENS


@pytest.mark.parametrize("status,message", [(401, "rejected"), (429, "rate-limited")])
async def test_openai_surfaces_safe_http_errors(monkeypatch, status, message):
    enable_openai(monkeypatch)
    mock_client(monkeypatch, lambda request: httpx.Response(status))
    with pytest.raises(ValueError, match=message):
        await OpenAI(settings()).summarize("meeting-1", "Planning", "Hello")
