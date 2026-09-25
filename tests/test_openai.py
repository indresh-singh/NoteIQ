import json

import httpx
import pytest

from app.config import settings
from app.openai import MAX_OUTPUT_TOKENS, MEETING_SUMMARY_FORMAT, OpenAI, OpenAIProviderError


def enable_openai(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-enterprise-test")
    monkeypatch.setenv("OPENAI_MODEL", "gpt-5.6-luna")
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
    # Longer than the former 60,000-character cap: the full transcript must
    # reach the provider without application-side truncation.
    transcript = "x" * 60_001

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
    insight = await OpenAI(settings()).summarize("meeting-1", "Planning", transcript)

    assert insight.id == "openai:meeting-1"
    assert insight.meetingNotes[0].text == "Scope agreed."
    assert insight.actionItems[0].ownerDisplayName == "Ada"
    assert payloads[0]["model"] == "gpt-5.6-luna"
    assert payloads[0]["max_output_tokens"] == MAX_OUTPUT_TOKENS
    assert MAX_OUTPUT_TOKENS == 10_000
    assert payloads[0]["text"] == {
        "format": MEETING_SUMMARY_FORMAT,
        "verbosity": "high",
    }
    schema = payloads[0]["text"]["format"]
    assert schema["type"] == "json_schema"
    assert schema["strict"] is True
    assert schema["schema"]["additionalProperties"] is False
    assert payloads[0]["reasoning"] == {"effort": "medium"}
    assert payloads[0]["store"] is True
    assert f"<transcript>\n{transcript}\n</transcript>" in payloads[0]["input"][1]["content"]
    assert "detailed, structured, decision-useful record" in payloads[0]["input"][0]["content"]
    note_schema = schema["schema"]["properties"]["meetingNotes"]["items"]
    assert note_schema["properties"]["subpoints"]["items"]["type"] == "object"
    assert list(schema["schema"]["properties"])[0] == "actionItems"
    assert "only the high-value concrete follow-ups" in payloads[0]["input"][0]["content"]


async def test_openai_parses_the_rest_api_output_array(monkeypatch):
    enable_openai(monkeypatch)
    mock_client(
        monkeypatch,
        lambda request: httpx.Response(
            200,
            json={
                "output": [
                    {
                        "type": "message",
                        "content": [
                            {
                                "type": "output_text",
                                "text": '{"meetingNotes": [], "actionItems": []}',
                            }
                        ],
                    }
                ]
            },
        ),
    )
    insight = await OpenAI(settings()).summarize("meeting-1", "Planning", "Hello")
    assert insight.id == "openai:meeting-1"


async def test_openai_limits_generated_actions_to_ten(monkeypatch):
    enable_openai(monkeypatch)
    mock_client(
        monkeypatch,
        lambda request: httpx.Response(
            200,
            json={
                "output_text": json.dumps(
                    {
                        "meetingNotes": [],
                        "actionItems": [{"text": f"Task {number}"} for number in range(12)],
                    }
                ),
            },
        ),
    )
    insight = await OpenAI(settings()).summarize("meeting-1", "Planning", "Transcript")
    assert len(insight.actionItems) == 10
    assert insight.actionItems[-1].text == "Task 9"
    assert MEETING_SUMMARY_FORMAT["schema"]["properties"]["actionItems"]["maxItems"] == 10


async def test_openai_classifies_and_logs_an_incomplete_response(monkeypatch, caplog):
    enable_openai(monkeypatch)
    mock_client(
        monkeypatch,
        lambda request: httpx.Response(
            200,
            json={
                "id": "resp_test",
                "status": "incomplete",
                "incomplete_details": {"reason": "max_output_tokens"},
                "max_output_tokens": 10_000,
                "output_text": '{"actionItems":[{"text":"private partial text',
                "usage": {
                    "input_tokens": 12_000,
                    "output_tokens": 10_000,
                    "output_tokens_details": {"reasoning_tokens": 2_000},
                },
            },
        ),
    )

    with pytest.raises(OpenAIProviderError) as caught:
        await OpenAI(settings()).summarize("meeting-1", "Planning", "Hello")

    assert caught.value.code == "OPENAI_RESPONSE_INCOMPLETE_MAX_OUTPUT_TOKENS"
    assert "error_code=OPENAI_RESPONSE_INCOMPLETE_MAX_OUTPUT_TOKENS" in caplog.text
    assert "response_status=incomplete" in caplog.text
    assert "input_tokens=12000 output_tokens=10000 reasoning_tokens=2000" in caplog.text
    assert "private partial text" not in caplog.text


@pytest.mark.parametrize(
    ("response", "code"),
    [
        (
            {
                "status": "completed",
                "output": [{"type": "message", "content": [{"type": "refusal", "refusal": "No"}]}],
            },
            "OPENAI_RESPONSE_REFUSED",
        ),
        ({"status": "completed", "output": []}, "OPENAI_RESPONSE_EMPTY"),
        (
            {"status": "completed", "output_text": '{"actionItems":["unfinished"'},
            "OPENAI_RESPONSE_INVALID_JSON",
        ),
    ],
)
async def test_openai_classifies_other_unusable_responses(monkeypatch, response, code):
    enable_openai(monkeypatch)
    mock_client(monkeypatch, lambda request: httpx.Response(200, json=response))

    with pytest.raises(OpenAIProviderError) as caught:
        await OpenAI(settings()).summarize("meeting-1", "Planning", "Hello")

    assert caught.value.code == code


@pytest.mark.parametrize("status,message", [(401, "rejected"), (429, "rate-limited")])
async def test_openai_surfaces_safe_http_errors(monkeypatch, status, message):
    enable_openai(monkeypatch)
    mock_client(monkeypatch, lambda request: httpx.Response(status))
    with pytest.raises(OpenAIProviderError, match=message) as caught:
        await OpenAI(settings()).summarize("meeting-1", "Planning", "Hello")
    assert caught.value.code == (
        "OPENAI_HTTP_AUTH_REJECTED" if status == 401 else "OPENAI_HTTP_RATE_LIMITED"
    )
