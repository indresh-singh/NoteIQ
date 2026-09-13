"""Save Teams transcript text; Copilot generation happens independently in Microsoft 365."""

import logging

import httpx

from app.activity import TRANSCRIPT_READY, queue_notification
from app.graph_client import GraphClient, retryable
from app.models import TranscriptEvent
from app.store import Store

log = logging.getLogger(__name__)


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
        queue_notification(
            store,
            user_id,
            f"transcript:{event.meeting_id}:{event.transcript_id}",
            subject,
            TRANSCRIPT_READY,
        )
        return "TRANSCRIPT_SAVED"
    except httpx.HTTPStatusError as error:
        if retryable(error) or error.response.status_code == 404:
            raise RuntimeError("Retry transcript retrieval") from None
        store.status(user_id, "ACCESS_REQUIRED")
        log.warning("Graph transcript status=%s", error.response.status_code)
        return "FAILED_PERMANENT"
