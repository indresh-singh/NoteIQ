import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import httpx
import pytest

from app.config import settings
from app.subscriptions import renew_subscriptions


def subscription(config, minutes):
    user = "11111111-1111-1111-1111-111111111111"
    return {
        "id": "sub",
        "resource": f"/copilot/users/{user}/onlineMeetings/getAllAiInsights",
        "notificationUrl": config.public_url + "/api/graph/notifications",
        "expirationDateTime": (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat(),
    }


async def test_create_missing_subscription(config, store):
    graph = AsyncMock()
    graph.list.return_value = []
    graph.request.return_value = {"id": "created"}
    await renew_subscriptions(graph, store)
    call = graph.request.call_args
    assert graph.request.await_count == 2
    assert call.kwargs["json"]["resource"].endswith("/getAllTranscripts")
    assert call.args == ("POST", "/subscriptions")
    assert call.kwargs["json"]["includeResourceData"] is False
    assert call.kwargs["json"]["lifecycleNotificationUrl"].endswith("/api/graph/lifecycle")


async def test_copilot_insight_subscription_created_with_external_summary_service(
    monkeypatch, config, store
):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    monkeypatch.setenv("OPENROUTER_MODEL", "test/model")
    settings.cache_clear()
    graph = AsyncMock()
    graph.list.return_value = []
    graph.request.return_value = {"id": "created"}
    await renew_subscriptions(graph, store)
    assert graph.request.await_count == 2
    resources = {call.kwargs["json"]["resource"] for call in graph.request.call_args_list}
    assert any(resource.endswith("/getAllAiInsights") for resource in resources)
    assert any(resource.endswith("/getAllTranscripts") for resource in resources)


@pytest.mark.parametrize("minutes,force,expected", [(10, False, 1), (50, False, 0), (50, True, 1)])
async def test_renew_or_reauthorize(config, store, minutes, force, expected):
    graph = AsyncMock()
    graph.list.return_value = [subscription(config, minutes)]
    graph.request.return_value = {"id": "created"}
    await renew_subscriptions(graph, store, force=force)
    assert graph.request.await_count == expected + 1
    if expected:
        assert graph.request.call_args_list[0].args == ("PATCH", "/subscriptions/sub")


async def test_disappearing_subscription_recreated(config, store):
    graph = AsyncMock()
    graph.list.return_value = [subscription(config, 10)]
    response = httpx.Response(404, request=httpx.Request("PATCH", "https://example.com"))
    graph.request.side_effect = [
        httpx.HTTPStatusError("gone", request=response.request, response=response),
        {"id": "recreated"},
        {"id": "new-transcript-sub"},
    ]
    await renew_subscriptions(graph, store)
    assert graph.request.call_args.args == ("POST", "/subscriptions")


async def test_disconnect_removes_subscription_without_recreating_it(config, store):
    graph = AsyncMock()
    graph.list.return_value = [subscription(config, 50)]
    store.disconnect("11111111-1111-1111-1111-111111111111")
    await renew_subscriptions(graph, store)
    graph.request.assert_awaited_once_with("DELETE", "/subscriptions/sub")


async def test_one_denied_user_does_not_block_another(config, store):
    other = "22222222-2222-2222-2222-222222222222"
    store.enroll(other, "Other")
    graph = AsyncMock()
    graph.list.return_value = []
    response = httpx.Response(403, request=httpx.Request("POST", "https://graph.microsoft.com"))
    graph.request.side_effect = [
        httpx.HTTPStatusError("denied", request=response.request, response=response),
        {"id": "user1-transcripts"},
        {"id": "other-insights"},
        {"id": "other-transcripts"},
    ]
    await renew_subscriptions(graph, store)
    assert store.user("11111111-1111-1111-1111-111111111111")["status"] == "ACCESS_REQUIRED"
    assert store.user(other)["status"] == "LISTENING"


# 429 is throttling, not a broken connection: see tests/test_throttling.py.
@pytest.mark.parametrize("code", [400, 409, 500, 503])
async def test_subscription_service_error_is_not_reported_as_missing_access(config, store, code):
    graph = AsyncMock()
    graph.list.return_value = []
    response = httpx.Response(code, request=httpx.Request("POST", "https://graph.microsoft.com"))
    graph.request.side_effect = httpx.HTTPStatusError(
        "failed", request=response.request, response=response
    )
    await renew_subscriptions(graph, store)
    assert store.user(store.users()[0])["status"] == "CONNECTION_ERROR"


async def test_created_subscription_is_renewed_next_cycle_without_reappearing_in_the_list(
    config, store
):
    """The local table, not Graph's own listing, is what renewal decides from.

    A subscription created this cycle must not need to show up in Graph's
    /subscriptions listing on the next cycle to be recognised as already
    existing and still fresh -- that recognition has to come from what this
    app itself recorded, which is the entire point of tracking expiry locally.
    """
    graph = AsyncMock()
    graph.list.return_value = []
    graph.request.return_value = {"id": "created-id"}
    await renew_subscriptions(graph, store)
    assert graph.request.await_count == 2

    graph.request.reset_mock()
    await renew_subscriptions(graph, store)
    graph.request.assert_not_awaited()

    await renew_subscriptions(graph, store, force=True)
    assert graph.request.await_count == 2
    for call in graph.request.call_args_list:
        assert call.args == ("PATCH", "/subscriptions/created-id")


async def test_renewal_concurrency_is_bounded(monkeypatch, config, store):
    monkeypatch.setenv("NOTEIQ_SUBSCRIPTION_CONCURRENCY", "5")
    settings.cache_clear()
    for i in range(20):
        store.enroll(f"33333333-3333-3333-3333-{i:012d}", f"User {i}")
    graph = AsyncMock()
    graph.list.return_value = []
    peak = 0
    current = 0

    async def handle(*args, **kwargs):
        nonlocal peak, current
        current += 1
        peak = max(peak, current)
        await asyncio.sleep(0.01)
        current -= 1
        return {"id": "sub"}

    graph.request.side_effect = handle
    await renew_subscriptions(graph, store)
    assert 1 < peak <= 5


def test_subscription_concurrency_bounds(monkeypatch):
    monkeypatch.setenv("NOTEIQ_SUBSCRIPTION_CONCURRENCY", "5")
    settings.cache_clear()
    assert settings().subscription_concurrency == 5
    monkeypatch.setenv("NOTEIQ_SUBSCRIPTION_CONCURRENCY", "0")
    settings.cache_clear()
    with pytest.raises(ValueError, match="NOTEIQ_SUBSCRIPTION_CONCURRENCY"):
        settings()
