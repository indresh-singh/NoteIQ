"""Exercise the real Graph client against HTTP fixtures, without tenant credentials."""

import json
from unittest.mock import AsyncMock

import httpx

from app.graph_client import GraphClient
from app.subscriptions import renew_subscriptions
from app.worker import run_job
from scripts.recover import recover
from tests.conftest import USER


async def test_subscription_webhook_graph_fetch_and_private_card(
    monkeypatch, config, client, store, samples, signed_in
):
    requests = []
    original_client = httpx.AsyncClient

    def handle(request):
        assert request.headers["Authorization"] == "Bearer fixture-token"
        assert request.url.host == "graph.microsoft.com"
        requests.append((request.method, request.url.path))
        if request.url.path == "/v1.0/subscriptions":
            if request.method == "GET":
                return httpx.Response(200, json={"value": []})
            body = json.loads(request.content)
            assert body["resource"] in {
                f"/copilot/users/{USER}/onlineMeetings/getAllAiInsights",
                f"/users/{USER}/onlineMeetings/getAllTranscripts",
            }
            assert body["notificationUrl"] == config.public_url + "/api/graph/notifications"
            assert body["changeType"] == "created"
            assert body["clientState"] == config.client_state.get_secret_value()
            return httpx.Response(201, json={"id": "subscription"})
        if "/aiInsights/" in request.url.path:
            return httpx.Response(200, json=samples["insight"])
        return httpx.Response(200, json=samples["meeting"])

    monkeypatch.setattr("app.graph_client.graph_token", AsyncMock(return_value="fixture-token"))
    monkeypatch.setattr(
        "app.graph_client.httpx.AsyncClient",
        lambda **kw: original_client(transport=httpx.MockTransport(handle), **kw),
    )
    graph = GraphClient()
    await renew_subscriptions(graph, store)
    assert client.post("/api/graph/notifications", json=samples["notification"]).status_code == 202
    assert await run_job(store, graph)
    resource = samples["notification"]["value"][0]["resource"].lstrip("/")
    meeting_path = resource.removeprefix("copilot").split("/aiInsights/")[0]
    assert requests == [
        ("GET", "/v1.0/subscriptions"),
        ("POST", "/v1.0/subscriptions"),
        ("POST", "/v1.0/subscriptions"),
        ("GET", "/v1.0" + meeting_path),
        ("GET", "/v1.0/" + resource),
    ]
    cards = client.get("/api/meetings", headers=signed_in).json()
    assert cards[0]["content"]["insight"]["actionItems"] == [
        {**item, "dueDate": None} for item in samples["insight"]["actionItems"]
    ]
    assert cards[0]["content"]["card"]["type"] == "AdaptiveCard"


async def test_recovery_escapes_join_link_and_queues_each_insight(monkeypatch, store):
    graph = AsyncMock()
    graph.list.side_effect = [[{"id": "meeting/id"}], [{"id": "one"}, {"id": "two"}]]
    monkeypatch.setattr("scripts.recover.GraphClient", lambda: graph)
    join_url = "https://teams.microsoft.com/l/meetup-join/abc?context=a'b&x=1"
    await recover(USER, join_url)
    lookup = httpx.URL("https://graph.microsoft.com/v1.0" + graph.list.call_args_list[0].args[0])
    assert lookup.params["$filter"] == "JoinWebUrl eq '" + join_url.replace("'", "''") + "'"
    assert graph.list.call_args_list[1].args[0] == (
        f"/copilot/users/{USER}/onlineMeetings/meeting%2Fid/aiInsights"
    )
    with store.connect() as db:
        events = [json.loads(row[0]) for row in db.execute("SELECT payload FROM jobs ORDER BY id")]
    assert [event["insight_id"] for event in events] == ["one", "two"]


async def test_recovery_can_queue_transcripts_alongside_insights(monkeypatch, store):
    graph = AsyncMock()
    graph.list.side_effect = [[{"id": "meeting"}], [{"id": "insight"}], [{"id": "transcript"}]]
    monkeypatch.setattr("scripts.recover.GraphClient", lambda: graph)
    await recover(
        USER, "https://teams.microsoft.com/l/meetup-join/example", include_transcripts=True
    )
    with store.connect() as db:
        events = [json.loads(row[0]) for row in db.execute("SELECT payload FROM jobs ORDER BY id")]
    assert events[0]["insight_id"] == "insight"
    assert events[1]["transcript_id"] == "transcript"
    assert graph.list.call_args.args[0] == f"/users/{USER}/onlineMeetings/meeting/transcripts"


async def test_recovery_accepts_graph_meeting_id(monkeypatch, store):
    graph = AsyncMock()
    graph.list.side_effect = [[{"id": "insight"}], [{"id": "transcript"}]]
    monkeypatch.setattr("scripts.recover.GraphClient", lambda: graph)

    await recover(USER, meeting_id="meeting/id", include_transcripts=True)

    assert graph.list.call_args_list[0].args[0] == (
        f"/copilot/users/{USER}/onlineMeetings/meeting%2Fid/aiInsights"
    )
    assert graph.list.call_args_list[1].args[0] == (
        f"/users/{USER}/onlineMeetings/meeting%2Fid/transcripts"
    )
