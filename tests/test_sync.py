import json

import httpx
import pytest

from app.config import settings
from app.insights import process_insight
from app.models import InsightEvent, MeetingSync, UserSync, parse_event
from app.sync import (
    discover_meetings,
    queue_sync,
    recover_from_link,
    settled,
    sync_meeting,
    sync_now,
)
from tests.conftest import USER


async def test_graph_list_id_can_differ_from_detail_id(store, graph, samples):
    graph.request.side_effect = [samples["meeting"], samples["insight"], samples["meeting"]]
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
    with pytest.raises(RuntimeError, match="unavailable"):
        await sync_meeting(MeetingSync(user_id=USER, meeting_id="m"), graph, store)
    assert json.loads(store.next_job()["payload"])["insight_id"] == "i"


def graph_error(status: int, code: str = "Forbidden") -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "https://graph.microsoft.com/v1.0/test")
    response = httpx.Response(
        status,
        request=request,
        headers={"request-id": "graph-request", "client-request-id": "client-request"},
        json={"error": {"code": code, "innerError": {"code": "DetailedInnerCode"}}},
    )
    return httpx.HTTPStatusError("Graph rejected request", request=request, response=response)


async def test_sync_skips_non_organizer_and_persists_ui_message(store, graph, caplog):
    other = "99999999-9999-9999-9999-999999999999"
    store.save_meeting(USER, "Meeting", {"meeting_id": "m"})
    graph.request.return_value = {
        "id": "m",
        "participants": {"organizer": {"identity": {"user": {"id": other}}}},
        "creationDateTime": "2026-09-21T10:00:00Z",
        "startDateTime": "2026-09-21T11:00:00Z",
        "endDateTime": "2026-09-21T12:00:00Z",
        "meetingType": "scheduled",
    }

    assert await sync_meeting(MeetingSync(user_id=USER, meeting_id="m"), graph, store) == (
        "SKIPPED_NOT_ORGANIZER"
    )
    graph.list.assert_not_called()
    content = store.find_meeting(USER, "m")
    assert content["sync_status"] == "SKIPPED_NOT_ORGANIZER"
    assert "not its organizer" in content["sync_message"]
    assert queue_sync(store, USER) == 0
    assert f"requested_user_id={USER} organizer_id={other}" in caplog.text
    assert "reason=requested_user_is_not_organizer" in caplog.text


@pytest.mark.parametrize("status", [401, 403])
async def test_sync_access_denial_is_terminal_and_not_retried(store, graph, status):
    store.save_meeting(USER, "Meeting", {"meeting_id": "m"})
    graph.list.side_effect = [graph_error(status), []]

    result = await sync_meeting(MeetingSync(user_id=USER, meeting_id="m"), graph, store)

    assert result == "SKIPPED_ACCESS_DENIED"
    assert graph.list.await_count == 2  # The other artifact is still diagnosed/recovered.
    assert store.find_meeting(USER, "m")["sync_status"] == "SKIPPED_ACCESS_DENIED"
    assert queue_sync(store, USER) == 0


async def test_base_meeting_permanent_4xx_is_terminal(store, graph):
    store.save_meeting(USER, "Meeting", {"meeting_id": "m"})
    graph.request.side_effect = graph_error(404, "ItemNotFound")

    result = await sync_meeting(MeetingSync(user_id=USER, meeting_id="m"), graph, store)

    assert result == "SKIPPED_GRAPH_REJECTED"
    graph.list.assert_not_called()
    assert store.find_meeting(USER, "m")["sync_status"] == "SKIPPED_GRAPH_REJECTED"
    assert queue_sync(store, USER) == 0


async def test_base_meeting_transient_failure_is_retried(store, graph):
    store.save_meeting(USER, "Meeting", {"meeting_id": "m"})
    error = graph_error(503, "ServiceUnavailable")
    graph.request.side_effect = error

    with pytest.raises(httpx.HTTPStatusError) as raised:
        await sync_meeting(MeetingSync(user_id=USER, meeting_id="m"), graph, store)

    assert raised.value is error
    graph.list.assert_not_called()
    assert "sync_status" not in store.find_meeting(USER, "m")


@pytest.mark.parametrize("status", [429, 500, 503])
async def test_sync_transient_http_failures_keep_original_error_for_retry(
    store, graph, status
):
    store.save_meeting(USER, "Meeting", {"meeting_id": "m"})
    error = graph_error(status, "TooManyRequests" if status == 429 else "ServiceUnavailable")
    graph.list.side_effect = [error, []]

    with pytest.raises(httpx.HTTPStatusError) as raised:
        await sync_meeting(MeetingSync(user_id=USER, meeting_id="m"), graph, store)

    assert raised.value is error
    assert graph.list.await_count == 2
    assert "sync_status" not in store.find_meeting(USER, "m")


async def test_copilot_insight_sync_runs_with_external_summary_service(monkeypatch, store, graph):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    monkeypatch.setenv("OPENROUTER_MODEL", "test/model")
    settings.cache_clear()
    store.save_meeting(USER, "Meeting", {"meeting_id": "m"})
    graph.list.side_effect = [[{"id": "t"}], [{"id": "i"}]]
    assert await sync_meeting(MeetingSync(user_id=USER, meeting_id="m"), graph, store) == "SYNCED"
    assert graph.list.await_count == 2
    payloads = []
    while job := store.next_job():
        payloads.append(json.loads(job["payload"]))
        store.finish_job(job["id"], "done")
    assert any(item.get("transcript_id") == "t" for item in payloads)
    assert any(item.get("insight_id") == "i" for item in payloads)


async def test_unknown_meeting_never_queries_graph(store, graph):
    assert (
        await sync_meeting(MeetingSync(user_id=USER, meeting_id="other"), graph, store)
        == "SKIPPED_UNKNOWN_MEETING"
    )
    graph.list.assert_not_called()


def test_refresh_requires_auth_and_checks_graph_only_for_own_meetings(
    client, store, signed_in, graph
):
    other = "99999999-9999-9999-9999-999999999999"
    store.enroll(other, "Other")
    store.save_meeting(other, "Other", {"meeting_id": "private"})
    store.save_meeting(USER, "Mine", {"meeting_id": "mine"})
    assert client.post("/api/sync", json={}).status_code == 401
    assert client.post("/api/sync", headers=signed_in, json={}).json() == {"queued": 0}
    # Nothing is queued for the worker to pick up later: discovery and the
    # per-meeting check both ran against Graph inline, within this request.
    with store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0
    paths = [call.args[0] for call in graph.list.call_args_list]
    assert any("mine" in path for path in paths)
    assert all(other not in path for path in paths)


async def test_recovery_uses_the_user_meeting_link(store, graph):
    graph.list.return_value = [{"id": "meeting", "subject": "Recovered"}]
    result = await recover_from_link(
        store, graph, USER, "https://teams.microsoft.com/meet/123456789"
    )
    assert result == {"found": 1, "queued": 1}
    assert store.meetings(USER)[0]["subject"] == "Recovered"


def test_refresh_discovers_new_transcripts_immediately(client, store, signed_in, graph):
    graph.list.return_value = [{"id": "t", "meetingId": "new-meeting"}]
    response = client.post("/api/sync", headers=signed_in, json={})
    assert response.json() == {"queued": 1}
    job = parse_event(store.next_job()["payload"])
    assert job.meeting_id == "new-meeting"
    assert job.transcript_id == "t"
    # Refresh must not force a subscription repair: that PATCHes every
    # subscription ahead of the fetch the click is waiting for.
    assert not client.app.state.repair.is_set()


def test_reconnect_still_forces_a_subscription_repair(client, store, signed_in):
    """Refresh backs off, but the explicit repair path must keep working."""
    assert client.post("/api/reconnect", headers=signed_in, json={}).status_code == 200
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


def transcript_only():
    return {"meeting_id": "m", "transcripts": [{"transcript": {"id": "t"}}]}


def both_artifacts():
    return {
        "meeting_id": "m",
        "transcripts": [{"transcript": {"id": "t"}}],
        "insights": [{"insight": {"id": "i"}}],
    }


def test_settled_requires_both_artifacts():
    assert settled(both_artifacts())
    assert not settled(transcript_only())
    assert not settled({"meeting_id": "m", "insights": [{"insight": {"id": "i"}}]})
    assert not settled({"meeting_id": "m"})
    # Rows written before the list shape still read correctly.
    assert settled({"meeting_id": "m", "transcript": {"id": "t"}, "insight": {"id": "i"}})
    assert not settled({"meeting_id": "m", "transcript": {"id": "t"}})


def test_polling_stops_once_a_meeting_has_both_artifacts(store):
    store.save_meeting(USER, "Done", both_artifacts())
    store.save_meeting(USER, "Waiting", dict(transcript_only(), meeting_id="waiting"))
    assert queue_sync(store, USER) == 1
    queued = [parse_event(store.claim_job()["payload"]) for _ in range(1)]
    assert [job.meeting_id for job in queued] == ["waiting"]
    assert store.claim_job() is None


def test_discovery_still_runs_when_every_meeting_is_settled(store):
    """Otherwise a newly organised meeting would never be found again."""
    store.save_meeting(USER, "Done", both_artifacts())
    assert queue_sync(store, USER, discover=True) == 1
    assert isinstance(parse_event(store.claim_job()["payload"]), UserSync)


async def test_refresh_still_rechecks_a_settled_meeting(store, graph):
    """Polling backs off, but the manual Refresh sweep stays exhaustive."""
    store.save_meeting(USER, "Done", both_artifacts())
    graph.list.return_value = [{"id": "new-insight"}]
    await sync_now(store, graph, USER)
    paths = [call.args[0] for call in graph.list.call_args_list]
    assert any("aiInsights" in path for path in paths)


def test_a_second_transcript_reopens_polling_until_its_insight_lands():
    """Graph creates one insight per transcript; the first insight is not the end."""
    two_transcripts_one_insight = {
        "meeting_id": "m",
        "transcripts": [{"transcript": {"id": "t1"}}, {"transcript": {"id": "t2"}}],
        "insights": [{"insight": {"id": "i1"}}],
    }
    assert not settled(two_transcripts_one_insight)
    two_transcripts_one_insight["insights"].append({"insight": {"id": "i2"}})
    assert settled(two_transcripts_one_insight)


def test_an_insight_without_a_transcript_is_not_settled():
    assert not settled({"meeting_id": "m", "insights": [{"insight": {"id": "i"}}]})
