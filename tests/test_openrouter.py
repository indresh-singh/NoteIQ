import json

import httpx
import pytest

from app.config import settings
from app.openrouter import FREE_ROUTER_MODEL, OpenRouter, extract_json_object


def mock_client(monkeypatch, handle):
    original = httpx.AsyncClient
    monkeypatch.setattr(
        "app.openrouter.httpx.AsyncClient",
        lambda **kwargs: original(transport=httpx.MockTransport(handle), **kwargs),
    )


def enable_openrouter(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    monkeypatch.setenv("OPENROUTER_MODEL", "test/model")
    settings.cache_clear()


async def test_summarize_parses_model_reply(monkeypatch):
    enable_openrouter(monkeypatch)

    def handle(request):
        assert request.headers["Authorization"] == "Bearer sk-or-test"
        assert request.headers["HTTP-Referer"] == settings().public_url
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": (
                                '{"meetingNotes": [{"title": "Budget", "text": "Agreed on scope."}], '
                                '"actionItems": [{"title": "Follow up", "ownerDisplayName": "Ada"}]}'
                            )
                        }
                    }
                ]
            },
        )

    mock_client(monkeypatch, handle)
    insight = await OpenRouter(settings()).summarize("transcript-1", "Budget review", "hello")
    assert insight.id == "openrouter:transcript-1"
    assert insight.meetingNotes[0].text == "Agreed on scope."
    assert insight.actionItems[0].ownerDisplayName == "Ada"


async def test_summarize_keeps_due_date_when_the_model_provides_one(monkeypatch):
    enable_openrouter(monkeypatch)

    def handle(request):
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": (
                                '{"meetingNotes": [], "actionItems": ['
                                '{"text": "Send proposal", "ownerDisplayName": "Ada", '
                                '"dueDate": "2026-09-20"}]}'
                            )
                        }
                    }
                ]
            },
        )

    mock_client(monkeypatch, handle)
    insight = await OpenRouter(settings()).summarize("transcript-1", "Budget review", "hello")
    assert insight.actionItems[0].dueDate == "2026-09-20"


async def test_summarize_leaves_due_date_unset_when_the_model_omits_it(monkeypatch):
    enable_openrouter(monkeypatch)

    def handle(request):
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": (
                                '{"meetingNotes": [], "actionItems": ['
                                '{"text": "Send proposal", "ownerDisplayName": "Ada"}]}'
                            )
                        }
                    }
                ]
            },
        )

    mock_client(monkeypatch, handle)
    insight = await OpenRouter(settings()).summarize("transcript-1", "Budget review", "hello")
    assert insight.actionItems[0].dueDate is None


async def test_summarize_rejects_non_json_reply(monkeypatch):
    enable_openrouter(monkeypatch)

    def handle(request):
        return httpx.Response(200, json={"choices": [{"message": {"content": "not json"}}]})

    mock_client(monkeypatch, handle)
    with pytest.raises(ValueError, match="usable summary"):
        await OpenRouter(settings()).summarize("transcript-1", "Budget review", "hello")


def test_extract_json_object_handles_markdown_fences():
    content = '```json\n{"meetingNotes": [], "actionItems": []}\n```'
    assert extract_json_object(content) == {"meetingNotes": [], "actionItems": []}


def test_extract_json_object_handles_bare_fences():
    content = '```\n{"meetingNotes": [], "actionItems": []}\n```'
    assert extract_json_object(content) == {"meetingNotes": [], "actionItems": []}


def test_extract_json_object_handles_stray_commentary():
    content = 'Sure, here is the summary:\n{"meetingNotes": [], "actionItems": []}\nLet me know!'
    assert extract_json_object(content) == {"meetingNotes": [], "actionItems": []}


def test_extract_json_object_raises_when_no_json_present():
    with pytest.raises(ValueError, match="No JSON object"):
        extract_json_object("I couldn't find any action items.")


async def test_summarize_parses_reply_wrapped_in_markdown_fences(monkeypatch):
    enable_openrouter(monkeypatch)

    def handle(request):
        content = (
            "```json\n"
            '{"meetingNotes": [{"text": "Agreed on scope."}], '
            '"actionItems": [{"text": "Send proposal", "ownerDisplayName": "Ada"}]}\n'
            "```"
        )
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    mock_client(monkeypatch, handle)
    insight = await OpenRouter(settings()).summarize("transcript-1", "Budget review", "hello")
    assert insight.meetingNotes[0].text == "Agreed on scope."
    assert insight.actionItems[0].ownerDisplayName == "Ada"


async def test_summarize_does_not_request_structured_output_mode(monkeypatch):
    enable_openrouter(monkeypatch)
    payloads = []

    def handle(request):
        payloads.append(json.loads(request.read()))
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": '{"meetingNotes": [], "actionItems": []}'}}]},
        )

    mock_client(monkeypatch, handle)
    await OpenRouter(settings()).summarize("transcript-1", "Budget review", "hello")
    assert "response_format" not in payloads[0]


async def test_summarize_asks_the_model_to_disable_reasoning(monkeypatch):
    enable_openrouter(monkeypatch)
    payloads = []

    def handle(request):
        payloads.append(json.loads(request.read()))
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": '{"meetingNotes": [], "actionItems": []}'}}]},
        )

    mock_client(monkeypatch, handle)
    await OpenRouter(settings()).summarize("transcript-1", "Budget review", "hello")
    assert payloads[0]["reasoning"] == {"enabled": False}


async def test_summarize_falls_back_to_reasoning_field_when_content_is_empty(monkeypatch):
    enable_openrouter(monkeypatch)

    def handle(request):
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": "",
                            "reasoning": '{"meetingNotes": [{"text": "From reasoning field."}], "actionItems": []}',
                        }
                    }
                ]
            },
        )

    mock_client(monkeypatch, handle)
    insight = await OpenRouter(settings()).summarize("transcript-1", "Budget review", "hello")
    assert insight.meetingNotes[0].text == "From reasoning field."


async def test_summarize_falls_back_to_reasoning_content_field_when_content_is_missing(
    monkeypatch,
):
    enable_openrouter(monkeypatch)

    def handle(request):
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": None,
                            "reasoning_content": (
                                '{"meetingNotes": [], "actionItems": '
                                '[{"text": "Follow up", "ownerDisplayName": "Ada"}]}'
                            ),
                        }
                    }
                ]
            },
        )

    mock_client(monkeypatch, handle)
    insight = await OpenRouter(settings()).summarize("transcript-1", "Budget review", "hello")
    assert insight.actionItems[0].ownerDisplayName == "Ada"


@pytest.mark.parametrize(
    "code,message", [(401, "rejected"), (429, "rate-limited"), (500, "could not complete")]
)
async def test_summarize_surfaces_http_errors(monkeypatch, code, message):
    enable_openrouter(monkeypatch)

    def handle(request):
        return httpx.Response(code)

    mock_client(monkeypatch, handle)
    with pytest.raises(ValueError, match=message):
        await OpenRouter(settings()).summarize("transcript-1", "Budget review", "hello")


async def test_summarize_wraps_transport_errors(monkeypatch):
    enable_openrouter(monkeypatch)

    def handle(request):
        raise httpx.ConnectError("offline")

    mock_client(monkeypatch, handle)
    with pytest.raises(ValueError, match="Unable to reach OpenRouter"):
        await OpenRouter(settings()).summarize("transcript-1", "Budget review", "hello")


async def test_summarize_falls_back_when_primary_model_is_rate_limited(monkeypatch):
    enable_openrouter(monkeypatch)
    requested_models = []

    def handle(request):
        model = json.loads(request.read())["model"]
        requested_models.append(model)
        if model == "test/model":
            return httpx.Response(429)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": '{"meetingNotes": [{"text": "From fallback."}], "actionItems": []}'
                        }
                    }
                ]
            },
        )

    mock_client(monkeypatch, handle)
    insight = await OpenRouter(settings()).summarize("transcript-1", "Budget review", "hello")
    assert requested_models == ["test/model", FREE_ROUTER_MODEL]
    assert insight.meetingNotes[0].text == "From fallback."


async def test_summarize_does_not_fall_back_on_rejected_api_key(monkeypatch):
    enable_openrouter(monkeypatch)
    requested_models = []

    def handle(request):
        requested_models.append(json.loads(request.read())["model"])
        return httpx.Response(401)

    mock_client(monkeypatch, handle)
    with pytest.raises(ValueError, match="rejected"):
        await OpenRouter(settings()).summarize("transcript-1", "Budget review", "hello")
    assert requested_models == ["test/model"]


async def test_summarize_raises_final_error_when_the_whole_chain_fails(monkeypatch):
    enable_openrouter(monkeypatch)

    def handle(request):
        return httpx.Response(429)

    mock_client(monkeypatch, handle)
    with pytest.raises(ValueError, match="rate-limited"):
        await OpenRouter(settings()).summarize("transcript-1", "Budget review", "hello")


async def test_summarize_does_not_duplicate_the_free_router_when_configured(
    monkeypatch,
):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    monkeypatch.setenv("OPENROUTER_MODEL", FREE_ROUTER_MODEL)
    settings.cache_clear()
    requested_models = []

    def handle(request):
        requested_models.append(json.loads(request.read())["model"])
        return httpx.Response(429)

    mock_client(monkeypatch, handle)
    with pytest.raises(ValueError, match="rate-limited"):
        await OpenRouter(settings()).summarize("transcript-1", "Budget review", "hello")
    assert requested_models == [FREE_ROUTER_MODEL]
