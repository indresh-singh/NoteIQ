import asyncio
from functools import lru_cache

import msal

from app.config import settings


def identity_client(*, token_cache=None) -> msal.ConfidentialClientApplication:
    config = settings()
    return msal.ConfidentialClientApplication(
        str(config.graph_client_id),
        authority=f"https://login.microsoftonline.com/{config.tenant_id}",
        client_credential=config.graph_secret.get_secret_value(),
        timeout=20,
        token_cache=token_cache,
    )


@lru_cache
def graph_identity() -> msal.ConfidentialClientApplication:
    return identity_client()


async def graph_token() -> str:
    result = await asyncio.to_thread(
        graph_identity().acquire_token_for_client, ["https://graph.microsoft.com/.default"]
    )
    if "access_token" not in result:
        raise RuntimeError("Microsoft Graph authentication failed; check app credentials")
    return result["access_token"]
