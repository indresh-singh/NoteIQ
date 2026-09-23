"""Offline incident replay and candidate fix; production behavior is not patched.

The 403 envelope is synthetic: the screenshot omitted Microsoft's message.
Candidate changes below are applied only through pytest monkeypatch.
"""

import json
from unittest.mock import AsyncMock

import httpx
import pytest

from app.insights import process_insight
from app.observability import response_diagnostics, safe_url
from app.subscriptions import ensure_subscription, renew_subscriptions
from tests.conftest import USER
from tests.test_insights import clients
from tests.test_subscriptions import subscription


def forbidden(message="Synthetic denial: production message unavailable"):
    response = httpx.Response(
        403,
        request=httpx.Request("POST", "https://graph.microsoft.com/v1.0/subscriptions"),
        headers={"request-id": "incident-replay-request"},
        json={"error": {"code": "ExtensionError", "message": message}},
    )
    return httpx.HTTPStatusError(
        "subscription rejected", request=response.request, response=response
    )


async def candidate_ensure(graph, config, resource, subscription_id):
    """Proposed guard: identify a callback conflict before attempting another POST.

    Never delete or take over the other callback automatically. Its operator must
    stop renewal and retire it (or use separate application registrations).
    """
    active = graph.list.return_value
    target = config.public_url + "/api/graph/notifications"
    matching = [item for item in active if item.get("resource", "").lstrip("/") == resource]
    if matching and not any(item.get("notificationUrl") == target for item in matching):
        conflicts = [
            {
                "id": item.get("id"),
                "callback": safe_url(item.get("notificationUrl", "")),
                "expires": item.get("expirationDateTime"),
            }
            for item in matching
        ]
        raise ValueError(
            f"Subscription callback conflict: resource={resource} existing={conflicts}"
        )
    return await ensure_subscription(graph, config, resource, subscription_id)


async def test_incident_badge_and_copilot_content_can_coexist(samples, store):
    graph = AsyncMock()
    graph.list.return_value = []
    graph.request.side_effect = [forbidden(), {}]
    await renew_subscriptions(graph, store)
    assert store.user(USER)["status"] == "ACCESS_REQUIRED"
    # Transcript subscription is still attempted after the insight rejection.
    assert graph.request.call_args_list[1].kwargs["json"]["resource"].endswith("getAllTranscripts")

    content_graph, _, event = clients(samples, store)
    assert await process_insight(event, content_graph, store) == "SAVED"
    assert store.meetings(USER)
    assert store.user(USER)["status"] == "ACCESS_REQUIRED"


async def test_badge_remains_after_healthy_renewal_until_reconnect(store):
    store.status(USER, "ACCESS_REQUIRED")
    graph = AsyncMock()
    graph.list.return_value = []
    graph.request.side_effect = [
        {"id": "insights"},
        {"id": "transcripts"},
        {},
        {},
    ]
    await renew_subscriptions(graph, store)
    assert store.user(USER)["status"] == "ACCESS_REQUIRED"
    # /api/reconnect performs this reset before asking the worker to renew.
    store.status(USER, "CONNECTING")
    await renew_subscriptions(graph, store, force=True)
    assert store.user(USER)["status"] == "LISTENING"


@pytest.mark.parametrize("candidate", [False, True], ids=["current", "proposed"])
async def test_foreign_callback_backtest(config, store, monkeypatch, caplog, candidate):
    existing = subscription(config, 50)
    existing["notificationUrl"] = (
        "https://old-deployment.test/api/graph/notifications?secret=hidden"
    )
    graph = AsyncMock()
    graph.list.return_value = [existing]

    async def enforce_single_subscription(method, path, **kwargs):
        if kwargs.get("json", {}).get("resource", "").endswith("getAllAiInsights"):
            raise forbidden("Synthetic quota denial for duplicate app/user subscription")
        return {}

    graph.request.side_effect = enforce_single_subscription
    if candidate:
        monkeypatch.setattr("app.subscriptions.ensure_subscription", candidate_ensure)
    await renew_subscriptions(graph, store)
    calls = graph.request.call_args_list
    if candidate:
        assert len(calls) == 1  # Only transcript creation; no duplicate insight POST.
        assert "Subscription callback conflict" in caplog.text
        assert "old-deployment.test" in caplog.text
        assert "hidden" not in caplog.text
        assert store.user(USER)["status"] == "CONNECTION_ERROR"
    else:
        assert len(calls) == 2  # Existing callback ignored; insight POST rejected.
        assert store.user(USER)["status"] == "ACCESS_REQUIRED"
    assert all(call.args[0] == "POST" for call in calls)  # Never steal/delete the other callback.
    assert calls[-1].kwargs["json"]["resource"].endswith("getAllTranscripts")


async def test_candidate_keeps_current_callback_renewal(config):
    graph = AsyncMock()
    existing = subscription(config, 10)
    graph.list.return_value = [existing]
    renewed_id, _ = await candidate_ensure(
        graph, config, existing["resource"].lstrip("/"), existing["id"]
    )
    assert renewed_id == "sub"
    graph.request.assert_awaited_once()
    assert graph.request.call_args.args == ("PATCH", "/subscriptions/sub")


async def test_candidate_recovers_after_conflicting_subscription_is_retired(
    config, store, monkeypatch
):
    monkeypatch.setattr("app.subscriptions.ensure_subscription", candidate_ensure)
    store.status(USER, "CONNECTING")
    graph = AsyncMock()
    graph.list.return_value = []  # Simulate verified retirement, do not delete anything live.
    graph.request.side_effect = [{"id": "insights"}, {"id": "transcripts"}]
    await renew_subscriptions(graph, store, force=True)
    assert graph.request.await_count == 2
    assert store.user(USER)["status"] == "LISTENING"


def test_current_diagnostics_retain_message_but_redact_error_code():
    details = json.loads(response_diagnostics(forbidden().response))
    assert details["body"]["error"]["code"] == "<redacted>"
    assert details["body"]["error"]["message"] == "Synthetic denial: production message unavailable"
    assert details["headers"]["request-id"] == "incident-replay-request"
