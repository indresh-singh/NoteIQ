import logging
import time

import httpx
from pydantic import ValidationError

from app.activity import INSIGHTS_READY, queue_notification
from app.adaptive_cards import build_card
from app.config import settings
from app.graph_client import GraphClient, retryable
from app.models import Insight, InsightEvent, MeetingSync
from app.store import Store

log = logging.getLogger(__name__)


async def process_insight(event: InsightEvent, graph: GraphClient, store: Store) -> str:
    started = time.monotonic()
    status = "PROCESSING"
    user_id = str(event.user_id)
    try:
        user = store.user(user_id)
        if not user or not user["enabled"]:
            status = "SKIPPED_NOT_ENROLLED"
            return status
        if settings().ai_provider != "copilot":
            status = "SKIPPED_PROVIDER_DISABLED"
            return status
        meeting = await graph.request("GET", event.meeting_path)
        organizer = (meeting.get("participants") or {}).get("organizer") or {}
        organizer_id = ((organizer.get("identity") or {}).get("user") or {}).get("id")
        if not organizer_id:
            status = "NEEDS_REVIEW"
        elif organizer_id.lower() != user_id:
            status = "SKIPPED_NOT_ORGANIZER"
        else:
            insight = Insight.model_validate(await graph.request("GET", event.insight_path))
            subject = meeting.get("subject") or "Teams meeting"
            card = build_card(insight, subject)
            if card is None:
                status = "SKIPPED_EMPTY"
            else:
                store.save_meeting(
                    user_id,
                    subject,
                    {
                        "meeting_id": event.meeting_id,
                        "insight": {
                            **insight.model_dump(mode="json"),
                            "source_id": event.insight_id,
                            "provider": "copilot",
                        },
                        "card": card,
                    },
                )
                status = "SAVED"
                store.enqueue(
                    [MeetingSync(user_id=user_id, meeting_id=event.meeting_id).model_dump_json()]
                )
                queue_notification(
                    store,
                    user_id,
                    f"insight:{event.meeting_id}:{event.insight_id}",
                    subject,
                    INSIGHTS_READY,
                )
                if store.user(user_id)["status"] not in {"MISSED_EVENTS", "ACCESS_REQUIRED"}:
                    store.status(user_id, "LISTENING")
    except httpx.HTTPStatusError as error:
        if retryable(error) or error.response.status_code == 404:
            raise RuntimeError("Retry meeting follow-up") from None
        status = "FAILED_PERMANENT"
        store.status(user_id, "ACCESS_REQUIRED")
        log.warning("Graph insight status=%s", error.response.status_code)
    except (ValidationError, ValueError):
        status = "NEEDS_REVIEW"
    finally:
        log.info(
            "Insight user=%s status=%s latency_ms=%d",
            user_id,
            status,
            (time.monotonic() - started) * 1000,
        )
    return status
