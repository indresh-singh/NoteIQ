import json
import time

import pytest

from app.models import InsightEvent, TranscriptEvent, UserSync, parse_event
from app.notifications import (
    UnsupportedNotificationResource,
    notification_resource_shape,
    validate_notifications,
)
from app.worker import run_job
from tests.conftest import USER


def test_validation_token_is_plain_text_and_not_queued(client):
    response = client.post("/api/graph/notifications", params={"validationToken": "a+b <test>"})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert response.text == "a+b <test>"
    assert client.app.state.store.next_job() is None


async def test_webhook_to_saved_card(client, store, graph, samples, signed_in):
    graph.request.side_effect = [samples["meeting"], samples["insight"]]
    assert client.post("/api/graph/notifications", json=samples["notification"]).status_code == 202
    queued = json.loads(store.next_job()["payload"])
    assert set(queued) == {"user_id", "meeting_id", "insight_id"}
    assert await run_job(store, graph)
    response = client.get("/api/meetings", headers=signed_in)
    assert response.status_code == 200
    assert len(response.json()) == 1
    assert response.json()[0]["content"]["insight"]["id"] == samples["insight"]["id"]
    # Saving an insight also checks for a missed transcript notification.
    assert await run_job(store, graph)
    assert store.next_job() is None


@pytest.mark.parametrize("state", [None, "", "wrong-secret", 42, "é"])
def test_invalid_secret_rejects_whole_batch(client, store, samples, state):
    samples["notification"]["value"].append(
        dict(samples["notification"]["value"][0], clientState=state)
    )
    assert client.post("/api/graph/notifications", json=samples["notification"]).status_code == 403
    assert store.next_job() is None


@pytest.mark.parametrize("payload", [None, [], {}, {"value": {}}, {"value": [None]}])
def test_malformed_body(client, payload):
    assert client.post("/api/graph/notifications", json=payload).status_code == 400


def test_foreign_tenant_rejected(client, samples):
    samples["notification"]["value"][0]["tenantId"] = "foreign"
    assert client.post("/api/graph/notifications", json=samples["notification"]).status_code == 403


def test_unenrolled_user_ignored(client, samples):
    assert client.post("/api/graph/notifications", json=samples["notification"]).status_code == 202
    assert client.app.state.store.next_job() is None


def test_unrelated_created_resource_is_rejected_without_becoming_an_insight(client, store, samples):
    samples["notification"]["value"][0]["resource"] = "communications/calls/sensitive-call-id"
    assert client.post("/api/graph/notifications", json=samples["notification"]).status_code == 400
    assert store.next_job() is None


def test_communications_transcript_resource_uses_subscription_owner(client, store, samples):
    subscription_id = samples["notification"]["value"][0]["subscriptionId"]
    store.save_subscription(USER, "transcripts", subscription_id, time.time() + 3600)
    samples["notification"]["value"][0].update(
        resource=(
            "communications/onlineMeetings('meeting%2Fone')/"
            "transcripts('transcript%20one')"
        ),
        resourceData={"id": "transcript one", "@odata.type": "#Microsoft.Graph.callTranscript"},
    )

    assert client.post("/api/graph/notifications", json=samples["notification"]).status_code == 202
    event = parse_event(store.next_job()["payload"])
    assert isinstance(event, TranscriptEvent)
    assert str(event.user_id) == USER
    assert event.meeting_id == "meeting/one"
    assert event.transcript_id == "transcript one"


def test_communications_transcript_resource_rejects_unknown_subscription(client, store, samples):
    samples["notification"]["value"][0]["resource"] = (
        "communications/onlineMeetings('meeting')/transcripts('transcript')"
    )

    assert client.post("/api/graph/notifications", json=samples["notification"]).status_code == 400
    assert store.next_job() is None


def test_unsupported_resource_shape_is_structural_and_redacts_identifiers(config, samples):
    resource = (
        "copilot/users/sensitive-user/onlineMeetings('sensitive-meeting')/aiInsights('secret')"
    )
    samples["notification"]["value"][0]["resource"] = resource
    with pytest.raises(UnsupportedNotificationResource) as error:
        validate_notifications(samples["notification"], config)
    assert error.value.resource_shape == (
        "prefix=copilot/users segments=5 meeting=parenthesized insight=parenthesized "
        "transcript=absent query=no fragment=no"
    )
    assert "sensitive" not in error.value.resource_shape
    assert "secret" not in error.value.resource_shape
    assert notification_resource_shape(resource) == error.value.resource_shape


def test_encoded_ids_round_trip():
    event = InsightEvent.from_resource(
        "copilot/users/11111111-1111-1111-1111-111111111111/onlineMeetings/a%2Fb%3D/aiInsights/c%2Bd"
    )
    assert event.meeting_id == "a/b="
    assert event.insight_path.endswith("a%2Fb%3D/aiInsights/c%2Bd")


@pytest.mark.parametrize(
    "resource",
    [
        "https://evil.invalid/path",
        "users/foo",
        "copilot/users/11111111-1111-1111-1111-111111111111/onlineMeetings/../aiInsights/id",
    ],
)
def test_invalid_resource(resource):
    with pytest.raises(ValueError):
        InsightEvent.from_resource(resource)


def test_lifecycle_schedules_repair(client, samples):
    samples["notification"]["value"][0]["lifecycleEvent"] = "reauthorizationRequired"
    assert client.post("/api/graph/lifecycle", json=samples["notification"]).status_code == 202
    assert client.app.state.repair.is_set()


def test_missed_event_queues_discovery_for_the_affected_user_only(client, store, samples):
    other = "22222222-2222-2222-2222-222222222222"
    store.enroll(other, "Someone else")
    samples["notification"]["value"][0]["lifecycleEvent"] = "missed"
    samples["notification"]["value"][0]["resource"] = (
        f"users/{USER}/onlineMeetings/getAllTranscripts"
    )
    assert client.post("/api/graph/lifecycle", json=samples["notification"]).status_code == 202
    assert store.user(USER)["status"] == "MISSED_EVENTS"
    assert store.user(other)["status"] != "MISSED_EVENTS"
    job = parse_event(store.next_job()["payload"])
    assert isinstance(job, UserSync)
    assert str(job.user_id) == USER
    with store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1


def test_missed_event_without_a_resolvable_resource_falls_back_to_everyone(client, store, samples):
    other = "22222222-2222-2222-2222-222222222222"
    store.enroll(other, "Someone else")
    samples["notification"]["value"][0]["lifecycleEvent"] = "missed"
    samples["notification"]["value"][0]["resource"] = ""
    assert client.post("/api/graph/lifecycle", json=samples["notification"]).status_code == 202
    assert store.user(USER)["status"] == "MISSED_EVENTS"
    assert store.user(other)["status"] == "MISSED_EVENTS"
    with store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 2


async def test_job_survives_restart_and_retries(store, graph, samples, config):
    from app.store import Store

    event = InsightEvent.from_resource(samples["notification"]["value"][0]["resource"])
    store.enqueue([event.model_dump_json()])
    restarted = Store(config.database)
    graph.request.side_effect = RuntimeError("sensitive-error")
    assert await run_job(restarted, graph)
    with restarted.connect() as db:
        job = dict(db.execute("SELECT * FROM jobs").fetchone())
    assert job["attempts"] == 1
    assert job["status"] == "pending"
    assert restarted.next_job() is None
    for _ in range(4):
        restarted.retry_job(job)
        job["attempts"] += 1
    with restarted.connect() as db:
        assert db.execute("SELECT status FROM jobs").fetchone()[0] == "failed"
