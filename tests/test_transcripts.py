import json
from unittest.mock import AsyncMock

import httpx
import pytest

from app.config import settings
from app.models import Insight, TranscriptEvent
from app.transcripts import process_transcript
from app.worker import run_job
from tests.conftest import USER


def event():
    return TranscriptEvent(user_id=USER, meeting_id="sample-meeting", transcript_id="transcript")


def enable_openrouter(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    monkeypatch.setenv("OPENROUTER_MODEL", "test/model")
    monkeypatch.setenv("AI_PROVIDER", "openrouter")
    settings.cache_clear()


@pytest.mark.parametrize(
    "resource",
    [
        f"users/{USER}/onlineMeetings('a%2Fb')/transcripts('c%2Bd')",
        f"/users/{USER}/onlineMeetings/a%2Fb/transcripts/c%2Bd",
    ],
)
def test_transcript_notification_formats(resource):
    parsed = TranscriptEvent.from_resource(resource)
    assert parsed.meeting_id == "a/b"
    assert parsed.transcript_path == f"/users/{USER}/onlineMeetings/a%2Fb/transcripts/c%2Bd"


async def test_webhook_transcript_content_and_private_view(
    client, store, samples, signed_in, graph
):
    samples["notification"]["value"][0]["resource"] = (
        f"users/{USER}/onlineMeetings('sample-meeting')/transcripts('transcript')"
    )
    graph.request.side_effect = [
        samples["meeting"],
        {"id": "transcript"},
        "WEBVTT\n<v Ada>Hello</v>",
    ]
    assert client.post("/api/graph/notifications", json=samples["notification"]).status_code == 202
    assert await run_job(store, graph)
    meetings = client.get("/api/meetings", headers=signed_in).json()
    transcript_id = meetings[0]["content"]["transcripts"][0]["transcript"]["local_id"]
    assert "WEBVTT" not in json.dumps(meetings)  # Load content only on demand.
    url = f"/api/transcripts/{transcript_id}"
    assert client.get(url, headers=signed_in).text == "WEBVTT\n<v Ada>Hello</v>"
    assert client.get(url).status_code == 401
    other = "22222222-2222-2222-2222-222222222222"
    store.enroll(other, "Other")
    assert (
        client.get(url, headers={"Authorization": "Bearer " + store.session(other)}).status_code
        == 404
    )
    store.disconnect(USER)
    assert store.transcript(USER, transcript_id) is None


@pytest.mark.parametrize("insight_first", [True, False])
def test_grouping_retains_both_artifacts_and_deduplicates(store, insight_first):
    insight = {"meeting_id": "meeting", "insight": {"id": "insight"}, "card": {"body": []}}
    transcript = {"meeting_id": "meeting", "transcript": {"id": "transcript", "local_id": 1}}
    content = [insight, transcript] if insight_first else [transcript, insight]
    for item in content * 2:
        store.save_meeting(USER, "Meeting", item)
    meetings = store.meetings(USER)
    assert len(meetings) == 1
    assert len(meetings[0]["content"]["insights"]) == 1
    assert len(meetings[0]["content"]["transcripts"]) == 1
    store.save_meeting(USER, "Meeting", {**insight, "insight": {"id": "second-segment"}})
    assert len(store.meetings(USER)[0]["content"]["insights"]) == 2


async def test_speaker_policy_fallback(store, samples):
    response = httpx.Response(
        403,
        request=httpx.Request("GET", "https://graph.microsoft.com"),
        json={"error": {"innerError": {"code": "SpeakerAttributionNotAllowed"}}},
    )
    graph = AsyncMock()
    graph.request.side_effect = [
        samples["meeting"],
        {"id": "transcript"},
        httpx.HTTPStatusError("denied", request=response.request, response=response),
        "00:01 Hello",
    ]
    assert await process_transcript(event(), graph, store) == "TRANSCRIPT_SAVED"
    assert (
        graph.request.call_args.kwargs["headers"]["Accept"]
        == "application/vnd.microsoft.graph.transcript+text"
    )


@pytest.mark.parametrize(
    "code,expected", [(403, "FAILED_PERMANENT"), (404, "retry"), (429, "retry")]
)
async def test_transcript_access_and_retry(store, samples, code, expected):
    response = httpx.Response(code, request=httpx.Request("GET", "https://graph.microsoft.com"))
    graph = AsyncMock()
    graph.request.side_effect = [
        samples["meeting"],
        {"id": "transcript"},
        httpx.HTTPStatusError("denied", request=response.request, response=response),
    ]
    if expected == "retry":
        with pytest.raises(RuntimeError, match="Retry transcript"):
            await process_transcript(event(), graph, store)
    else:
        assert await process_transcript(event(), graph, store) == expected
    assert store.meetings(USER) == []


async def test_transcript_attendee_is_skipped(store, samples):
    samples["meeting"]["participants"]["organizer"]["identity"]["user"]["id"] = "someone-else"
    graph = AsyncMock()
    graph.request.return_value = samples["meeting"]
    assert await process_transcript(event(), graph, store) == "SKIPPED_NOT_ORGANIZER"
    assert graph.request.await_count == 1


async def test_openrouter_provider_generates_insight_instead_of_copilot(
    monkeypatch, store, samples
):
    enable_openrouter(monkeypatch)

    class FakeOpenRouter:
        def __init__(self, config):
            assert config.openrouter_model == "test/model"

        async def summarize(self, transcript_id, subject, text):
            assert text == "WEBVTT\nHello"
            return Insight(
                id=f"openrouter:{transcript_id}",
                meetingNotes=[{"text": "Discussed budget."}],
                actionItems=[{"text": "Send proposal.", "ownerDisplayName": "Ada"}],
            )

    monkeypatch.setattr("app.transcripts.OpenRouter", FakeOpenRouter)
    graph = AsyncMock()
    graph.request.side_effect = [samples["meeting"], {"id": "transcript"}, "WEBVTT\nHello"]

    assert await process_transcript(event(), graph, store) == "TRANSCRIPT_SAVED"

    insights = store.meetings(USER)[0]["content"]["insights"]
    assert len(insights) == 1
    assert insights[0]["insight"]["id"] == "openrouter:transcript"
    assert insights[0]["insight"]["provider"] == "openrouter"
    assert "OpenRouter (test/model)" in json.dumps(insights[0]["card"])


async def test_openrouter_failure_does_not_fail_transcript_processing(monkeypatch, store, samples):
    enable_openrouter(monkeypatch)

    class FailingOpenRouter:
        def __init__(self, config):
            pass

        async def summarize(self, transcript_id, subject, text):
            raise ValueError("OpenRouter rejected this API key.")

    monkeypatch.setattr("app.transcripts.OpenRouter", FailingOpenRouter)
    graph = AsyncMock()
    graph.request.side_effect = [samples["meeting"], {"id": "transcript"}, "WEBVTT\nHello"]

    assert await process_transcript(event(), graph, store) == "TRANSCRIPT_SAVED"
    assert store.meetings(USER)[0]["content"].get("insights") is None
