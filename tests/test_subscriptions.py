from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import httpx
import pytest

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
    await renew_subscriptions(graph, store)
    call = graph.request.call_args
    assert graph.request.await_count == 2
    assert call.kwargs["json"]["resource"].endswith("/getAllTranscripts")
    assert call.args == ("POST", "/subscriptions")
    assert call.kwargs["json"]["includeResourceData"] is False
    assert call.kwargs["json"]["lifecycleNotificationUrl"].endswith("/api/graph/lifecycle")


@pytest.mark.parametrize("minutes,force,expected", [(10, False, 1), (50, False, 0), (50, True, 1)])
async def test_renew_or_reauthorize(config, store, minutes, force, expected):
    graph = AsyncMock()
    graph.list.return_value = [subscription(config, minutes)]
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
        {},
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
        {},
        {},
        {},
    ]
    await renew_subscriptions(graph, store)
    assert store.user("11111111-1111-1111-1111-111111111111")["status"] == "ACCESS_REQUIRED"
    assert store.user(other)["status"] == "LISTENING"


@pytest.mark.parametrize("code", [400, 409, 429, 500, 503])
async def test_subscription_service_error_is_not_reported_as_missing_access(config, store, code):
    graph = AsyncMock()
    graph.list.return_value = []
    response = httpx.Response(code, request=httpx.Request("POST", "https://graph.microsoft.com"))
    graph.request.side_effect = httpx.HTTPStatusError(
        "failed", request=response.request, response=response
    )
    await renew_subscriptions(graph, store)
    assert store.user(store.users()[0])["status"] == "CONNECTION_ERROR"
