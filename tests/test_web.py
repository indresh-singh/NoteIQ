import hashlib
import re
from unittest.mock import Mock
from urllib.parse import parse_qs, urlsplit

import pytest

from app.store import Store, digest
from tests.conftest import TENANT, USER

VERIFIER = "a" * 43
CHALLENGE = hashlib.sha256(VERIFIER.encode()).hexdigest()


def oauth(monkeypatch, claims=None):
    identity = Mock()
    identity.initiate_auth_code_flow.return_value = {
        "state": "oauth-state",
        "nonce": "nonce",
        "code_verifier": "pkce-secret",
        "auth_uri": "https://login.microsoftonline.com/authorize?state=oauth-state",
    }
    identity.acquire_token_by_auth_code_flow.return_value = {
        "id_token_claims": claims or {"oid": USER, "tid": TENANT, "name": "Demo Organizer"},
        "access_token": "microsoft-access-secret",
        "refresh_token": "microsoft-refresh-secret",
    }
    monkeypatch.setattr("app.web.identity_client", lambda **kwargs: identity)
    return identity


def start(client):
    response = client.post("/api/auth/start", json={"challenge": CHALLENGE})
    assert response.status_code == 200
    return parse_qs(urlsplit(response.json()["url"]).query)["state"][0]


def callback(client, state):
    response = client.get("/auth/callback", params={"state": state, "code": "microsoft-auth-code"})
    assert response.status_code == 200
    assert "microsoft-access-secret" not in response.text
    assert "microsoft-refresh-secret" not in response.text
    return re.search(r'data-code="([^"]+)"', response.text).group(1)


def test_me_reports_the_active_ai_provider(client, store, signed_in):
    assert client.get("/api/me", headers=signed_in).json()["ai_provider"] == "copilot"


def test_login_enrolls_verified_user_and_starts_updates(client, monkeypatch):
    identity = oauth(monkeypatch)
    state = start(client)
    assert client.app.state.store.users() == []
    code = callback(client, state)
    assert client.app.state.store.users() == []
    response = client.post("/api/auth/complete", json={"code": code, "verifier": VERIFIER})
    assert response.status_code == 200
    headers = {"Authorization": "Bearer " + response.json()["token"]}
    assert client.get("/api/me", headers=headers).json()["id"] == USER
    assert client.app.state.repair.is_set()
    flow = identity.acquire_token_by_auth_code_flow.call_args.args[0]
    assert flow["code_verifier"] == "pkce-secret"
    assert flow["nonce"] == "nonce"
    assert identity.initiate_auth_code_flow.call_args.kwargs["redirect_uri"].endswith(
        "/auth/callback"
    )
    assert (
        client.post("/api/auth/complete", json={"code": code, "verifier": VERIFIER}).status_code
        == 401
    )


def test_login_code_is_bound_to_initiating_tab(client, monkeypatch):
    oauth(monkeypatch)
    code = callback(client, start(client))
    assert (
        client.post("/api/auth/complete", json={"code": code, "verifier": "b" * 43}).status_code
        == 401
    )
    assert client.app.state.store.users() == []
    assert (
        client.post("/api/auth/complete", json={"code": code, "verifier": VERIFIER}).status_code
        == 200
    )


def test_teams_popup_uses_the_teams_completion_channel(client, monkeypatch):
    oauth(monkeypatch)
    response = client.post("/api/auth/start", json={"challenge": CHALLENGE, "in_teams": True})
    state = parse_qs(urlsplit(response.json()["url"]).query)["state"][0]
    response = client.get("/auth/callback", params={"state": state, "code": "code"})
    assert response.status_code == 200
    assert 'data-in-teams="true"' in response.text
    assert "microsoft-access-secret" not in response.text


def test_identity_provider_unavailable_returns_safe_error(client, monkeypatch):
    monkeypatch.setattr("app.web.identity_client", Mock(side_effect=RuntimeError("secret-details")))
    response = client.post("/api/auth/start", json={"challenge": CHALLENGE})
    assert response.status_code == 503
    assert "secret-details" not in response.text


@pytest.mark.parametrize(
    "claims",
    [
        {"oid": USER, "tid": "cccccccc-cccc-cccc-cccc-cccccccccccc"},
        {"oid": "not-a-user-id", "tid": TENANT},
        {"tid": TENANT},
    ],
)
def test_foreign_or_invalid_identity_cannot_enroll(client, monkeypatch, claims):
    oauth(monkeypatch, claims)
    state = start(client)
    assert client.get("/auth/callback", params={"state": state, "code": "code"}).status_code == 400
    assert client.app.state.store.users() == []


def test_unknown_state_and_msal_failure_are_rejected(client, monkeypatch):
    identity = oauth(monkeypatch)
    assert client.get("/auth/callback?state=forged&code=code").status_code == 400
    identity.acquire_token_by_auth_code_flow.assert_not_called()
    state = start(client)
    identity.acquire_token_by_auth_code_flow.side_effect = ValueError("private-nonce-error")
    response = client.get("/auth/callback", params={"state": state, "code": "code"})
    assert response.status_code == 400
    assert "private-nonce-error" not in response.text
    assert client.app.state.store.users() == []


def test_expired_flow_cannot_be_used(client, monkeypatch):
    oauth(monkeypatch)
    state = start(client)
    with client.app.state.store.connect() as db:
        db.execute("UPDATE temporary SET expires=0")
    assert client.get("/auth/launch", params={"state": state}).status_code == 400
    assert client.get("/auth/callback", params={"state": state, "code": "code"}).status_code == 400


def test_results_are_private_and_never_selected_by_browser_user_id(client, store, signed_in):
    other = "22222222-2222-2222-2222-222222222222"
    store.enroll(other, "Other person")
    store.save_meeting(other, "Confidential other meeting", {})
    store.save_meeting(USER, "My meeting", {})
    assert client.get("/api/meetings").status_code == 401
    response = client.get("/api/meetings?user_id=" + other, headers=signed_in)
    assert [item["subject"] for item in response.json()] == ["My meeting"]
    assert "Confidential" not in response.text


def test_logout_revokes_session_without_disabling_collection(client, store, signed_in):
    assert client.post("/api/logout", headers=signed_in, json={}).status_code == 200
    assert client.get("/api/me", headers=signed_in).status_code == 401
    assert USER in store.users()


def test_disconnect_deletes_results_revokes_sessions_and_prevents_late_writes(
    client, store, signed_in
):
    store.save_meeting(USER, "My meeting", {})
    assert client.post("/api/disconnect", headers=signed_in, json={}).status_code == 200
    store.save_meeting(USER, "Late Graph response", {})
    assert store.meetings(USER) == []
    assert USER not in store.users()
    assert client.get("/api/me", headers=signed_in).status_code == 401
    assert client.app.state.repair.is_set()


def test_session_expiry_and_only_hashes_stored(client, store):
    token = store.session(USER)
    with store.connect() as db:
        assert db.execute("SELECT token_hash FROM sessions").fetchone()[0] == digest(token)
        db.execute("UPDATE sessions SET expires=0")
    assert client.get("/api/me", headers={"Authorization": "Bearer " + token}).status_code == 401


def test_wrong_origin_is_rejected(client, signed_in):
    assert (
        client.post(
            "/api/disconnect",
            json={},
            headers={
                **signed_in,
                "Origin": "https://untrusted.invalid",
            },
        ).status_code
        == 403
    )


def test_page_and_static_assets_are_public_but_data_is_protected(client, config):
    assert client.get("/healthz").json() == {"status": "ok"}
    page = client.get("/")
    assert page.status_code == 200
    assert "Connect Microsoft 365" in page.text
    assert "frame-ancestors" in page.headers["content-security-policy"]
    assert "x-frame-options" not in page.headers
    assert page.headers["referrer-policy"] == "no-referrer"
    assert page.headers["cache-control"] == "no-store"
    assert client.get("/static/vendor/teams.min.js").status_code == 200
    assert client.get("/static/vendor/adaptivecards.min.js").status_code == 200
    assert client.get("/static/../.env").status_code == 404
    assert client.get("/api/messages").status_code == 404
    assert client.get("/api/me").status_code == 401
    assert Store(config.database).users() == []
