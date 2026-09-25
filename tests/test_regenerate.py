from app.config import settings
from app.models import Insight
from app.openai import OpenAIProviderError
from tests.conftest import USER


def enable_openrouter(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    monkeypatch.setenv("OPENROUTER_MODEL", "test/model")
    settings.cache_clear()


def seed_meeting_with_transcript(store, text="Hello transcript"):
    local_id = store.save_transcript(USER, "m", "t", text)
    store.save_meeting(
        USER,
        "Meeting",
        {
            "meeting_id": "m",
            "transcript": {
                "id": "t",
                "local_id": local_id,
                "createdDateTime": None,
                "contentCorrelationId": None,
            },
        },
    )
    return store.meetings(USER)[0]["id"]


class FakeOpenRouter:
    def __init__(self, config):
        pass

    async def summarize(self, transcript_id, subject, text):
        assert text == "Hello transcript"
        return Insight(
            id=f"openrouter:{transcript_id}",
            meetingNotes=[{"text": "Regenerated note."}],
            actionItems=[{"text": "Follow up", "ownerDisplayName": "Ada"}],
        )


def test_regenerate_replaces_openrouter_insight(monkeypatch, client, store, signed_in):
    enable_openrouter(monkeypatch)
    monkeypatch.setattr("app.transcripts.OpenRouter", FakeOpenRouter)
    meeting_id = seed_meeting_with_transcript(store)

    response = client.post(f"/api/meetings/{meeting_id}/regenerate", headers=signed_in, json={})
    assert response.status_code == 200
    result = response.json()
    assert result.keys() == {"status", "meeting_id", "occurrence_id", "provider", "insights"}
    assert result["status"] == "saved"
    assert result["meeting_id"] == meeting_id
    assert result["occurrence_id"] is None
    assert result["provider"] == "openrouter"
    insights = result["insights"]
    assert len(insights) == 1
    assert insights[0].keys() == {"insight"}
    # Keyed by meeting, not transcript -- see test_transcripts.py.
    assert insights[0]["insight"]["id"] == "openrouter:m"
    assert insights[0]["insight"]["provider"] == "openrouter"


def test_regenerate_can_select_openrouter_when_openai_is_preferred(
    monkeypatch, client, store, signed_in
):
    enable_openrouter(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-enterprise-test")
    settings.cache_clear()
    monkeypatch.setattr("app.transcripts.OpenRouter", FakeOpenRouter)
    meeting_id = seed_meeting_with_transcript(store)

    response = client.post(
        f"/api/meetings/{meeting_id}/regenerate",
        headers=signed_in,
        json={"provider": "openrouter"},
    )
    assert response.status_code == 200
    assert response.json()["insights"][0]["insight"]["provider"] == "openrouter"


def test_regenerate_requires_an_external_summary_service(client, store, signed_in):
    meeting_id = seed_meeting_with_transcript(store)
    response = client.post(f"/api/meetings/{meeting_id}/regenerate", headers=signed_in, json={})
    assert response.status_code == 409
    assert "AI provider" in response.json()["detail"]


def test_regenerate_works_under_the_default_copilot_provider(monkeypatch, client, store, signed_in):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    monkeypatch.setenv("OPENROUTER_MODEL", "test/model")
    settings.cache_clear()
    monkeypatch.setattr("app.transcripts.OpenRouter", FakeOpenRouter)
    meeting_id = seed_meeting_with_transcript(store)

    response = client.post(f"/api/meetings/{meeting_id}/regenerate", headers=signed_in, json={})
    assert response.status_code == 200
    assert response.json()["insights"][0]["insight"]["provider"] == "openrouter"


def test_regenerate_requires_a_transcript(monkeypatch, client, store, signed_in):
    enable_openrouter(monkeypatch)
    store.save_meeting(USER, "Meeting", {"meeting_id": "m"})
    meeting_id = store.meetings(USER)[0]["id"]
    response = client.post(f"/api/meetings/{meeting_id}/regenerate", headers=signed_in, json={})
    assert response.status_code == 409
    assert "transcript" in response.json()["detail"].lower()


def test_regenerate_rejects_unknown_meeting(monkeypatch, client, store, signed_in):
    enable_openrouter(monkeypatch)
    response = client.post("/api/meetings/999/regenerate", headers=signed_in, json={})
    assert response.status_code == 404


def test_regenerate_surfaces_openrouter_failure(monkeypatch, client, store, signed_in):
    enable_openrouter(monkeypatch)

    class FailingOpenRouter:
        def __init__(self, config):
            pass

        async def summarize(self, transcript_id, subject, text):
            raise ValueError("OpenRouter rejected this API key.")

    monkeypatch.setattr("app.transcripts.OpenRouter", FailingOpenRouter)
    meeting_id = seed_meeting_with_transcript(store)

    response = client.post(f"/api/meetings/{meeting_id}/regenerate", headers=signed_in, json={})
    assert response.status_code == 502


def test_regenerate_surfaces_the_openai_error_code(monkeypatch, client, store, signed_in):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-enterprise-test")
    settings.cache_clear()

    class IncompleteOpenAI:
        def __init__(self, config):
            pass

        async def summarize(self, transcript_id, subject, text):
            raise OpenAIProviderError(
                "OPENAI_RESPONSE_INCOMPLETE_MAX_OUTPUT_TOKENS",
                "OpenAI returned an incomplete response.",
            )

    monkeypatch.setattr("app.transcripts.OpenAI", IncompleteOpenAI)
    meeting_id = seed_meeting_with_transcript(store)

    response = client.post(
        f"/api/meetings/{meeting_id}/regenerate",
        headers=signed_in,
        json={"provider": "openai"},
    )

    assert response.status_code == 502
    assert response.json()["detail"].endswith(
        "Error code: OPENAI_RESPONSE_INCOMPLETE_MAX_OUTPUT_TOKENS."
    )
