from unittest.mock import AsyncMock

import httpx
import pytest

from app.graph_client import GraphClient, retry


async def test_graph_auth_and_response(monkeypatch):
    token = AsyncMock(return_value="test-token")
    monkeypatch.setattr("app.graph_client.graph_token", token)
    original_client = httpx.AsyncClient

    def handle(request):
        assert request.headers["Authorization"] == "Bearer test-token"
        assert request.url.path == "/v1.0/subscriptions"
        return httpx.Response(200, json={"value": [{"id": "sub"}]})

    monkeypatch.setattr(
        "app.graph_client.httpx.AsyncClient",
        lambda **kw: original_client(transport=httpx.MockTransport(handle), **kw),
    )
    assert await GraphClient().list("/subscriptions") == [{"id": "sub"}]


async def test_foreign_pagination_url_rejected():
    with pytest.raises(ValueError):
        await GraphClient().request("GET", "https://evil.invalid/steal-token")


class TestConnectionReuse:
    """A client per request meant a TLS handshake on every Graph call, and the
    polling sweep alone makes thousands an hour."""

    async def test_every_call_shares_one_client(self, monkeypatch):
        monkeypatch.setattr("app.graph_client.graph_token", AsyncMock(return_value="t"))
        original_client = httpx.AsyncClient
        built = []

        def build(**kw):
            client = original_client(
                transport=httpx.MockTransport(lambda request: httpx.Response(200, json={})), **kw
            )
            built.append(client)
            return client

        monkeypatch.setattr("app.graph_client.httpx.AsyncClient", build)
        graph = GraphClient()
        await graph.request("GET", "/subscriptions")
        await graph.request("GET", "/subscriptions")
        assert len(built) == 1
        await graph.aclose()

    async def test_a_closed_client_is_replaced_rather_than_reused(self):
        graph = GraphClient()
        first = graph.client()
        await first.aclose()
        assert graph.client() is not first
        await graph.aclose()

    async def test_closing_twice_is_safe(self):
        """Shutdown runs it; a failed startup can run it again."""
        graph = GraphClient()
        graph.client()
        await graph.aclose()
        await graph.aclose()


async def test_pagination():
    graph = GraphClient()
    graph.request = AsyncMock(
        side_effect=[
            {
                "value": [{"id": "one"}],
                "@odata.nextLink": "https://graph.microsoft.com/v1.0/subscriptions?$skip=1",
            },
            {"value": [{"id": "two"}]},
        ]
    )
    assert len(await graph.list("/subscriptions")) == 2


@pytest.mark.parametrize("code,delays", [(429, [3, 3]), (500, [1, 2]), (403, [])])
async def test_retry_policy(monkeypatch, code, delays):
    sleep = AsyncMock()
    monkeypatch.setattr("app.graph_client.asyncio.sleep", sleep)
    response = httpx.Response(
        code,
        headers={"Retry-After": "3"} if code == 429 else {},
        request=httpx.Request("GET", "https://graph.test"),
    )
    operation = AsyncMock(
        side_effect=httpx.HTTPStatusError("failed", request=response.request, response=response)
    )
    with pytest.raises(httpx.HTTPStatusError):
        await retry(operation)
    assert [call.args[0] for call in sleep.call_args_list] == delays


async def test_transport_failure_then_success(monkeypatch):
    monkeypatch.setattr("app.graph_client.asyncio.sleep", AsyncMock())
    operation = AsyncMock(side_effect=[httpx.ConnectError("offline"), "ok"])
    assert await retry(operation) == "ok"


async def test_transcript_text_keeps_accept_header_on_retry(monkeypatch):
    monkeypatch.setattr("app.graph_client.graph_token", AsyncMock(return_value="app-token"))
    monkeypatch.setattr("app.graph_client.asyncio.sleep", AsyncMock())
    original = httpx.AsyncClient
    requests = []

    def handle(request):
        assert request.headers["Accept"] == "text/vtt"
        assert request.headers["Authorization"] == "Bearer app-token"
        requests.append(request)
        return (
            httpx.Response(503) if len(requests) == 1 else httpx.Response(200, text="WEBVTT\nHello")
        )

    monkeypatch.setattr(
        "app.graph_client.httpx.AsyncClient",
        lambda **kwargs: original(transport=httpx.MockTransport(handle), **kwargs),
    )
    result = await GraphClient().request(
        "GET",
        "/users/user/onlineMeetings/meeting/transcripts/id/content",
        text=True,
        headers={"Accept": "text/vtt"},
    )
    assert result == "WEBVTT\nHello"
    assert len(requests) == 2
