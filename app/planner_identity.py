"""Per-user encrypted MSAL caches. Microsoft tokens never reach the browser."""

import asyncio
import base64
import logging

import msal
from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from app.auth import identity_client
from app.graph_client import GraphClient

log = logging.getLogger(__name__)

PLANNER_SCOPES = [
    "https://graph.microsoft.com/User.Read",
    "https://graph.microsoft.com/Tasks.ReadWrite",
]


def cache_cipher(config, user_id):
    # Domain-separated key bound to this tenant, app and user. Rotating the app
    # secret requires reconnecting Planner; no new deployment secret is needed.
    key = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=str(config.tenant_id).encode(),
        info=f"noteiq-planner:{config.graph_client_id}:{user_id}".encode(),
    ).derive(config.graph_secret.get_secret_value().encode())
    return Fernet(base64.urlsafe_b64encode(key))


async def delegated_token(config, store, user_id):
    encrypted = store.planner_cache(user_id)
    cache = msal.SerializableTokenCache()
    try:
        cache.deserialize(cache_cipher(config, user_id).decrypt(encrypted.encode()).decode())
    except (InvalidToken, ValueError, AttributeError):
        raise ValueError("Reconnect personal Planner to renew Microsoft access.") from None

    def acquire():
        identity = identity_client(token_cache=cache)
        accounts = identity.get_accounts()
        account = next(
            (
                a
                for a in accounts
                if a.get("local_account_id") == user_id and a.get("realm") == str(config.tenant_id)
            ),
            None,
        )
        return identity.acquire_token_silent(PLANNER_SCOPES, account=account) if account else None

    try:
        result = await asyncio.to_thread(acquire)
    except Exception as error:
        log.warning(
            "Planner token refresh failed user=%s error_type=%s", user_id, type(error).__name__
        )
        raise ValueError("Microsoft sign-in is unavailable. Try Planner again shortly.") from None
    if cache.has_state_changed:
        store.update_planner_cache(
            user_id,
            encrypted,
            cache_cipher(config, user_id).encrypt(cache.serialize().encode()).decode(),
        )
    if not result or not result.get("access_token"):
        raise ValueError("Reconnect personal Planner to renew Microsoft access.")
    return result["access_token"]


class DelegatedGraph:
    """Request-local credentials, using the shared transport and Graph diagnostics."""

    def __init__(self, graph, token, version="v1.0"):
        self.graph = graph
        self.token = token
        self.version = version

    async def request(self, method, path, **kwargs):
        return await self.graph.request(
            method, path, access_token=self.token, graph_version=self.version, **kwargs
        )

    async def list(self, path):
        return await GraphClient.list(self, path)
