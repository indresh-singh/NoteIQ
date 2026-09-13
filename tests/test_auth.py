from unittest.mock import Mock

import pytest

from app.auth import graph_token


async def test_client_credentials_use_graph_scope(monkeypatch):
    identity = Mock()
    identity.acquire_token_for_client.return_value = {"access_token": "access-token"}
    monkeypatch.setattr("app.auth.graph_identity", lambda: identity)
    assert await graph_token() == "access-token"
    identity.acquire_token_for_client.assert_called_once_with(
        ["https://graph.microsoft.com/.default"]
    )


async def test_graph_auth_failure_is_sanitized(monkeypatch):
    identity = Mock()
    identity.acquire_token_for_client.return_value = {"error_description": "private-secret"}
    monkeypatch.setattr("app.auth.graph_identity", lambda: identity)
    with pytest.raises(RuntimeError, match="authentication failed") as error:
        await graph_token()
    assert "private-secret" not in str(error.value)
