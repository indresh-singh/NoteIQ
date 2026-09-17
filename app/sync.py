"""Discover missed transcripts and repair saved meeting artifacts."""

import logging
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import httpx

from app.config import settings
from app.meetings import meeting_filter
from app.models import InsightEvent, MeetingSync, TranscriptEvent, UserSync

log = logging.getLogger(__name__)


def queue_sync(store, user_id, *, discover=False):
    meetings = store.meetings(user_id)
    payloads = [
        MeetingSync(user_id=user_id, meeting_id=m["content"]["meeting_id"]).model_dump_json()
        for m in meetings
        if m["created"] >= time.time() - 7 * 86400 and m["content"].get("source") != "upload"
    ]
    if discover:
        payloads.append(UserSync(user_id=user_id).model_dump_json())
    store.enqueue(payloads)
    return len(payloads)


async def discover_meetings(event, graph, store):
    user_id = str(event.user_id)
    user = store.user(user_id)
    if not user or not user["enabled"]:
        return "SKIPPED_NOT_ENROLLED"
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=7)
    path = (
        f"/users/{user_id}/onlineMeetings/getAllTranscripts("
        f"meetingOrganizerUserId='{user_id}',"
        f"startDateTime={start.strftime('%Y-%m-%dT%H:%M:%SZ')},"
        f"endDateTime={end.strftime('%Y-%m-%dT%H:%M:%SZ')})"
    )
    try:
        items = await graph.list(path)
    except httpx.HTTPStatusError as error:
        if error.response.status_code in {401, 403}:
            store.status(user_id, "ACCESS_REQUIRED")
        log.warning(
            "Transcript discovery user=%s http_status=%s", user_id, error.response.status_code
        )
        raise
    known = {
        (m["content"]["meeting_id"], item["transcript"]["id"])
        for m in store.meetings(user_id)
        for item in m["content"].get("transcripts", [])
    }
    payloads = []
    for item in items:
        owner = ((item.get("meetingOrganizer") or {}).get("user") or {}).get("id")
        if owner and owner.lower() != user_id:
            continue
        meeting_id, transcript_id = item.get("meetingId"), item.get("id")
        if not meeting_id or not transcript_id:
            log.warning("Transcript discovery user=%s missing artifact identifiers", user_id)
            continue
        if (meeting_id, transcript_id) not in known:
            payloads.append(
                TranscriptEvent(
                    user_id=user_id, meeting_id=meeting_id, transcript_id=transcript_id
                ).model_dump_json()
            )
    store.enqueue(payloads)
    log.info(
        "Transcript discovery user=%s available=%s queued=%s", user_id, len(items), len(payloads)
    )
    return "DISCOVERED"


async def recover_from_link(store, graph, user_id, meeting_url):
    """Seed a missed meeting from its Teams join link, then fetch its artifacts."""
    meetings = await graph.list(
        f"/users/{user_id}/onlineMeetings?$filter=" + quote(meeting_filter(meeting_url), safe="")
    )
    for meeting in meetings:
        if meeting.get("id"):
            store.save_meeting(
                user_id,
                meeting.get("subject") or "Teams meeting",
                {"meeting_id": meeting["id"]},
            )
    queued = queue_sync(store, user_id)
    return {"found": len(meetings), "queued": queued}


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
    kinds = [("transcript", path + "/transcripts", TranscriptEvent, "transcript_id")]
    if settings().ai_provider == "copilot":
        kinds.append(("insight", "/copilot" + path + "/aiInsights", InsightEvent, "insight_id"))
    for kind, resource, model, field in kinds:
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
