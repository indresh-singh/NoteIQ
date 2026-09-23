import pytest

from app.config import settings
from app.models import Insight
from app.sync import queue_sync
from tests.conftest import USER


def enable(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setenv("OPENROUTER_MODEL", "test/model")
    settings.cache_clear()


def enable_openai_and_openrouter(monkeypatch):
    enable(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "test-openai-key")
    settings.cache_clear()


class FakeRouter:
    def __init__(self, config):
        pass

    async def summarize(self, transcript_id, subject, text):
        assert text == "Ada: I will follow up."
        return Insight(
            id=transcript_id,
            meetingNotes=[{"text": "Discussion"}],
            actionItems=[{"text": "Follow up", "ownerDisplayName": "Ada"}],
        )


@pytest.mark.parametrize("filename", ["notes.txt", "notes.vtt", "notes.srt", "teams.docx"])
def test_upload_saved_and_excluded_from_graph(monkeypatch, client, store, signed_in, filename):
    enable(monkeypatch)
    monkeypatch.setattr("app.web.OpenRouter", FakeRouter)
    response = client.post(
        "/api/transcripts/upload",
        headers=signed_in,
        json={
            "filename": filename,
            "subject": "Planning",
            "text": "Ada: I will follow up.",
        },
    )
    assert response.status_code == 200
    meeting = store.meetings(USER)[0]
    assert meeting["content"]["source"] == "upload"
    assert meeting["content"]["insight"]["provider"] == "openrouter"
    local_id = meeting["content"]["transcripts"][0]["transcript"]["local_id"]
    assert store.transcript(USER, local_id) == "Ada: I will follow up."
    assert store.transcript("another-user", local_id) is None
    assert queue_sync(store, USER) == 0


def test_upload_requires_auth(client):
    assert client.post("/api/transcripts/upload", json={}).status_code == 401


def test_docx_upload_accepts_60000_extracted_characters(monkeypatch, client, signed_in):
    enable(monkeypatch)

    class LimitRouter:
        def __init__(self, config):
            pass

        async def summarize(self, transcript_id, subject, text):
            assert len(text) == 60_000
            return Insight(
                id=transcript_id,
                meetingNotes=[{"text": "Discussion"}],
                actionItems=[],
            )

    monkeypatch.setattr("app.web.OpenRouter", LimitRouter)
    response = client.post(
        "/api/transcripts/upload",
        headers=signed_in,
        json={"filename": "teams.docx", "subject": "Planning", "text": "x" * 60_000},
    )

    assert response.status_code == 200


def test_new_upload_replaces_previous_but_preserves_teams(monkeypatch, client, store, signed_in):
    enable(monkeypatch)
    monkeypatch.setattr("app.web.OpenRouter", FakeRouter)
    store.save_meeting(USER, "Teams meeting", {"meeting_id": "teams-meeting"})
    body = {"filename": "notes.txt", "subject": "First", "text": "Ada: I will follow up."}
    assert client.post("/api/transcripts/upload", headers=signed_in, json=body).status_code == 200
    first = store.meetings(USER)[0]
    transcript_id = first["content"]["transcript"]["local_id"]
    body["subject"] = "Latest"
    assert client.post("/api/transcripts/upload", headers=signed_in, json=body).status_code == 200
    assert {m["subject"] for m in store.meetings(USER)} == {"Latest", "Teams meeting"}
    assert store.transcript(USER, transcript_id) is None


def test_upload_invalid_input(monkeypatch, client, signed_in):
    enable(monkeypatch)
    for filename, text, status in [
        ("notes.pdf", "hello", 400),
        ("notes.txt", " ", 400),
        ("notes.txt", "x" * 60001, 422),
    ]:
        response = client.post(
            "/api/transcripts/upload",
            headers=signed_in,
            json={"filename": filename, "subject": "Meeting", "text": text},
        )
        assert response.status_code == status


def test_upload_provider_failure_not_saved(monkeypatch, client, store, signed_in):
    enable(monkeypatch)

    class FailingRouter(FakeRouter):
        async def summarize(self, *args):
            raise ValueError("Unable to reach OpenRouter.")

    monkeypatch.setattr("app.web.OpenRouter", FailingRouter)
    response = client.post(
        "/api/transcripts/upload",
        headers=signed_in,
        json={"filename": "notes.txt", "subject": "Meeting", "text": "hello"},
    )
    assert response.status_code == 502
    assert store.meetings(USER) == []


def test_upload_falls_back_to_openrouter_when_openai_fails(
    monkeypatch, client, store, signed_in
):
    enable_openai_and_openrouter(monkeypatch)

    class FailingOpenAI:
        def __init__(self, config):
            pass

        async def summarize(self, *args):
            raise ValueError("OpenAI rejected this API key.")

    monkeypatch.setattr("app.web.OpenAI", FailingOpenAI)
    monkeypatch.setattr("app.web.OpenRouter", FakeRouter)
    response = client.post(
        "/api/transcripts/upload",
        headers=signed_in,
        json={
            "filename": "notes.txt",
            "subject": "Planning",
            "text": "Ada: I will follow up.",
        },
    )

    assert response.status_code == 200
    meeting = store.meetings(USER)[0]
    assert meeting["content"]["insight"]["provider"] == "openrouter"
