import json

import pytest

from app.insights import process_insight
from app.models import InsightEvent, MeetingSync
from app.sync import queue_sync, sync_meeting
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
    assert client.post("/api/sync", headers=signed_in, json={}).json() == {"queued": 1}
    queue_sync(store, USER)
    with store.connect() as db:
        rows = db.execute("SELECT payload FROM jobs").fetchall()
    assert len(rows) == 1
    assert json.loads(rows[0][0])["meeting_id"] == "mine"
