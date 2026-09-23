import json
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from cryptography.fernet import InvalidToken

from app.graph_client import GraphClient
from app.planner import Planner
from app.planner_identity import DelegatedGraph, cache_cipher, delegated_token
from tests.conftest import TENANT, USER
from tests.test_web import CHALLENGE, VERIFIER, callback, oauth


def test_consent_requires_session(client):
    assert client.post("/api/planner/connect", json={"challenge": CHALLENGE}).status_code == 401


def test_consent_is_bound_to_same_account(client, signed_in, monkeypatch):
    oauth(monkeypatch, {"oid": "22222222-2222-2222-2222-222222222222", "tid": TENANT})
    client.post("/api/planner/connect", headers=signed_in, json={"challenge": CHALLENGE})
    response = client.get("/auth/callback", params={"state": "oauth-state", "code": "code"})
    assert "same Microsoft account" in response.text
    assert client.app.state.store.planner_cache(USER) is None


def test_consent_stores_encrypted_cache_only_after_handoff(client, signed_in, monkeypatch, config):
    identity = oauth(monkeypatch)
    identity.acquire_token_by_auth_code_flow.return_value["scope"] = "User.Read Tasks.ReadWrite"
    client.post("/api/planner/connect", headers=signed_in, json={"challenge": CHALLENGE})
    assert (
        "https://graph.microsoft.com/Tasks.ReadWrite"
        in identity.initiate_auth_code_flow.call_args.kwargs["scopes"]
    )
    code = callback(client, "oauth-state")
    store = client.app.state.store
    assert store.planner_cache(USER) is None
    assert (
        client.post("/api/auth/complete", json={"code": code, "verifier": "b" * 43}).status_code
        == 401
    )
    assert store.planner_cache(USER) is None
    assert (
        client.post("/api/auth/complete", json={"code": code, "verifier": VERIFIER}).status_code
        == 200
    )
    encrypted = store.planner_cache(USER)
    assert encrypted and not encrypted.startswith("{")
    assert json.loads(cache_cipher(config, USER).decrypt(encrypted.encode())) == {}
    with pytest.raises(InvalidToken):
        cache_cipher(config, "other-user").decrypt(encrypted.encode())
    assert (
        client.post("/api/auth/complete", json={"code": code, "verifier": VERIFIER}).status_code
        == 401
    )


def test_missing_consent_does_not_save_connection(client, signed_in, monkeypatch):
    oauth(monkeypatch)
    client.post("/api/planner/connect", headers=signed_in, json={"challenge": CHALLENGE})
    response = client.get("/auth/callback", params={"state": "oauth-state", "code": "code"})
    assert "did not grant Tasks.ReadWrite" in response.text
    assert client.app.state.store.planner_cache(USER) is None


async def test_personal_discovery_paginates_with_delegated_credentials(config, graph):
    graph.request.side_effect = [
        {
            "value": [{"id": "personal", "title": "Private", "container": {"type": "user"}}],
            "@odata.nextLink": "https://graph.microsoft.com/v1.0/me/planner/plans?$skiptoken=next",
        },
        {"value": [{"id": "group", "title": "Shared"}]},
    ]
    planner = Planner(config, DelegatedGraph(graph, "delegated-token"), delegated=True)
    plans = await planner.available_plans(USER)
    assert [p["id"] for p in plans] == ["personal", "group"]
    assert plans[0]["path"] == "Personal"
    for call in graph.request.call_args_list:
        assert call.kwargs["access_token"] == "delegated-token"
    graph.list.assert_not_called()


async def test_graph_never_uses_app_token_for_delegated_call(monkeypatch):
    app_token = AsyncMock(side_effect=AssertionError("must not use app token"))
    monkeypatch.setattr("app.graph_client.graph_token", app_token)
    seen = []

    def respond(request):
        seen.append(request)
        return httpx.Response(200, json={"value": []})

    graph = GraphClient()
    graph._client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    try:
        await DelegatedGraph(graph, "user-token", "beta").list("/me/planner/plans")
        assert seen[0].headers["Authorization"] == "Bearer user-token"
        assert seen[0].url.path == "/beta/me/planner/plans"
        with pytest.raises(ValueError, match="Unsupported Graph URL"):
            await DelegatedGraph(graph, "user-token").list("https://evil.test/steal")
    finally:
        await graph.aclose()


async def test_silent_refresh_persists_encrypted_cache(config, store, monkeypatch):
    cipher = cache_cipher(config, USER)
    store.save_planner_cache(USER, cipher.encrypt(b"{}").decode())

    def identity_factory(*, token_cache):
        identity = Mock()
        identity.get_accounts.return_value = [{"local_account_id": USER, "realm": TENANT}]

        def acquire(scopes, account):
            token_cache.deserialize('{"refreshed": true}')
            token_cache.has_state_changed = True
            return {"access_token": "refreshed-token"}

        identity.acquire_token_silent.side_effect = acquire
        return identity

    monkeypatch.setattr("app.planner_identity.identity_client", identity_factory)
    assert await delegated_token(config, store, USER) == "refreshed-token"
    assert json.loads(cipher.decrypt(store.planner_cache(USER).encode())) == {"refreshed": True}


async def test_revoked_cache_requires_reconnect(config, store, monkeypatch):
    store.save_planner_cache(USER, cache_cipher(config, USER).encrypt(b"{}").decode())
    identity = Mock()
    identity.get_accounts.return_value = []
    monkeypatch.setattr("app.planner_identity.identity_client", lambda **kw: identity)
    with pytest.raises(ValueError, match="Reconnect personal Planner"):
        await delegated_token(config, store, USER)


def test_delegated_routes_and_disconnect(client, signed_in, monkeypatch, graph):
    store = client.app.state.store
    store.save_planner_cache(USER, "encrypted")
    monkeypatch.setattr("app.web.delegated_token", AsyncMock(return_value="user-token"))
    graph.request.return_value = {"value": [{"id": "personal", "title": "Private"}]}
    response = client.get("/api/planner/available-plans", headers=signed_in)
    assert response.json()["plans"][0]["id"] == "personal"
    graph.request.return_value = {"title": "Private"}
    assert (
        client.post(
            "/api/planner/plans", headers=signed_in, json={"plan_id": "personal"}
        ).status_code
        == 200
    )
    assert graph.request.call_args.kwargs["access_token"] == "user-token"
    assert client.get("/api/planner", headers=signed_in).json()["delegated_connected"]
    assert client.post("/api/planner/disconnect", headers=signed_in, json={}).status_code == 200
    assert store.planner_cache(USER) is None
    assert store.planner_plans(USER) == []
    store.update_planner_cache(USER, "encrypted", "stale-refresh")
    assert store.planner_cache(USER) is None


def test_expired_delegation_does_not_fallback_to_application(client, signed_in, monkeypatch, graph):
    client.app.state.store.save_planner_cache(USER, "encrypted")
    monkeypatch.setattr(
        "app.web.delegated_token", AsyncMock(side_effect=ValueError("Reconnect personal Planner"))
    )
    assert client.get("/api/planner/available-plans", headers=signed_in).status_code == 409
    graph.list.assert_not_called()
    graph.request.assert_not_called()


async def test_personal_export_uses_delegation_for_every_step(config, graph):
    graph.request.side_effect = [{"id": "task"}, {"@odata.etag": "etag"}, {}]
    planner = Planner(config, DelegatedGraph(graph, "user-token"), delegated=True)
    result = await planner.create_task("personal", "Action", "Description")
    assert result["id"] == "task"
    assert [call.args[0] for call in graph.request.call_args_list] == ["POST", "GET", "PATCH"]
    assert all(call.kwargs["access_token"] == "user-token" for call in graph.request.call_args_list)
    assert graph.request.call_args.kwargs["headers"] == {"If-Match": "etag"}


async def test_discovery_403_is_not_an_empty_success(config, graph):
    response = httpx.Response(
        403, request=httpx.Request("GET", "https://graph.microsoft.com/v1.0/me/planner/plans")
    )
    graph.request.side_effect = httpx.HTTPStatusError(
        "Forbidden", request=response.request, response=response
    )
    with pytest.raises(ValueError, match="denied access"):
        await Planner(config, DelegatedGraph(graph, "user-token"), delegated=True).available_plans(
            USER
        )


def test_full_disconnect_removes_delegation(store):
    store.save_planner_cache(USER, "encrypted")
    store.disconnect(USER)
    assert store.planner_cache(USER) is None
