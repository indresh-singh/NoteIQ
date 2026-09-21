import json
import sqlite3
from unittest.mock import AsyncMock

import httpx

from app.activity import INSIGHTS_READY, queue_notification, send_next_notification
from app.graph_client import GraphClient
from app.insights import process_insight
from app.models import InsightEvent, TranscriptEvent
from app.store import Store
from app.transcripts import process_transcript
from tests.conftest import USER
from tests.test_web import CHALLENGE, oauth


async def test_activity_uses_app_token_and_targets_organizer(store, config, monkeypatch):
    queue_notification(store, USER, "insight:meeting:id", "Budget", INSIGHTS_READY)
    queue_notification(store, USER, "insight:meeting:id", "Budget", INSIGHTS_READY)
    monkeypatch.setattr("app.graph_client.graph_token", AsyncMock(return_value="app-token"))
    original = httpx.AsyncClient
    sent = []

    def handle(request):
        assert request.headers["Authorization"] == "Bearer app-token"
        assert request.method == "POST"
        assert request.url.path == f"/v1.0/users/{USER}/teamwork/sendActivityNotification"
        sent.append(json.loads(request.content))
        return httpx.Response(204)

    monkeypatch.setattr(
        "app.graph_client.httpx.AsyncClient",
        lambda **kwargs: original(transport=httpx.MockTransport(handle), **kwargs),
    )
    assert await send_next_notification(store, GraphClient())
    assert not await send_next_notification(store, GraphClient())
    assert len(sent) == 1
    assert sent[0]["activityType"] == "insightsReady"
    app_id = config.teams_app_id or config.graph_client_id
    assert sent[0]["topic"]["webUrl"] == f"https://teams.microsoft.com/l/entity/{app_id}/meetings"
    assert "chat" not in json.dumps(sent)


async def test_retry_keeps_chain_id_and_saved_content(store):
    store.save_meeting(USER, "Meeting", {"meeting_id": "meeting"})
    queue_notification(store, USER, "insight:meeting", "Meeting", INSIGHTS_READY)
    graph = AsyncMock()
    graph.request.side_effect = [httpx.ConnectError("offline"), {}]
    assert await send_next_notification(store, graph)
    with store.connect() as db:
        row = db.execute("SELECT * FROM activity_outbox").fetchone()
        assert row["attempts"] == 1 and row["status"] == "pending"
        db.execute("UPDATE activity_outbox SET due=0")
    assert len(store.meetings(USER)) == 1
    assert await send_next_notification(store, graph)
    bodies = [call.kwargs["json"] for call in graph.request.call_args_list]
    assert bodies[0]["chainId"] == bodies[1]["chainId"]
    assert bodies[0]["activityType"] == "insightsReady"


async def test_a_meeting_notifies_once_for_insights_and_never_for_a_transcript(store, samples):
    graph = AsyncMock()
    graph.request.side_effect = [samples["meeting"], {"id": "transcript"}, "WEBVTT\nHello"]
    event = TranscriptEvent(user_id=USER, meeting_id="sample-meeting", transcript_id="transcript")
    assert await process_transcript(event, graph, store) == "TRANSCRIPT_SAVED"
    graph.request.side_effect = None
    assert not await send_next_notification(store, graph)
    insight = InsightEvent.from_resource(samples["notification"]["value"][0]["resource"])
    for _ in range(2):
        graph.request.side_effect = [samples["meeting"], samples["insight"]]
        assert await process_insight(insight, graph, store) == "SAVED"
    graph.request.side_effect = None
    assert await send_next_notification(store, graph)
    assert graph.request.call_args.kwargs["json"]["activityType"] == "insightsReady"
    assert not await send_next_notification(store, graph)
    assert len(store.meetings(USER)) == 1


async def test_queued_transcript_notifications_from_an_older_build_are_dropped(store):
    queue_notification(store, USER, "transcript:meeting:id", "Meeting", "Transcript ready")
    graph = AsyncMock()
    assert await send_next_notification(store, graph)
    graph.request.assert_not_awaited()
    with store.connect() as db:
        assert db.execute("SELECT status FROM activity_outbox").fetchone()[0] == "cancelled"


async def test_disconnect_stops_notifications(store):
    queue_notification(store, USER, "insight:m:i", "Meeting", INSIGHTS_READY)
    store.disconnect(USER)
    graph = AsyncMock()
    assert not await send_next_notification(store, graph)
    queue_notification(store, USER, "insight:m:i2", "Meeting", INSIGHTS_READY)
    assert not await send_next_notification(store, graph)
    graph.request.assert_not_awaited()


def test_old_chat_credentials_and_outbox_retired(config):
    with sqlite3.connect(config.database) as db:
        db.execute("CREATE TABLE chat_connections(token_cache TEXT)")
        db.execute("INSERT INTO chat_connections VALUES ('old-refresh-token')")
        db.execute("CREATE TABLE outbox(message TEXT)")
        db.execute("INSERT INTO outbox VALUES ('old chat send')")
    store = Store(config.database)
    with store.connect() as db:
        names = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "chat_connections" not in names and "outbox" not in names
    assert "activity_outbox" in names


def test_no_chat_consent_or_routes(client, signed_in, monkeypatch):
    identity = oauth(monkeypatch)
    client.post("/api/auth/start", json={"challenge": CHALLENGE, "teams_messages": True})
    assert identity.initiate_auth_code_flow.call_args.kwargs["scopes"] == ["User.Read"]
    assert client.get("/api/chats", headers=signed_in).status_code == 404
    assert "chat-picker" not in client.get("/").text


def test_notification_retry_is_scoped_to_current_user(client, store, signed_in):
    other = "22222222-2222-2222-2222-222222222222"
    store.enroll(other, "Other")
    for user in (USER, other):
        queue_notification(store, user, "insight:m:i", "Meeting", INSIGHTS_READY)
    with store.connect() as db:
        db.execute("UPDATE activity_outbox SET status='failed', attempts=5")
    assert client.get("/api/me", headers=signed_in).json()["notifications"] == "DELIVERY_ERROR"
    assert client.post("/api/notifications/retry", json={}, headers=signed_in).status_code == 200
    with store.connect() as db:
        states = dict(db.execute("SELECT user_id, status FROM activity_outbox"))
    assert states == {USER: "pending", other: "failed"}
