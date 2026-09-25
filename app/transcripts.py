"""Save Teams transcript text; Copilot generation happens independently in Microsoft 365.

Whenever OpenRouter is configured, the same transcript text is also sent to
OpenRouter for a second, independent meeting summary and action item list
(see app/openrouter.py) — Copilot and OpenRouter insights are captured side
by side, not as an either/or choice.
"""

import logging

import httpx

from app.activity import INSIGHTS_READY, queue_notification
from app.adaptive_cards import build_card
from app.config import settings
from app.graph_client import GraphClient, retryable
from app.models import MeetingSync, SessionInsightEvent, TranscriptEvent, age_seconds
from app.occurrences import (
    entries,
    requires_sessions,
    select_session,
    session_key,
    sessions,
    transcript_version,
)
from app.openai import OpenAI, OpenAIProviderError
from app.openrouter import OpenRouter
from app.store import Store, digest

log = logging.getLogger(__name__)


def _stored_transcript(content: dict | None, transcript_id: str, metadata: dict) -> dict | None:
    """Match a Graph detail response to an existing historical transcript."""
    if not content:
        return None
    graph_ids = {value for value in (transcript_id, metadata.get("id")) if value}
    correlation = metadata.get("contentCorrelationId")
    for entry in entries(content, "transcript"):
        transcript = entry["transcript"]
        aliases = {value for value in (transcript.get("id"), transcript.get("source_id")) if value}
        if aliases & graph_ids or (
            correlation and transcript.get("contentCorrelationId") == correlation
        ):
            return transcript
    return None


def queue_missing_session_insights(store: Store, user_id: str, meeting_id: str) -> int:
    """Queue one low-priority generation per complete call and provider."""
    content = store.find_meeting(user_id, meeting_id)
    if not content:
        return 0
    transcripts = entries(content, "transcript")
    if not transcripts or any(
        entry["transcript"].get("session_metadata_checked") is not True
        for entry in transcripts
    ):
        return 0
    config = settings()
    payloads = []
    for group in sessions(content):
        version = transcript_version(group)
        for provider in config.external_summary_providers:
            current = any(
                entry["insight"].get("provider") == provider
                and entry["insight"].get("occurrence_id") == group["id"]
                and entry["insight"].get("transcript_version") == version
                for entry in group["insights"]
            )
            if not current:
                payloads.append(
                    SessionInsightEvent(
                        user_id=user_id,
                        meeting_id=meeting_id,
                        occurrence_id=group["id"],
                        provider=provider,
                    ).model_dump_json()
                )
    store.enqueue(payloads)
    if payloads:
        log.info(
            "Session insight repair queued user=%s meeting=%s sessions=%s jobs=%s",
            user_id,
            digest(meeting_id)[:8],
            len(sessions(content)),
            len(payloads),
        )
    return len(payloads)


def meeting_transcript_text(store: Store, user_id: str, content: dict) -> str:
    """Every segment of a meeting's transcript, oldest first, as one document.

    Stopping and restarting transcription splits a meeting into several
    transcripts, and Copilot summarises each in isolation. Joining them first
    lets OpenRouter summarise the meeting as a whole instead of producing one
    partial summary per segment.
    """
    ordered = sorted(
        entries(content, "transcript"),
        key=lambda e: (e.get("transcript") or {}).get("createdDateTime") or "",
    )
    parts = []
    for entry in ordered:
        local_id = (entry.get("transcript") or {}).get("local_id")
        text = store.transcript(user_id, local_id) if local_id else None
        if text:
            parts.append(text)
    return "\n\n".join(parts)


async def summarize_with_openrouter(
    store: Store,
    user_id: str,
    event: TranscriptEvent,
    subject: str,
    text: str,
    occurrence_id: str | None = None,
    source_version: str | None = None,
) -> bool:
    """Generate and save an OpenRouter insight. Returns whether it succeeded.

    Used both as a best-effort step after a transcript is saved (caller
    ignores the result) and by the manual "Regenerate" endpoint in app/web.py,
    which surfaces a failure to the user instead of just logging it.

    The insight is keyed by meeting and call session: a later segment of that
    call replaces its summary without overwriting another day's result.
    """
    try:
        key = event.meeting_id + (":" + occurrence_id if occurrence_id else "")
        insight = await OpenRouter(settings()).summarize(key, subject, text)
    except ValueError as error:
        # These messages are hand-written in app/openrouter.py and never include
        # transcript content or secrets, so it's safe to log the reason directly.
        log.warning(
            "OpenRouter summary user=%s meeting=%s failed reason=%s",
            user_id,
            event.meeting_id,
            error,
        )
        return False
    card = build_card(insight, subject, source=f"OpenRouter ({settings().openrouter_model})")
    if card is None:
        return False
    saved = store.save_meeting(
        user_id,
        subject,
        {
            "meeting_id": event.meeting_id,
            "insight": {
                **insight.model_dump(mode="json"),
                "source_id": insight.id,
                "provider": "openrouter",
                "occurrence_id": occurrence_id,
                "transcript_version": source_version,
            },
            "card": card,
        },
    )
    if saved is False:
        return False
    queue_notification(store, user_id, f"insight:{key}", subject, INSIGHTS_READY)
    return True


async def summarize_with_ai(
    store: Store,
    user_id: str,
    event: TranscriptEvent,
    subject: str,
    text: str,
    provider: str | None = None,
    raise_on_failure: bool = False,
    occurrence_id: str | None = None,
    source_version: str | None = None,
) -> bool:
    """Generate an insight with one explicitly selected external provider."""
    config = settings()
    provider = provider or config.summary_provider
    if provider == "openrouter" and config.openrouter_enabled:
        return await summarize_with_openrouter(
            store,
            user_id,
            event,
            subject,
            text,
            occurrence_id=occurrence_id,
            source_version=source_version,
        )
    if provider != "openai" or not config.openai_enabled:
        return False
    try:
        key = event.meeting_id + (":" + occurrence_id if occurrence_id else "")
        insight = await OpenAI(config).summarize(key, subject, text)
    except OpenAIProviderError as error:
        log.warning(
            "OpenAI summary failed error_code=%s user=%s meeting=%s reason=%s",
            error.code,
            user_id,
            event.meeting_id,
            error,
        )
        if raise_on_failure:
            raise
        return False
    card = build_card(insight, subject, source=f"OpenAI ({config.openai_model})")
    if card is None:
        return False
    saved = store.save_meeting(
        user_id,
        subject,
        {
            "meeting_id": event.meeting_id,
            "insight": {
                **insight.model_dump(mode="json"),
                "source_id": insight.id,
                "provider": "openai",
                "occurrence_id": occurrence_id,
                "transcript_version": source_version,
            },
            "card": card,
        },
    )
    if saved is False:
        return False
    queue_notification(store, user_id, f"insight:{key}", subject, INSIGHTS_READY)
    return True


async def process_session_insight(
    event: SessionInsightEvent, graph: GraphClient, store: Store
) -> str:
    """Generate one provider result from the final, fully repaired call."""
    del graph
    user_id = str(event.user_id)
    user = store.user(user_id)
    if not user or not user["enabled"]:
        return "SKIPPED_NOT_ENROLLED"
    meeting = store.find_meeting_row(user_id, event.meeting_id)
    if not meeting:
        return "SKIPPED_UNKNOWN_MEETING"
    try:
        selected = select_session(meeting["content"], event.occurrence_id)
    except (KeyError, ValueError):
        return "SKIPPED_SESSION_CHANGED"
    version = transcript_version(selected)
    if any(
        entry["insight"].get("provider") == event.provider
        and entry["insight"].get("occurrence_id") == event.occurrence_id
        and entry["insight"].get("transcript_version") == version
        for entry in selected.get("insights", [])
    ):
        return "SKIPPED_CURRENT"
    text = meeting_transcript_text(store, user_id, selected)
    if not text:
        return "SKIPPED_NO_TRANSCRIPT"
    transcripts = selected.get("transcripts") or []
    latest = transcripts[-1]["transcript"]
    ok = await summarize_with_ai(
        store,
        user_id,
        TranscriptEvent(
            user_id=user_id,
            meeting_id=event.meeting_id,
            transcript_id=latest["id"],
        ),
        meeting["subject"],
        text,
        provider=event.provider,
        raise_on_failure=True,
        occurrence_id=event.occurrence_id,
        source_version=version,
    )
    if not ok:
        raise RuntimeError("Session insight generation did not save a result")
    return "SESSION_INSIGHT_SAVED"


async def process_transcript(event: TranscriptEvent, graph: GraphClient, store: Store) -> str:
    user_id = str(event.user_id)
    user = store.user(user_id)
    if not user or not user["enabled"]:
        return "SKIPPED_NOT_ENROLLED"
    try:
        meeting = await graph.request("GET", event.meeting_path)
        organizer = (meeting.get("participants") or {}).get("organizer") or {}
        owner = ((organizer.get("identity") or {}).get("user") or {}).get("id", "")
        if not owner:
            return "NEEDS_REVIEW"
        if owner.lower() != user_id:
            return "SKIPPED_NOT_ORGANIZER"
        metadata = await graph.request("GET", event.transcript_path)
        transcript_lag = age_seconds(metadata.get("createdDateTime"))
        log.info(
            "Transcript user=%s meeting=%s publish_lag_s=%s",
            user_id,
            digest(event.meeting_id)[:8],
            "unknown" if transcript_lag is None else round(transcript_lag),
        )
        saved_before = store.find_meeting(user_id, event.meeting_id)
        historical = _stored_transcript(saved_before, event.transcript_id, metadata)
        if (
            historical is not None
            and historical.get("session_metadata_checked") is True
            and historical.get("local_id") is not None
        ):
            # A notification or a repair queued by an older deployment may
            # arrive after this artifact was already repaired. Reuse its saved
            # text and avoid another content download and AI generation.
            queue_missing_session_insights(store, user_id, event.meeting_id)
            return "SKIPPED_TRANSCRIPT_CURRENT"
        metadata_only = event.metadata_only or (
            historical is not None
            and historical.get("session_metadata_checked") is not True
            and historical.get("local_id") is not None
        )
        subject = meeting.get("subject") or "Teams meeting"
        if metadata_only and historical is not None:
            repaired = {
                **historical,
                "source_id": metadata.get("id") or event.transcript_id,
                "createdDateTime": metadata.get("createdDateTime")
                or historical.get("createdDateTime"),
                "endDateTime": metadata.get("endDateTime") or historical.get("endDateTime"),
                "callId": metadata.get("callId") or historical.get("callId"),
                "session_metadata_checked": True,
                "contentCorrelationId": metadata.get("contentCorrelationId")
                or historical.get("contentCorrelationId"),
            }
            repaired.pop("session_metadata_status", None)
            store.save_meeting(
                user_id,
                subject,
                {
                    "meeting_id": event.meeting_id,
                    "meeting_metadata": {
                        "meeting_type": meeting.get("meetingType"),
                        "start_date_time": meeting.get("startDateTime"),
                        "end_date_time": meeting.get("endDateTime"),
                    },
                    "transcript": repaired,
                },
            )
            queue_missing_session_insights(store, user_id, event.meeting_id)
            return "SESSION_METADATA_REPAIRED"
        try:
            text = await graph.request(
                "GET",
                event.transcript_path + "/content",
                text=True,
                headers={"Accept": "text/vtt"},
            )
        except httpx.HTTPStatusError as error:
            # Respect tenant policy: fall back to content without speaker names only for this error.
            try:
                code = error.response.json().get("error", {}).get("innerError", {}).get("code")
            except ValueError:
                code = None
            if error.response.status_code != 403 or code != "SpeakerAttributionNotAllowed":
                raise
            text = await graph.request(
                "GET",
                event.transcript_path + "/content",
                text=True,
                headers={"Accept": "application/vnd.microsoft.graph.transcript+text"},
            )
        local_id = store.save_transcript(user_id, event.meeting_id, event.transcript_id, text)
        if local_id is None:
            return "SKIPPED_NOT_ENROLLED"
        store.save_meeting(
            user_id,
            subject,
            {
                "meeting_id": event.meeting_id,
                "meeting_metadata": {
                    "meeting_type": meeting.get("meetingType"),
                    "start_date_time": meeting.get("startDateTime"),
                    "end_date_time": meeting.get("endDateTime"),
                },
                "transcript": {
                    "id": event.transcript_id,
                    # getAllTranscripts and a meeting's own /transcripts list can
                    # name the same transcript differently. Record both so the
                    # next sync recognises it instead of fetching and
                    # summarising it a second time under the other id.
                    "source_id": metadata.get("id"),
                    "local_id": local_id,
                    "createdDateTime": metadata.get("createdDateTime"),
                    "endDateTime": metadata.get("endDateTime"),
                    "callId": metadata.get("callId"),
                    "session_metadata_checked": True,
                    "contentCorrelationId": metadata.get("contentCorrelationId"),
                },
            },
        )
        store.enqueue([MeetingSync(user_id=user_id, meeting_id=event.meeting_id).model_dump_json()])
        config = settings()
        if config.external_ai_enabled:
            # Re-read and scope to this call, including restarted transcription
            # segments but excluding every other call under the same umbrella.
            saved = store.find_meeting(user_id, event.meeting_id)
            occurrence_id = None
            if saved and requires_sessions(saved):
                occurrence_id = session_key(
                    {**metadata, "id": event.transcript_id, "source_id": metadata.get("id")}
                )
                saved = select_session(saved, occurrence_id)
            whole = meeting_transcript_text(store, user_id, saved) if saved else text
            for provider in config.external_summary_providers:
                await summarize_with_ai(
                    store,
                    user_id,
                    event,
                    subject,
                    whole or text,
                    provider=provider,
                    occurrence_id=occurrence_id,
                    source_version=transcript_version(saved) if saved else None,
                )
        return "TRANSCRIPT_SAVED"
    except httpx.HTTPStatusError as error:
        if retryable(error) or error.response.status_code == 404:
            log.warning(
                "Transcript retrieval will retry user=%s meeting=%s http_status=%s",
                user_id,
                digest(event.meeting_id)[:8],
                error.response.status_code,
            )
            raise RuntimeError("Retry transcript retrieval") from error
        store.status(user_id, "ACCESS_REQUIRED")
        # GraphClient logged the sanitized response; HTTPStatusError text is
        # not repeated here because providers may put sensitive body data in it.
        log.warning(
            "Transcript retrieval permanently failed user=%s meeting=%s http_status=%s",
            user_id,
            digest(event.meeting_id)[:8],
            error.response.status_code,
        )
        return "FAILED_PERMANENT"
