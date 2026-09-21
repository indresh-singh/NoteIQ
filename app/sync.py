"""Discover missed transcripts and repair saved meeting artifacts."""

import logging
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import httpx

from app.graph_client import retryable
from app.meetings import meeting_filter
from app.models import InsightEvent, MeetingSync, TranscriptEvent, UserSync, age_seconds
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

NOT_ORGANIZER_MESSAGE = (
    "This meeting was found, but you are not its organizer. NoteIQ can only retrieve "
    "transcripts and Copilot insights for meetings you organize."
)
ORGANIZER_UNVERIFIED_MESSAGE = (
    "NoteIQ found the meeting but Microsoft Graph did not identify its organizer, so "
    "the transcript and Copilot insights were not requested."
)
ACCESS_DENIED_MESSAGE = (
    "Microsoft Graph denied access to this meeting's transcript or Copilot insights. "
    "The meeting may have expired or its access may be restricted."
)

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


def _organizer_id(meeting: dict) -> str:
    organizer = (meeting.get("participants") or {}).get("organizer") or {}
    return str(((organizer.get("identity") or {}).get("user") or {}).get("id") or "")


def _meeting_metadata(meeting: dict) -> dict:
    """Return useful, non-content meeting fields for the UI and diagnostics."""
    return {
        "organizer_id": _organizer_id(meeting) or None,
        "meeting_type": meeting.get("meetingType") or meeting.get("type"),
        "odata_type": meeting.get("@odata.type"),
        "creation_date_time": meeting.get("creationDateTime"),
        "start_date_time": meeting.get("startDateTime"),
        "end_date_time": meeting.get("endDateTime"),
        "expiration_date_time": meeting.get("expirationDateTime"),
    }


def _log_meeting_details(user_id: str, meeting_id: str, meeting: dict, source: str) -> None:
    metadata = _meeting_metadata(meeting)
    organizer_id = metadata["organizer_id"] or "-"
    end_age = age_seconds(metadata["end_date_time"])
    expiration_age = age_seconds(metadata["expiration_date_time"])
    log.info(
        "Meeting details source=%s requested_user_id=%s organizer_id=%s organizer_match=%s "
        "meeting=%s meeting_type=%s odata_type=%s creation_time=%s start_time=%s "
        "end_time=%s expiration_time=%s seconds_since_end=%s seconds_since_expiration=%s "
        "allowed_presenters=%s record_automatically=%s",
        source,
        user_id,
        organizer_id,
        organizer_id.lower() == user_id.lower() if organizer_id != "-" else "unknown",
        digest(meeting_id)[:8],
        metadata["meeting_type"] or "-",
        metadata["odata_type"] or "-",
        metadata["creation_date_time"] or "-",
        metadata["start_date_time"] or "-",
        metadata["end_date_time"] or "-",
        metadata["expiration_date_time"] or "-",
        round(end_age) if end_age is not None else "unknown",
        round(expiration_age) if expiration_age is not None else "unknown",
        meeting.get("allowedPresenters", "-"),
        meeting.get("recordAutomatically", "-"),
    )


def _http_error_fields(error: httpx.HTTPStatusError) -> tuple[str | None, str | None]:
    try:
        body = error.response.json().get("error") or {}
    except (AttributeError, ValueError):
        body = {}
    return body.get("code"), (body.get("innerError") or {}).get("code")


def _log_sync_http_failure(
    *, user_id: str, meeting_id: str, phase: str, error: httpx.HTTPStatusError
) -> None:
    code, inner_code = _http_error_fields(error)
    log.warning(
        "Meeting sync Graph rejection user=%s meeting=%s phase=%s http_status=%s "
        "retryable=%s graph_code=%s inner_code=%s request_id=%s client_request_id=%s",
        user_id,
        digest(meeting_id)[:8],
        phase,
        error.response.status_code,
        retryable(error),
        code,
        inner_code,
        error.response.headers.get("request-id", "-"),
        error.response.headers.get("client-request-id", "-"),
    )


async def recover_from_link(store, graph, user_id, meeting_url):
    """Seed a missed meeting from its Teams join link, then fetch its artifacts."""
    meetings = await graph.list(
        f"/users/{user_id}/onlineMeetings?$filter=" + quote(meeting_filter(meeting_url), safe="")
    )
    owned = []
    skipped_not_organizer = 0
    skipped_unverified = 0
    skipped_access_denied = 0
    for meeting in meetings:
        meeting_id = meeting.get("id")
        if not meeting_id:
            log.warning(
                "Meeting recovery result rejected user=%s reason=missing_meeting_id", user_id
            )
            skipped_unverified += 1
            continue
        # Graph list shapes can omit participants. Fetch the canonical meeting
        # before deciding ownership rather than treating a missing field as a match.
        if not _organizer_id(meeting):
            path = f"/users/{user_id}/onlineMeetings/{quote(meeting_id, safe='')}"
            try:
                detail = await graph.request("GET", path)
            except httpx.HTTPStatusError as error:
                _log_sync_http_failure(
                    user_id=user_id,
                    meeting_id=meeting_id,
                    phase="recovery_meeting",
                    error=error,
                )
                if error.response.status_code not in {401, 403}:
                    raise
                skipped_access_denied += 1
                log.warning(
                    "Meeting recovery rejected requested_user_id=%s meeting=%s "
                    "reason=access_denied http_status=%s ui_message=%r",
                    user_id,
                    digest(meeting_id)[:8],
                    error.response.status_code,
                    ACCESS_DENIED_MESSAGE,
                )
                continue
            # Preserve list fields (notably subject) when a narrowed/mock detail
            # response contains only the organizer fields needed for validation.
            meeting = {**meeting, **detail}
        _log_meeting_details(user_id, meeting_id, meeting, "recovery")
        owner = _organizer_id(meeting)
        if not owner:
            skipped_unverified += 1
            log.warning(
                "Meeting recovery rejected requested_user_id=%s organizer_id=- meeting=%s "
                "reason=organizer_unverified",
                user_id,
                digest(meeting_id)[:8],
            )
            continue
        if owner.lower() != user_id.lower():
            skipped_not_organizer += 1
            log.warning(
                "Meeting recovery rejected requested_user_id=%s organizer_id=%s meeting=%s "
                "reason=requested_user_is_not_organizer ui_message=%r",
                user_id,
                owner,
                digest(meeting_id)[:8],
                NOT_ORGANIZER_MESSAGE,
            )
            continue
        owned.append(meeting_id)
        store.save_meeting(
            user_id,
            meeting.get("subject") or "Teams meeting",
            {"meeting_id": meeting_id, "meeting_metadata": _meeting_metadata(meeting)},
        )
    queued = queue_sync(store, user_id, within_window=False) if owned else 0
    result = {"found": len(meetings), "queued": queued}
    if skipped_not_organizer or skipped_unverified or skipped_access_denied:
        result.update(
            {
                "eligible": len(owned),
                "skipped_not_organizer": skipped_not_organizer,
                "skipped_organizer_unverified": skipped_unverified,
                "skipped_access_denied": skipped_access_denied,
                "message": (
                    NOT_ORGANIZER_MESSAGE
                    if skipped_not_organizer
                    else (
                        ACCESS_DENIED_MESSAGE
                        if skipped_access_denied
                        else ORGANIZER_UNVERIFIED_MESSAGE
                    )
                ),
            }
        )
    log.info(
        "Meeting recovery completed user=%s found=%s owned=%s skipped_not_organizer=%s "
        "skipped_organizer_unverified=%s skipped_access_denied=%s queued=%s",
        user_id,
        len(meetings),
        len(owned),
        skipped_not_organizer,
        skipped_unverified,
        skipped_access_denied,
        queued,
    )
    return result


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
    try:
        meeting = await graph.request("GET", path)
    except httpx.HTTPStatusError as error:
        _log_sync_http_failure(
            user_id=user_id, meeting_id=event.meeting_id, phase="meeting", error=error
        )
        if retryable(error):
            raise
        denied = error.response.status_code in {401, 403}
        status = "SKIPPED_ACCESS_DENIED" if denied else "SKIPPED_GRAPH_REJECTED"
        message = (
            ACCESS_DENIED_MESSAGE
            if denied
            else "Microsoft Graph rejected this meeting request. See server logs for details."
        )
        store.set_meeting_sync_state(user_id, event.meeting_id, status, message)
        return status

    _log_meeting_details(user_id, event.meeting_id, meeting, "sync")
    owner = _organizer_id(meeting)
    if not owner:
        store.set_meeting_sync_state(
            user_id,
            event.meeting_id,
            "SKIPPED_ORGANIZER_UNVERIFIED",
            ORGANIZER_UNVERIFIED_MESSAGE,
            _meeting_metadata(meeting),
        )
        log.warning(
            "Meeting artifact retrieval skipped requested_user_id=%s organizer_id=- meeting=%s "
            "reason=organizer_unverified ui_message=%r",
            user_id,
            tag,
            ORGANIZER_UNVERIFIED_MESSAGE,
        )
        return "SKIPPED_ORGANIZER_UNVERIFIED"
    if owner.lower() != user_id.lower():
        store.set_meeting_sync_state(
            user_id,
            event.meeting_id,
            "SKIPPED_NOT_ORGANIZER",
            NOT_ORGANIZER_MESSAGE,
            _meeting_metadata(meeting),
        )
        log.warning(
            "Meeting artifact retrieval skipped requested_user_id=%s organizer_id=%s meeting=%s "
            "reason=requested_user_is_not_organizer ui_message=%r",
            user_id,
            owner,
            tag,
            NOT_ORGANIZER_MESSAGE,
        )
        return "SKIPPED_NOT_ORGANIZER"
    store.set_meeting_sync_state(
        user_id, event.meeting_id, None, metadata=_meeting_metadata(meeting)
    )
    transient_errors = []
    permanent_errors = []
    # Copilot insight sync always runs: Copilot and external-service insights
    # are captured side by side.
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
        except httpx.HTTPStatusError as error:
            _log_sync_http_failure(
                user_id=user_id, meeting_id=event.meeting_id, phase=kind, error=error
            )
            if retryable(error):
                transient_errors.append(error)
            else:
                permanent_errors.append(error)
            log.exception(
                "Meeting sync failed user=%s meeting=%s kind=%s resource=%s error_type=%s error=%s",
                user_id,
                tag,
                kind,
                resource,
                type(error).__name__,
                error,
            )
        except httpx.TransportError as error:
            transient_errors.append(error)
            log.exception(
                "Meeting sync transport failure user=%s meeting=%s kind=%s resource=%s "
                "retryable=true error_type=%s error=%s",
                user_id,
                tag,
                kind,
                resource,
                type(error).__name__,
                error,
            )
        except Exception as error:
            # Unknown failures are retried instead of being silently declared
            # permanent. Preserve the original exception and traceback rather
            # than replacing it with an uninformative RuntimeError.
            transient_errors.append(error)
            log.exception(
                "Meeting sync unexpected failure user=%s meeting=%s kind=%s resource=%s "
                "retryable=unknown error_type=%s error=%s",
                user_id,
                tag,
                kind,
                resource,
                type(error).__name__,
                error,
            )
    if transient_errors:
        log.warning(
            "Meeting sync deferred user=%s meeting=%s transient_failures=%s "
            "permanent_failures=%s successful_kinds=%s",
            user_id,
            tag,
            len(transient_errors),
            len(permanent_errors),
            len(kinds) - len(transient_errors) - len(permanent_errors),
        )
        raise transient_errors[0]
    if permanent_errors:
        statuses = {error.response.status_code for error in permanent_errors}
        denied = bool(statuses & {401, 403})
        status = "SKIPPED_ACCESS_DENIED" if denied else "SKIPPED_GRAPH_REJECTED"
        message = (
            ACCESS_DENIED_MESSAGE
            if denied
            else "Microsoft Graph rejected this meeting's artifact request. See server logs for details."
        )
        store.set_meeting_sync_state(
            user_id, event.meeting_id, status, message, _meeting_metadata(meeting)
        )
        log.warning(
            "Meeting sync completed terminally user=%s meeting=%s status=%s http_statuses=%s "
            "failed_kinds=%s",
            user_id,
            tag,
            status,
            sorted(statuses),
            len(permanent_errors),
        )
        return status
    return "SYNCED"
