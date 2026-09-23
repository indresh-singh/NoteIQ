"""Discover missed transcripts and repair saved meeting artifacts."""

import asyncio
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

# Manual Refresh runs inline so a click brings results back in the same
# request, which means its cost has to be bounded as meetings and users grow:
# - REFRESH_LANES meetings are checked at once for one person, instead of one
#   after another;
# - REFRESH_GRAPH_SLOTS caps those checks across everyone refreshing on one
#   replica, so a crowd of clicks cannot starve the worker's share of the
#   Graph connection pool (GraphClient allows 20);
# - REFRESH_DEADLINE_SECONDS stops a Refresh that is still going, leaving the
#   meetings it did not reach to the background sweep.
# The deadline sits below the browser's Refresh timeout (90s in web/app.js) so
# the answer always arrives, and below REFRESH_LOCK_SECONDS so a Refresh can
# never outlive the per-user lock that stops a second one from starting.
REFRESH_LANES = 4
REFRESH_GRAPH_SLOTS = 8
REFRESH_DEADLINE_SECONDS = 80
REFRESH_LOCK_SECONDS = 120

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

# Which artifact a denial blocked, for a message that names it.
DENIED_MESSAGES = {
    frozenset({"transcript"}): (
        "Microsoft Graph denied access to this meeting's transcript. The meeting may "
        "have expired or its access may be restricted."
    ),
    frozenset({"insight"}): (
        "Microsoft Graph denied access to this meeting's Copilot insights. The meeting "
        "may have expired or its access may be restricted."
    ),
    frozenset({"transcript", "insight"}): ACCESS_DENIED_MESSAGE,
}
MEETING_REJECTED_MESSAGE = (
    "Microsoft Graph rejected this meeting request. See server logs for details."
)
ARTIFACT_REJECTED_MESSAGE = (
    "Microsoft Graph rejected this meeting's artifact request. See server logs for details."
)


def _record_permanent_failure(
    store,
    user_id: str,
    meeting_id: str,
    saved: dict,
    failed: dict[str, bool],
    rejected_message: str,
    metadata: dict | None = None,
) -> str:
    """Stop polling a meeting Graph refuses, warning only about what is missing.

    failed maps each blocked kind ("transcript"/"insight") to whether Graph
    denied it (401/403) rather than rejecting it. Either way the status stays
    terminal, so the sweep stops asking. But a card that already holds that
    kind shows no warning: re-checking a finished meeting -- which Refresh does
    for every recent meeting -- must not put an access alarm above content the
    person can already read. Any insight counts, whichever provider made it.
    """
    held = {kind for kind in ("transcript", "insight") if bodies(saved, kind)}
    missing = {kind: denied for kind, denied in failed.items() if kind not in held}
    denied = {kind for kind, was_denied in failed.items() if was_denied}
    status = "SKIPPED_ACCESS_DENIED" if denied else "SKIPPED_GRAPH_REJECTED"
    missing_denied = frozenset(kind for kind, was_denied in missing.items() if was_denied)
    if not missing:
        message = None
    elif missing_denied:
        message = DENIED_MESSAGES[missing_denied]
    else:
        message = rejected_message
    store.set_meeting_sync_state(user_id, meeting_id, status, message, metadata)
    log.warning(
        "Meeting sync blocked user=%s meeting=%s status=%s blocked_kinds=%s held_kinds=%s "
        "warning_shown=%s",
        user_id,
        digest(meeting_id)[:8],
        status,
        ",".join(sorted(failed)),
        ",".join(sorted(held)) or "-",
        message is not None,
    )
    return status


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


async def discover_meetings(event, graph, store, found: dict | None = None):
    """Queue every transcript Graph holds for this organizer that we do not.

    found, when given, is incremented with how many of those belong to meetings
    NoteIQ has never saved -- what Refresh reports as a new meeting.
    """
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
    new_meetings = set()
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
            if found is not None and store.find_meeting(user_id, meeting_id) is None:
                new_meetings.add(meeting_id)
    store.enqueue(payloads)
    if found is not None:
        found["new_meetings"] += len(new_meetings)
    log.info(
        "Transcript discovery user=%s available=%s queued=%s new_meetings=%s",
        user_id,
        len(items),
        len(payloads),
        len(new_meetings),
    )
    return "DISCOVERED"


def _throttled(error: Exception) -> bool:
    return isinstance(error, httpx.HTTPStatusError) and error.response.status_code == 429


async def sync_now(
    store,
    graph,
    user_id: str,
    *,
    slots: asyncio.Semaphore | None = None,
    deadline: float = REFRESH_DEADLINE_SECONDS,
) -> dict:
    """Check Graph for new meetings and insights immediately, in this request.

    Used by the manual Refresh button so a click reflects Graph's current
    state right away, instead of only queuing a UserSync job for the
    background worker to notice on its next pass. Any transcript/insight this
    finds is still queued for content-fetch (process_transcript/process_insight)
    so retries and backoff keep working the same way they do for webhook-driven
    events; only the "is there anything new?" discovery step runs inline here.

    Returns what the person is told: new_meetings and new_insights, counted
    where they are found rather than inferred from queue depth, which a
    concurrently running worker changes underneath. A new transcript for a
    meeting already on screen is fetched but deliberately not counted.
    complete is False when the deadline or Graph throttling stopped the check
    early; the sweep reaches the rest within minutes.
    """
    started = time.monotonic()
    slots = slots or asyncio.Semaphore(REFRESH_LANES)
    found = {"new_meetings": 0, "new_insights": 0}
    # Deliberately exhaustive, unlike the background sweep: a person clicking
    # Refresh is asking for every recent meeting to be re-checked, including
    # settled ones and ones past the publication window. Newest first, so a
    # deadline cuts off the meetings least likely to have anything new.
    meeting_ids = store.sync_candidates(
        user_id,
        since=time.time() - RECENT_SECONDS,
        within_window=False,
        only_unsettled=False,
    )
    pending = iter(meeting_ids)
    checked = 0

    def paused() -> bool:
        # Set by any 429 -- this Refresh's, the worker's, another replica's.
        return store.graph_paused_until() > time.time()

    # Graph is already throttling the tenant: asking again only extends it.
    throttled = paused()

    async def discover():
        nonlocal throttled
        if throttled:
            return
        try:
            async with slots:
                await discover_meetings(UserSync(user_id=user_id), graph, store, found)
        except Exception as error:
            throttled = throttled or _throttled(error)
            log.exception(
                "Immediate sync discovery failed user=%s error_type=%s error=%s",
                user_id,
                type(error).__name__,
                error,
            )

    async def lane():
        nonlocal checked, throttled
        # Lanes share one iterator, so each meeting is checked exactly once.
        for meeting_id in pending:
            # Graph's throttling is per tenant: once it says slow down, more
            # calls only lengthen the penalty, so the sweep takes over.
            if throttled or paused():
                throttled = True
                return
            try:
                async with slots:
                    await sync_meeting(
                        MeetingSync(user_id=user_id, meeting_id=meeting_id), graph, store, found
                    )
            except Exception as error:
                throttled = throttled or _throttled(error)
                log.exception(
                    "Immediate meeting sync failed user=%s meeting=%s error_type=%s error=%s",
                    user_id,
                    digest(meeting_id)[:8],
                    type(error).__name__,
                    error,
                )
            checked += 1

    timed_out = False
    try:
        async with asyncio.timeout(deadline):
            if not throttled:
                await asyncio.gather(
                    discover(), *(lane() for _ in range(min(REFRESH_LANES, len(meeting_ids))))
                )
    except TimeoutError:
        timed_out = True
    result = {
        **found,
        "checked": checked,
        "total": len(meeting_ids),
        "complete": not timed_out and not throttled,
        "throttled": throttled,
        # How long until Graph allows calls again, for "try again in ..." text.
        "retry_after": max(round(store.graph_paused_until() - time.time()), 0),
    }
    log.info(
        "Refresh completed user=%s checked=%s total=%s new_meetings=%s new_insights=%s "
        "complete=%s timed_out=%s throttled=%s duration_ms=%d",
        user_id,
        checked,
        len(meeting_ids),
        found["new_meetings"],
        found["new_insights"],
        result["complete"],
        timed_out,
        throttled,
        (time.monotonic() - started) * 1000,
    )
    return result


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


async def sync_meeting(event, graph, store, found: dict | None = None):
    """Queue any transcript or insight Graph holds for one meeting that we do not.

    found, when given, is incremented with the new insights -- what Refresh
    reports. New transcripts are queued the same way but not counted.
    """
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
        # Without the meeting, neither artifact can be listed.
        denied = error.response.status_code in {401, 403}
        return _record_permanent_failure(
            store,
            user_id,
            event.meeting_id,
            saved,
            {"transcript": denied, "insight": denied},
            MEETING_REJECTED_MESSAGE,
        )

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
    permanent_errors: dict[str, httpx.HTTPStatusError] = {}
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
            if found is not None and kind == "insight":
                found["new_insights"] += len(fresh)
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
                permanent_errors[kind] = error
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
        statuses = {error.response.status_code for error in permanent_errors.values()}
        status = _record_permanent_failure(
            store,
            user_id,
            event.meeting_id,
            saved,
            {
                kind: error.response.status_code in {401, 403}
                for kind, error in permanent_errors.items()
            },
            ARTIFACT_REJECTED_MESSAGE,
            _meeting_metadata(meeting),
        )
        log.warning(
            "Meeting sync completed terminally user=%s meeting=%s status=%s http_statuses=%s "
            "failed_kinds=%s",
            user_id,
            tag,
            status,
            sorted(statuses),
            ",".join(sorted(permanent_errors)),
        )
        return status
    return "SYNCED"
