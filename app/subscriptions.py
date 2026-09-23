import asyncio
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import httpx

from app.config import settings
from app.graph_client import GraphClient, graph_throttle_seconds
from app.store import Store

log = logging.getLogger(__name__)

RESOURCE_KINDS = ("insights", "transcripts")


def resource_for(user_id: str, kind: str) -> str:
    return (
        f"copilot/users/{user_id}/onlineMeetings/getAllAiInsights"
        if kind == "insights"
        else f"users/{user_id}/onlineMeetings/getAllTranscripts"
    )


def parse_tracked_resource(resource: str) -> tuple[str, str] | None:
    """(user_id, resource_kind) for one of the two resources this app subscribes to, or None."""
    resource = resource.lstrip("/")
    match = re.fullmatch(r"copilot/users/([^/]+)/onlineMeetings/getAllAiInsights", resource)
    if match:
        return match[1], "insights"
    match = re.fullmatch(r"users/([^/]+)/onlineMeetings/getAllTranscripts", resource)
    if match:
        return match[1], "transcripts"
    return None


def resource_owner(resource: str) -> str | None:
    """Extract the user id a subscription resource belongs to, or None."""
    parsed = parse_tracked_resource(resource)
    return parsed[0] if parsed else None


async def ensure_subscription(
    graph, config, resource: str, subscription_id: str | None
) -> tuple[str, datetime]:
    """Renew a known subscription, or create one from scratch.

    subscription_id is what the local table currently believes is live; None
    means no local record exists, so creation is attempted straight away. A
    404 on the renewal means Graph's own copy is already gone -- falling
    through to create is not a double-subscribe, it is catching up to Graph.
    """
    expiry = datetime.now(timezone.utc) + timedelta(hours=1)
    if subscription_id:
        try:
            await graph.request(
                "PATCH",
                f"/subscriptions/{quote(subscription_id, safe='')}",
                json={"expirationDateTime": expiry.isoformat()},
            )
            return subscription_id, expiry
        except httpx.HTTPStatusError as error:
            if error.response.status_code != 404:
                raise
    created = await graph.request(
        "POST",
        "/subscriptions",
        json={
            "changeType": "created",
            "notificationUrl": config.public_url + "/api/graph/notifications",
            "lifecycleNotificationUrl": config.public_url + "/api/graph/lifecycle",
            "resource": "/" + resource,
            "includeResourceData": False,
            "expirationDateTime": expiry.isoformat(),
            "clientState": config.client_state.get_secret_value(),
        },
    )
    return created["id"], expiry


async def renew_subscriptions(graph: GraphClient, store: Store, force: bool = False) -> int:
    """Bring every enrolled user's subscriptions up to date with Graph.

    Returns how many users were deferred because Graph throttled the tenant;
    the caller comes back for them sooner than the normal renewal cadence.
    """
    config = settings()
    notification_url = config.public_url + "/api/graph/notifications"
    active = await graph.list("/subscriptions")
    users = set(store.users())

    # Orphan cleanup: a subscription this app owns whose user is no longer
    # enrolled. Unrelated to renewal cadence, so this stays a plain sequential
    # walk over what is usually a handful of stale rows.
    for item in active:
        owner = resource_owner(item.get("resource", ""))
        if owner and owner not in users and item.get("notificationUrl") == notification_url:
            try:
                await graph.request("DELETE", f"/subscriptions/{quote(item['id'], safe='')}")
            except httpx.HTTPStatusError as error:
                if error.response.status_code != 404:
                    raise

    # Mirror Graph's live state into the local table before asking it anything.
    # This is what makes the local table trustworthy: a row lost to a restart,
    # a fresh deploy, or an out-of-band change in Graph is corrected here
    # rather than causing a duplicate subscription later.
    reconciled = []
    for item in active:
        if item.get("notificationUrl") != notification_url:
            continue
        parsed = parse_tracked_resource(item.get("resource", ""))
        if not parsed or parsed[0] not in users:
            continue
        user_id, kind = parsed
        try:
            expires_at = datetime.fromisoformat(
                item["expirationDateTime"].replace("Z", "+00:00")
            ).timestamp()
        except (KeyError, ValueError):
            continue
        reconciled.append((user_id, kind, item["id"], expires_at))
    store.reconcile_subscriptions(reconciled)

    # One query for what needs a Graph call, instead of walking every enrolled
    # user and rescanning `active` for each -- the O(users) cost that made a
    # 1500-user renewal cycle take minutes instead of seconds.
    due_by_user: dict[str, dict[str, str | None]] = {}
    for row in store.due_subscriptions(force=force):
        due_by_user.setdefault(row["user_id"], {})[row["resource_kind"]] = row["subscription_id"]

    gate = asyncio.Semaphore(settings().subscription_concurrency)
    deferred_users = set()

    async def process_user(user_id: str) -> None:
        errors = []
        deferred = False
        # Copilot insight subscriptions run regardless of AI_PROVIDER: Copilot
        # and OpenRouter insights are captured side by side, not as an
        # either/or choice. A missing transcript permission must not prevent
        # insight subscription setup, so each resource is tried independently.
        for kind in RESOURCE_KINDS:
            if kind not in due_by_user.get(user_id, {}):
                continue
            resource = resource_for(user_id, kind)
            subscription_id = due_by_user[user_id][kind]
            async with gate:
                # Once Graph has throttled the tenant, the users still waiting
                # for the gate are deferred rather than piling on more calls.
                if store.graph_paused_until() > time.time():
                    deferred = True
                    break
                try:
                    new_id, expiry = await ensure_subscription(
                        graph, config, resource, subscription_id
                    )
                    store.save_subscription(user_id, kind, new_id, expiry.timestamp())
                except httpx.HTTPStatusError as error:
                    wait = graph_throttle_seconds(error)
                    if wait is not None:
                        # Throttling says nothing about this user's connection,
                        # so their status is left as it was.
                        store.pause_graph(wait)
                        deferred = True
                        log.warning(
                            "Subscription throttled user=%s resource=%s retry_after_s=%.1f",
                            user_id,
                            resource,
                            wait,
                        )
                        break
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
                except Exception as error:
                    errors.append("CONNECTION_ERROR")
                    log.exception(
                        "Subscription failed user=%s resource=%s force=%s error_type=%s error=%s",
                        user_id,
                        resource,
                        force,
                        type(error).__name__,
                        error,
                    )
        if deferred:
            deferred_users.add(user_id)
        if errors:
            store.status(user_id, "ACCESS_REQUIRED" if "ACCESS_REQUIRED" in errors else errors[0])
        elif deferred:
            pass
        elif store.user(user_id)["status"] not in {"ACCESS_REQUIRED", "MISSED_EVENTS"}:
            store.status(user_id, "LISTENING")

    log.info(
        "Subscription renewal due=%s user_count=%s concurrency=%s force=%s",
        sum(len(kinds) for kinds in due_by_user.values()),
        len(users),
        config.subscription_concurrency,
        force,
    )
    await asyncio.gather(*(process_user(user_id) for user_id in sorted(due_by_user)))
    if deferred_users:
        log.warning(
            "Subscription renewal deferred by throttling user_count=%s", len(deferred_users)
        )
    return len(deferred_users)
