"""Discover missed transcripts and repair saved meeting artifacts."""

import logging
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import httpx

from app.meetings import meeting_filter
from app.models import InsightEvent, MeetingSync, TranscriptEvent, UserSync
from app.store import (
    PUBLICATION_WINDOW_HOURS,
    bodies,
    digest,
    newest_transcript_at,
    settled,
)

log = logging.getLogger(__name__)

# How far back any sweep or manual refresh looks. Graph's own transcript
# discovery is bounded to the same window.
RECENT_SECONDS = 7 * 86400

# bodies and settled live in app.store, which computes them once per write into
# the columns the sweep reads; re-exported here because this is where the rules
# they express are documented.
__all__ = [
    "PUBLICATION_WINDOW_HOURS",
    "bodies",
    "discover_meetings",
    "past_publication_window",
    "queue_sync",
    "recover_from_link",
    "settled",
    "sync_meeting",
    "sync_now",
]


def past_publication_window(content: dict, hours: float = PUBLICATION_WINDOW_HOURS) -> bool:
    """True once even the newest transcript is older than Copilot's stated window.

    Conservative by construction: a meeting with no transcript yet, or with
    timestamps we cannot read, keeps polling rather than being abandoned. The
    sweep applies this same rule in SQL against the stored
    newest_transcript_at column, which is why both read it from one function.
    """
    newest = newest_transcript_at(content)
    return newest is not None and time.time() - newest > hours * 3600


def queue_sync(store, user_id, *, discover=False, within_window=True):
    """Queue a poll for each meeting still worth polling.

    within_window=False drops the publication-window bound for the manual
    recovery path, whose whole purpose is reaching meetings old enough that the
    background sweep has given up on them.
    """
    payloads = [
        MeetingSync(user_id=user_id, meeting_id=meeting_id).model_dump_json()
        for meeting_id in store.sync_candidates(
            user_id, since=time.time() - RECENT_SECONDS, within_window=within_window
        )
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
    # Match on every id a saved transcript is known by. getAllTranscripts names
    # transcripts differently from a meeting's own /transcripts list, so keying
    # on one id alone re-fetches -- and re-summarises -- what we already hold.
    known = store.transcript_aliases(user_id)
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


async def sync_now(store, graph, user_id: str) -> int:
    """Check Graph for new transcripts/insights immediately, in this request.

    Used by the manual Refresh button so a click reflects Graph's current
    state right away, instead of only queuing a UserSync job for the
    background worker to notice on its next pass. Any transcript/insight this
    finds is still queued for content-fetch (process_transcript/process_insight)
    so retries and backoff keep working the same way they do for webhook-driven
    events; only the "is there anything new?" discovery step runs inline here.
    """
    before = store.pending_job_count()
    try:
        await discover_meetings(UserSync(user_id=user_id), graph, store)
    except Exception as error:
        log.exception(
            "Immediate sync discovery failed user=%s error_type=%s error=%s",
            user_id,
            type(error).__name__,
            error,
        )
    # Deliberately exhaustive, unlike the background sweep: a person clicking
    # Refresh is asking for every recent meeting to be re-checked, including
    # settled ones and ones past the publication window. Only the reading of it
    # is cheaper now -- the candidate list comes from indexed columns instead of
    # every saved card.
    for meeting_id in store.sync_candidates(
        user_id,
        since=time.time() - RECENT_SECONDS,
        within_window=False,
        only_unsettled=False,
    ):
        try:
            await sync_meeting(MeetingSync(user_id=user_id, meeting_id=meeting_id), graph, store)
        except Exception as error:
            log.exception(
                "Immediate meeting sync failed user=%s meeting=%s error_type=%s error=%s",
                user_id,
                digest(meeting_id)[:8],
                type(error).__name__,
                error,
            )
    return store.pending_job_count() - before


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
    queued = queue_sync(store, user_id, within_window=False)
    return {"found": len(meetings), "queued": queued}


async def sync_meeting(event, graph, store):
    user_id = str(event.user_id)
    user = store.user(user_id)
    if not user or not user["enabled"]:
        return "SKIPPED_NOT_ENROLLED"
    saved = store.find_meeting(user_id, event.meeting_id)
    if saved is None:
        return "SKIPPED_UNKNOWN_MEETING"
    path = f"/users/{user_id}/onlineMeetings/{quote(event.meeting_id, safe='')}"
    tag = digest(event.meeting_id)[:8]
    failed = False
    # Copilot insight sync runs regardless of AI_PROVIDER: Copilot and OpenRouter
    # insights are captured side by side, not as an either/or choice.
    kinds = [
        ("transcript", path + "/transcripts", TranscriptEvent, "transcript_id"),
        ("insight", "/copilot" + path + "/aiInsights", InsightEvent, "insight_id"),
    ]
    for kind, resource, model, field in kinds:
        try:
            known = {item[kind]["id"] for item in saved.get(kind + "s", [])}
            known.update(item[kind].get("source_id") for item in saved.get(kind + "s", []))
            items = await graph.list(resource)
            fresh = [item["id"] for item in items if item["id"] not in known]
            store.enqueue(
                [
                    model(
                        user_id=user_id, meeting_id=event.meeting_id, **{field: item_id}
                    ).model_dump_json()
                    for item_id in fresh
                ]
            )
            log.info(
                "Meeting sync user=%s meeting=%s kind=%s available=%s new=%s",
                user_id,
                tag,
                kind,
                len(items),
                len(fresh),
            )
            if fresh:
                # The first poll that sees an artifact brackets Microsoft's
                # publication time to within one polling interval. Pairing this
                # with the preceding available=0 line is what separates their
                # lag from ours.
                log.info(
                    "Artifact first visible user=%s meeting=%s kind=%s count=%s",
                    user_id,
                    tag,
                    kind,
                    len(fresh),
                )
        except Exception as error:
            failed = True
            log.exception(
                "Meeting sync failed user=%s meeting=%s kind=%s resource=%s error_type=%s error=%s",
                user_id,
                tag,
                kind,
                resource,
                type(error).__name__,
                error,
            )
    if failed:
        raise RuntimeError("Retry meeting sync")
    return "SYNCED"
