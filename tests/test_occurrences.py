from unittest.mock import AsyncMock

import pytest

from app.config import settings
from app.models import Insight, MeetingSync, SessionInsightEvent, TranscriptEvent, parse_event
from app.occurrences import present_meeting, select_session, sessions, transcript_version
from app.sync import sync_meeting
from app.transcripts import (
    meeting_transcript_text,
    process_session_insight,
    process_transcript,
    queue_missing_session_insights,
)
from tests.conftest import USER


def seed(store, call, transcript, day, text=None):
    local_id = store.save_transcript(USER, "series", transcript, text or transcript)
    store.save_meeting(
        USER,
        "Daily Brief",
        {
            "meeting_id": "series",
            "meeting_metadata": {"meeting_type": "recurring"},
            "transcript": {
                "id": transcript,
                "local_id": local_id,
                "callId": call,
                "createdDateTime": f"2026-09-{day:02}T12:00:00Z",
                "contentCorrelationId": transcript,
            },
        },
    )
    return store.meetings(USER)[0]["id"]


def add_insight(store, call, provider="openai", text="Follow up"):
    store.save_meeting(
        USER,
        "Daily Brief",
        {
            "meeting_id": "series",
            "insight": {
                "id": f"{provider}:{call}",
                "provider": provider,
                "occurrence_id": f"call:{call}",
                "actionItems": [{"text": text}],
            },
        },
    )


def test_sessions_sort_and_combine_only_matching_call_ids(store):
    seed(store, "older", "a", 23)
    seed(store, "today", "c", 24)
    seed(store, "older", "b", 23)
    add_insight(store, "older")
    add_insight(store, "today")  # identical actions on different days must survive dedupe
    groups = sessions(store.find_meeting(USER, "series"))
    assert [g["id"] for g in groups] == ["call:today", "call:older"]
    assert [len(g["transcripts"]) for g in groups] == [1, 2]
    assert [len(g["insights"]) for g in groups] == [1, 1]


def test_legacy_combined_insights_are_not_assigned_and_copilot_uses_correlation(store):
    seed(store, "older", "a", 23)
    seed(store, "today", "b", 24)
    store.save_meeting(
        USER,
        "Daily Brief",
        {
            "meeting_id": "series",
            "insight": {
                "id": "legacy",
                "provider": "openai",
                "actionItems": [{"text": "Mixed"}],
            },
        },
    )
    store.save_meeting(
        USER,
        "Daily Brief",
        {
            "meeting_id": "series",
            "insight": {
                "id": "copilot",
                "provider": "copilot",
                "contentCorrelationId": "a",
                "actionItems": [{"text": "Older task"}],
            },
        },
    )
    content = present_meeting(store.meetings(USER)[0])["content"]
    assert content["unassigned_insights"] == 1
    assert content["occurrences"][0]["insights"] == []
    assert content["occurrences"][1]["insights"][0]["insight"]["id"] == "copilot"


def test_missing_call_ids_are_never_grouped_by_date(store):
    seed(store, None, "a", 24)
    seed(store, None, "b", 24)
    content = store.find_meeting(USER, "series")
    assert len(sessions(content)) == 2
    with pytest.raises(ValueError, match="Select"):
        select_session(content, None)
    with pytest.raises(KeyError):
        select_session(content, "call:someone-else")


def test_same_call_restarted_transcripts_restore_whole_call_insights(store):
    row = seed(store, "actual-call", "first", 24, "Before transcription paused")
    seed(store, "actual-call", "second", 24, "After transcription resumed")
    store.save_meeting(
        USER,
        "Catchup",
        {
            "meeting_id": "series",
            "meeting_metadata": {"meeting_type": "scheduled"},
            "insight": {
                "id": "legacy",
                "provider": "openai",
                "meetingNotes": [{"text": "Whole meeting"}],
            },
        },
    )
    presented = present_meeting(store.meeting(USER, row))["content"]
    assert len(presented["occurrences"]) == 1
    assert presented["unassigned_insights"] == 0
    assert not presented["occurrences"][0]["metadata_pending"]
    selected = select_session(store.find_meeting(USER, "series"), "call:actual-call")
    assert meeting_transcript_text(store, USER, selected) == (
        "Before transcription paused\n\nAfter transcription resumed"
    )
    assert selected["insights"][0]["insight"]["id"] == "legacy"
    store.save_meeting(
        USER,
        "Catchup",
        {
            "meeting_id": "series",
            "insight": {
                "id": "new",
                "provider": "openai",
                "occurrence_id": "call:actual-call",
                "meetingNotes": [{"text": "New complete summary"}],
            },
        },
    )
    presented = present_meeting(store.meeting(USER, row))["content"]
    assert presented["unassigned_insights"] == 0
    assert [e["insight"]["id"] for e in presented["occurrences"][0]["insights"]] == ["new"]


def test_duplicate_transcript_aliases_are_shown_and_fed_to_ai_once(store):
    local_id = store.save_transcript(USER, "one-off", "a", "One transcript")
    content = {
        "meeting_id": "one-off",
        "meeting_metadata": {"meeting_type": "scheduled"},
        "transcripts": [
            {
                "transcript": {
                    "id": alias,
                    "local_id": local_id,
                    "contentCorrelationId": "same-recording",
                    "createdDateTime": "2026-09-23T20:48:28Z",
                }
            }
            for alias in ("list-id", "detail-id")
        ],
        "insight": {"id": "legacy", "provider": "openai", "meetingNotes": [{"text": "Summary"}]},
    }
    presented = present_meeting({"content": content})["content"]
    assert len(presented["transcripts"]) == 1
    assert meeting_transcript_text(store, USER, content) == "One transcript"
    assert presented["insights"][0]["insight"]["id"] == "legacy"
    assert len(content["transcripts"]) == 2  # projection does not delete historical data


def test_distinct_call_ids_are_not_combined_regardless_of_invite_type(store):
    seed(store, "first-call", "a", 24)
    seed(store, "second-call", "b", 24)
    content = store.find_meeting(USER, "series")
    content["meeting_metadata"]["meeting_type"] = "scheduled"
    assert len(sessions(content)) == 2


def test_known_single_call_summary_survives_non_recurring_grouping(store):
    seed(store, "one-call", "a", 24)
    seed(store, "one-call", "b", 24)
    add_insight(store, "one-call")
    content = store.find_meeting(USER, "series")
    content["meeting_metadata"]["meeting_type"] = "scheduled"
    groups = sessions(content)
    assert groups[0]["id"] == "call:one-call"
    assert len(groups[0]["insights"]) == 1


@pytest.mark.parametrize("provider", ["openai", "openrouter"])
async def test_same_call_transcript_restart_summarizes_both_parts(
    monkeypatch, store, samples, provider
):
    monkeypatch.setenv(f"{provider.upper()}_API_KEY", "test")
    if provider == "openrouter":
        monkeypatch.setenv("OPENROUTER_MODEL", "test/model")
    settings.cache_clear()
    seed(store, "actual-call", "first", 24, "Before pause")
    seen = []

    class AI:
        def __init__(self, config):
            pass

        async def summarize(self, key, subject, text):
            seen.append((key, text))
            return Insight(id=f"{provider}:{key}", meetingNotes=[{"text": "Combined"}])

    monkeypatch.setattr(
        "app.transcripts." + ("OpenAI" if provider == "openai" else "OpenRouter"), AI
    )
    graph = AsyncMock()
    graph.request.side_effect = [
        {**samples["meeting"], "meetingType": "scheduled"},
        {"id": "second", "callId": "actual-call", "createdDateTime": "2026-09-24T12:10:00Z"},
        "After resume",
    ]
    await process_transcript(
        TranscriptEvent(user_id=USER, meeting_id="series", transcript_id="second"), graph, store
    )
    assert seen == [("series:call:actual-call", "Before pause\n\nAfter resume")]
    group = sessions(store.find_meeting(USER, "series"))[0]
    assert len(group["insights"]) == 1


@pytest.mark.parametrize("provider", ["openai", "openrouter"])
async def test_ingestion_only_sends_current_call_to_ai(monkeypatch, store, samples, provider):
    monkeypatch.setenv(f"{provider.upper()}_API_KEY", "test")
    if provider == "openrouter":
        monkeypatch.setenv("OPENROUTER_MODEL", "test/model")
    settings.cache_clear()
    seed(store, "yesterday", "a", 23, "Yesterday private topic")
    seed(store, "today", "b", 24, "First segment")
    seen = []

    class AI:
        def __init__(self, config):
            pass

        async def summarize(self, key, subject, text):
            seen.append((key, text))
            return Insight(id=f"{provider}:{key}", meetingNotes=[{"text": "Current call"}])

    monkeypatch.setattr(
        "app.transcripts." + ("OpenAI" if provider == "openai" else "OpenRouter"), AI
    )
    graph = AsyncMock()
    graph.request.side_effect = [
        {**samples["meeting"], "meetingType": "recurring"},
        {"id": "c", "callId": "today", "createdDateTime": "2026-09-24T12:10:00Z"},
        "Second segment",
    ]
    assert (
        await process_transcript(
            TranscriptEvent(user_id=USER, meeting_id="series", transcript_id="c"), graph, store
        )
        == "TRANSCRIPT_SAVED"
    )
    assert seen == [("series:call:today", "First segment\n\nSecond segment")]
    groups = sessions(store.find_meeting(USER, "series"))
    assert len(groups[0]["insights"]) == 1
    assert not groups[1]["insights"]


@pytest.mark.parametrize("provider", ["openai", "openrouter"])
def test_regenerate_requires_session_and_preserves_other_sessions(
    monkeypatch, client, store, signed_in, provider
):
    monkeypatch.setenv(f"{provider.upper()}_API_KEY", "test")
    if provider == "openrouter":
        monkeypatch.setenv("OPENROUTER_MODEL", "test/model")
    settings.cache_clear()
    row = seed(store, "older", "a", 23)
    seed(store, "today", "b", 24)
    add_insight(store, "older", provider)

    class AI:
        def __init__(self, config):
            pass

        async def summarize(self, key, subject, text):
            assert text == "b"
            return Insight(id=f"{provider}:{key}", meetingNotes=[{"text": "Today"}])

    monkeypatch.setattr(
        "app.transcripts." + ("OpenAI" if provider == "openai" else "OpenRouter"), AI
    )
    url = f"/api/meetings/{row}/regenerate"
    assert client.post(url, headers=signed_in, json={"provider": provider}).status_code == 409
    assert (
        client.post(
            url, headers=signed_in, json={"provider": provider, "occurrence_id": "call:foreign"}
        ).status_code
        == 404
    )
    for _ in range(2):
        response = client.post(
            url, headers=signed_in, json={"provider": provider, "occurrence_id": "call:today"}
        )
        assert response.status_code == 200
        assert response.json()["occurrence_id"] == "call:today"
        assert response.json()["provider"] == provider
        assert len(response.json()["insights"]) == 1
    groups = sessions(store.find_meeting(USER, "series"))
    assert [len(g["insights"]) for g in groups] == [1, 1]
    assert groups[1]["insights"][0]["insight"]["actionItems"] == [{"text": "Follow up"}]


@pytest.mark.parametrize("destination", ["clickup", "planner"])
def test_exports_are_isolated_and_idempotent_per_session(client, store, signed_in, destination):
    if destination == "clickup":
        from tests.test_clickup import connect

        fake = connect(client, store, signed_in)
        client.post("/api/clickup/lists", headers=signed_in, json={"list_id": "123"})
    else:
        from tests.test_planner import FakePlanner, add_plan

        fake = FakePlanner()
        client.app.state.planner = fake
        add_plan(client, signed_in)
    row = seed(store, "older", "a", 23)
    seed(store, "today", "b", 24)
    add_insight(store, "older")
    add_insight(store, "today")
    url = f"/api/meetings/{row}/{destination}"
    assert client.post(url, headers=signed_in, json={}).status_code == 409
    assert (
        client.post(url, headers=signed_in, json={"occurrence_id": "call:foreign"}).status_code
        == 404
    )
    for call in ("older", "today"):
        body = {"provider": "openai", "occurrence_id": f"call:{call}"}
        assert client.post(url, headers=signed_in, json=body).json() == {"created": 1, "skipped": 0}
        assert client.post(url, headers=signed_in, json=body).json() == {"created": 0, "skipped": 1}
    assert len(fake.created) == 2
    assert "2026-09-23" in fake.created[0][-1]
    assert "2026-09-24" in fake.created[1][-1]


async def test_sync_recovers_legacy_metadata_once(store, graph):
    seed(store, None, "a", 24)
    graph.list.side_effect = [[{"id": "a"}], []]
    await sync_meeting(MeetingSync(user_id=USER, meeting_id="series"), graph, store)
    job = store.next_job()
    assert '"transcript_id":"a"' in job["payload"]
    assert parse_event(job["payload"]).metadata_only is True
    store.finish_job(job["id"], "done")
    saved = store.find_meeting(USER, "series")["transcript"]
    store.save_meeting(
        USER,
        "Daily Brief",
        {"meeting_id": "series", "transcript": {**saved, "session_metadata_checked": True}},
    )
    graph.list.side_effect = [[{"id": "a"}], []]
    await sync_meeting(MeetingSync(user_id=USER, meeting_id="series"), graph, store)
    assert store.next_job() is None


def test_complete_repair_queues_all_eleven_final_sessions_once(monkeypatch, store):
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    settings.cache_clear()
    # Twelve transcript artifacts represent eleven calls because transcription
    # was stopped and restarted during one of them.
    for number in range(12):
        call = "restart" if number in {0, 1} else f"call-{number}"
        local_id = store.save_transcript(USER, "series", f"t-{number}", f"part {number}")
        store.save_meeting(
            USER,
            "Daily Brief",
            {
                "meeting_id": "series",
                "transcript": {
                    "id": f"t-{number}",
                    "local_id": local_id,
                    "callId": call,
                    "session_metadata_checked": True,
                    "createdDateTime": f"2026-09-{number + 1:02}T10:00:00Z",
                },
            },
        )

    assert len(sessions(store.find_meeting(USER, "series"))) == 11
    assert queue_missing_session_insights(store, USER, "series") == 11
    events = []
    while job := store.claim_job():
        events.append(parse_event(job["payload"]))
    assert len(events) == 11
    assert all(isinstance(event, SessionInsightEvent) for event in events)
    assert {event.occurrence_id for event in events} == {
        "call:restart",
        *(f"call:call-{number}" for number in range(2, 12)),
    }


async def test_metadata_repair_reuses_text_then_generates_once_for_the_call(
    monkeypatch, store, samples
):
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    settings.cache_clear()
    organizer = samples["meeting"]["participants"]["organizer"]
    organizer["upn"] = "organizer@example.com"
    organizer["identity"]["user"]["displayName"] = "Organizer"
    for transcript_id, text in (("first", "Before pause"), ("second", "After resume")):
        local_id = store.save_transcript(USER, "series", transcript_id, text)
        store.save_meeting(
            USER,
            "Daily Brief",
            {
                "meeting_id": "series",
                "transcript": {"id": transcript_id, "local_id": local_id},
            },
        )

    graph = AsyncMock()
    graph.request.side_effect = [
        samples["meeting"],
        {"id": "first", "callId": "same-call", "createdDateTime": "2026-09-24T10:00:00Z"},
        samples["meeting"],
        {"id": "second", "callId": "same-call", "createdDateTime": "2026-09-24T10:10:00Z"},
    ]
    for transcript_id in ("first", "second"):
        result = await process_transcript(
            TranscriptEvent(
                user_id=USER,
                meeting_id="series",
                transcript_id=transcript_id,
                metadata_only=True,
            ),
            graph,
            store,
        )
        assert result == "SESSION_METADATA_REPAIRED"
    assert graph.request.await_count == 4
    assert all(not call.args[1].endswith("/content") for call in graph.request.await_args_list)
    assert store.find_meeting(USER, "series")["meeting_metadata"]["participants"] == [
        {"name": "Organizer", "email": "organizer@example.com", "organizer": True}
    ]

    job = store.claim_job()
    event = parse_event(job["payload"])
    assert isinstance(event, SessionInsightEvent)
    assert event.occurrence_id == "call:same-call"
    assert store.claim_job() is None
    seen = []

    class AI:
        def __init__(self, config):
            pass

        async def summarize(self, key, subject, text):
            seen.append((key, subject, text))
            return Insight(id="generated", meetingNotes=[{"text": "Whole call"}])

    monkeypatch.setattr("app.transcripts.OpenAI", AI)
    assert await process_session_insight(event, graph, store) == "SESSION_INSIGHT_SAVED"
    assert seen == [
        ("series:call:same-call", samples["meeting"]["subject"], "Before pause\n\nAfter resume")
    ]
    group = sessions(store.find_meeting(USER, "series"))[0]
    assert len(group["insights"]) == 1
    assert group["insights"][0]["insight"]["transcript_version"] == transcript_version(group)


def test_stale_generation_cannot_replace_complete_session_summary(store):
    seed(store, "today", "a", 24)
    before = select_session(store.find_meeting(USER, "series"), "call:today")
    seed(store, "today", "b", 24)
    add_insight(store, "today", text="Complete result")
    assert (
        store.save_meeting(
            USER,
            "Daily Brief",
            {
                "meeting_id": "series",
                "insight": {
                    "id": "openai:today",
                    "provider": "openai",
                    "occurrence_id": "call:today",
                    "transcript_version": transcript_version(before),
                    "actionItems": [{"text": "Incomplete result"}],
                },
            },
        )
        is False
    )
    insight = sessions(store.find_meeting(USER, "series"))[0]["insights"][0]["insight"]
    assert insight["actionItems"] == [{"text": "Complete result"}]


def test_concurrent_session_writes_preserve_every_transcript(store):
    from concurrent.futures import ThreadPoolExecutor

    def save(number):
        store.save_meeting(
            USER,
            "Daily Brief",
            {"meeting_id": "series", "transcript": {"id": str(number), "callId": str(number)}},
        )

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(save, range(12)))
    assert len(store.meetings(USER)) == 1
    assert len(sessions(store.find_meeting(USER, "series"))) == 12
