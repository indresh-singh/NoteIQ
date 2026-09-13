"""Repair missed notifications for meetings already known to this user."""

import logging
import time
from urllib.parse import quote

from app.models import InsightEvent, MeetingSync, TranscriptEvent

log = logging.getLogger(__name__)


def queue_sync(store, user_id):
    meetings = store.meetings(user_id)[:10]
    payloads = [
        MeetingSync(user_id=user_id, meeting_id=m["content"]["meeting_id"]).model_dump_json()
        for m in meetings
        if m["created"] >= time.time() - 86400
    ]
    store.enqueue(payloads)
    return len(payloads)


async def sync_meeting(event, graph, store):
    user_id = str(event.user_id)
    user = store.user(user_id)
    if not user or not user["enabled"]:
        return "SKIPPED_NOT_ENROLLED"
    saved = next(
        (
            m["content"]
            for m in store.meetings(user_id)
            if m["content"]["meeting_id"] == event.meeting_id
        ),
        None,
    )
    if saved is None:
        return "SKIPPED_UNKNOWN_MEETING"
    path = f"/users/{user_id}/onlineMeetings/{quote(event.meeting_id, safe='')}"
    failed = False
    for kind, resource, model, field in (
        ("transcript", path + "/transcripts", TranscriptEvent, "transcript_id"),
        ("insight", "/copilot" + path + "/aiInsights", InsightEvent, "insight_id"),
    ):
        try:
            known = {item[kind]["id"] for item in saved.get(kind + "s", [])}
            known.update(item[kind].get("source_id") for item in saved.get(kind + "s", []))
            items = await graph.list(resource)
            store.enqueue(
                [
                    model(
                        user_id=user_id, meeting_id=event.meeting_id, **{field: item["id"]}
                    ).model_dump_json()
                    for item in items
                    if item["id"] not in known
                ]
            )
            log.info("Meeting sync user=%s kind=%s available=%s", user_id, kind, len(items))
        except Exception:
            failed = True
            log.warning("Meeting sync user=%s kind=%s failed", user_id, kind)
    if failed:
        raise RuntimeError("Retry meeting sync")
    return "SYNCED"
