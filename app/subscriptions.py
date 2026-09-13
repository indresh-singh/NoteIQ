import logging
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import httpx

from app.config import settings
from app.graph_client import GraphClient
from app.store import Store

log = logging.getLogger(__name__)


async def ensure_subscription(graph, active, resource, config, force):
    notification_url = config.public_url + "/api/graph/notifications"
    existing = [
        item
        for item in active
        if item.get("resource", "").lstrip("/") == resource
        and item.get("notificationUrl") == notification_url
    ]
    expiry = datetime.now(timezone.utc) + timedelta(hours=1)
    for item in existing:
        if not force and datetime.fromisoformat(
            item["expirationDateTime"].replace("Z", "+00:00")
        ) > datetime.now(timezone.utc) + timedelta(minutes=30):
            return
        try:
            await graph.request(
                "PATCH",
                f"/subscriptions/{quote(item['id'], safe='')}",
                json={"expirationDateTime": expiry.isoformat()},
            )
            return
        except httpx.HTTPStatusError as error:
            if error.response.status_code != 404:
                raise
    await graph.request(
        "POST",
        "/subscriptions",
        json={
            "changeType": "created",
            "notificationUrl": notification_url,
            "lifecycleNotificationUrl": config.public_url + "/api/graph/lifecycle",
            "resource": "/" + resource,
            "includeResourceData": False,
            "expirationDateTime": expiry.isoformat(),
            "clientState": config.client_state.get_secret_value(),
        },
    )


async def renew_subscriptions(graph: GraphClient, store: Store, force: bool = False) -> None:
    config = settings()
    active = await graph.list("/subscriptions")
    users = set(store.users())
    for item in active:
        resource = item.get("resource", "").lstrip("/")
        owner = re.fullmatch(r"copilot/users/([^/]+)/onlineMeetings/getAllAiInsights", resource)
        owner = owner or re.fullmatch(r"users/([^/]+)/onlineMeetings/getAllTranscripts", resource)
        if (
            owner
            and owner[1] not in users
            and item.get("notificationUrl") == config.public_url + "/api/graph/notifications"
        ):
            try:
                await graph.request("DELETE", f"/subscriptions/{quote(item['id'], safe='')}")
            except httpx.HTTPStatusError as error:
                if error.response.status_code != 404:
                    raise
    for user_id in sorted(users):
        errors = []
        # A missing transcript permission must not prevent insight subscription setup.
        for resource in (
            f"copilot/users/{user_id}/onlineMeetings/getAllAiInsights",
            f"users/{user_id}/onlineMeetings/getAllTranscripts",
        ):
            try:
                await ensure_subscription(graph, active, resource, config, force)
            except httpx.HTTPStatusError as error:
                errors.append(
                    "ACCESS_REQUIRED"
                    if error.response.status_code in {401, 403}
                    else "CONNECTION_ERROR"
                )
                log.warning(
                    "Subscription user=%s resource=%s http_status=%s",
                    user_id,
                    resource,
                    error.response.status_code,
                )
            except Exception:
                errors.append("CONNECTION_ERROR")
                log.warning("Subscription user=%s failed", user_id)
        if errors:
            store.status(user_id, "ACCESS_REQUIRED" if "ACCESS_REQUIRED" in errors else errors[0])
        elif store.user(user_id)["status"] not in {"ACCESS_REQUIRED", "MISSED_EVENTS"}:
            store.status(user_id, "LISTENING")
