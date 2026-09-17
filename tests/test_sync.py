import json

import pytest

from app.config import settings
from app.insights import process_insight
from app.models import InsightEvent, MeetingSync, UserSync, parse_event
from app.sync import discover_meetings, recover_from_link, sync_meeting
from tests.conftest import USER


async def test_graph_list_id_can_differ_from_detail_id(store, graph, samples):
    graph.request.side_effect = [samples["meeting"], samples["insight"]]
    event = InsightEvent(user_id=USER, meeting_id="m", insight_id="listing-alias")
    assert await process_insight(event, graph, store) == "SAVED"
    graph.list.side_effect = [[], [{"id": "listing-alias"}]]
    await sync_meeting(MeetingSync(user_id=USER, meeting_id="m"), graph, store)
    with store.connect() as db:
        payloads = [json.loads(row[0]) for row in db.execute("SELECT payload FROM jobs")]
    assert not any("insight_id" in item for item in payloads)


async def test_missing_transcript_is_queued_without_duplicate_insight(store, graph):
    store.save_meeting(USER, "Meeting", {"meeting_id": "m", "insight": {"id": "i"}})
    graph.list.side_effect = [[{"id": "t"}], [{"id": "i"}]]
    assert await sync_meeting(MeetingSync(user_id=USER, meeting_id="m"), graph, store) == "SYNCED"
    assert json.loads(store.next_job()["payload"])["transcript_id"] == "t"
    with store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1


async def test_transcript_failure_does_not_block_insight_recovery(store, graph):
    store.save_meeting(USER, "Meeting", {"meeting_id": "m"})
    graph.list.side_effect = [RuntimeError("unavailable"), [{"id": "i"}]]
    with pytest.raises(RuntimeError, match="Retry"):
        await sync_meeting(MeetingSync(user_id=USER, meeting_id="m"), graph, store)
    assert json.loads(store.next_job()["payload"])["insight_id"] == "i"


async def test_openrouter_provider_skips_copilot_insight_sync(monkeypatch, store, graph):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    monkeypatch.setenv("OPENROUTER_MODEL", "test/model")
    monkeypatch.setenv("AI_PROVIDER", "openrouter")
    settings.cache_clear()
    store.save_meeting(USER, "Meeting", {"meeting_id": "m"})
    graph.list.return_value = [{"id": "t"}]
    assert await sync_meeting(MeetingSync(user_id=USER, meeting_id="m"), graph, store) == "SYNCED"
    assert graph.list.await_count == 1
    assert json.loads(store.next_job()["payload"])["transcript_id"] == "t"


async def test_unknown_meeting_never_queries_graph(store, graph):
    assert (
        await sync_meeting(MeetingSync(user_id=USER, meeting_id="other"), graph, store)
        == "SKIPPED_UNKNOWN_MEETING"
    )
    graph.list.assert_not_called()


def test_refresh_requires_auth_and_queues_only_own_meetings(client, store, signed_in):
    other = "99999999-9999-9999-9999-999999999999"
    store.enroll(other, "Other")
    store.save_meeting(other, "Other", {"meeting_id": "private"})
    store.save_meeting(USER, "Mine", {"meeting_id": "mine"})
    assert client.post("/api/sync", json={}).status_code == 401
    assert client.post("/api/sync", headers=signed_in, json={}).json() == {"queued": 2}
    with store.connect() as db:
        rows = db.execute("SELECT payload FROM jobs").fetchall()
    assert len(rows) == 2
    assert json.loads(rows[0][0])["meeting_id"] == "mine"
    assert isinstance(parse_event(rows[1][0]), UserSync)


async def test_recovery_uses_the_user_meeting_link(store, graph):
    graph.list.return_value = [{"id": "meeting", "subject": "Recovered"}]
    result = await recover_from_link(
        store, graph, USER, "https://teams.microsoft.com/meet/123456789"
    )
    assert result == {"found": 1, "queued": 1}
    assert store.meetings(USER)[0]["subject"] == "Recovered"


def test_refresh_discovers_when_no_meetings_are_saved(client, store, signed_in):
    assert client.post("/api/sync", headers=signed_in, json={}).json() == {"queued": 1}
    assert isinstance(parse_event(store.next_job()["payload"]), UserSync)
    assert client.app.state.repair.is_set()


async def test_discovery_recovers_unknown_meeting_and_deduplicates(store, graph):
    graph.list.return_value = [{"id": "t", "meetingId": "new-meeting"}]
    event = UserSync(user_id=USER)
    assert await discover_meetings(event, graph, store) == "DISCOVERED"
    assert await discover_meetings(event, graph, store) == "DISCOVERED"
    job = parse_event(store.next_job()["payload"])
    assert job.meeting_id == "new-meeting"
    assert job.transcript_id == "t"
    path = graph.list.call_args.args[0]
    assert f"meetingOrganizerUserId='{USER}'" in path
    assert "startDateTime=" in path and "endDateTime=" in path
    with store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1


async def test_discovery_skips_saved_and_other_organizers(store, graph):
    store.save_meeting(USER, "Saved", {"meeting_id": "m", "transcript": {"id": "t"}})
    graph.list.return_value = [
        {"id": "t", "meetingId": "m"},
        {"id": "other", "meetingId": "private", "meetingOrganizer": {"user": {"id": "other"}}},
    ]
    await discover_meetings(UserSync(user_id=USER), graph, store)
    assert store.next_job() is None


async def test_discovery_does_not_query_for_unenrolled_user(store, graph):
    event = UserSync(user_id="99999999-9999-9999-9999-999999999999")
    assert await discover_meetings(event, graph, store) == "SKIPPED_NOT_ENROLLED"
    graph.list.assert_not_called()
