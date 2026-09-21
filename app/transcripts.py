"""Save Teams transcript text; Copilot generation happens independently in Microsoft 365.

Whenever OpenRouter is configured, the same transcript text is also sent to
OpenRouter for a second, independent meeting summary and action item list
(see app/openrouter.py) — Copilot and OpenRouter insights are captured side
by side, not as an either/or choice.
"""

import logging

import httpx

from app.activity import INSIGHTS_READY, TRANSCRIPT_READY, queue_notification
from app.adaptive_cards import build_card
from app.config import settings
from app.graph_client import GraphClient, retryable
from app.models import MeetingSync, TranscriptEvent
from app.openrouter import OpenRouter
from app.store import Store

log = logging.getLogger(__name__)


async def summarize_with_openrouter(
    store: Store, user_id: str, event: TranscriptEvent, subject: str, text: str
) -> bool:
    """Generate and save an OpenRouter insight. Returns whether it succeeded.

    Used both as a best-effort step after a transcript is saved (caller
    ignores the result) and by the manual "Regenerate" endpoint in app/web.py,
    which surfaces a failure to the user instead of just logging it.
    """
    try:
        insight = await OpenRouter(settings()).summarize(event.transcript_id, subject, text)
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
    store.save_meeting(
        user_id,
        subject,
        {
            "meeting_id": event.meeting_id,
            "insight": {
                **insight.model_dump(mode="json"),
                "source_id": insight.id,
                "provider": "openrouter",
            },
            "card": card,
        },
    )
    queue_notification(
        store, user_id, f"insight:{event.meeting_id}:{insight.id}", subject, INSIGHTS_READY
    )
    return True


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
        subject = meeting.get("subject") or "Teams meeting"
        store.save_meeting(
            user_id,
            subject,
            {
                "meeting_id": event.meeting_id,
                "transcript": {
                    "id": event.transcript_id,
                    "local_id": local_id,
                    "createdDateTime": metadata.get("createdDateTime"),
                    "contentCorrelationId": metadata.get("contentCorrelationId"),
                },
            },
        )
        store.enqueue([MeetingSync(user_id=user_id, meeting_id=event.meeting_id).model_dump_json()])
        queue_notification(
            store,
            user_id,
            f"transcript:{event.meeting_id}:{event.transcript_id}",
            subject,
            TRANSCRIPT_READY,
        )
        if settings().openrouter_enabled:
            await summarize_with_openrouter(store, user_id, event, subject, text)
        return "TRANSCRIPT_SAVED"
    except httpx.HTTPStatusError as error:
        if retryable(error) or error.response.status_code == 404:
            raise RuntimeError("Retry transcript retrieval") from None
        store.status(user_id, "ACCESS_REQUIRED")
        log.warning("Graph transcript status=%s", error.response.status_code)
        return "FAILED_PERMANENT"
