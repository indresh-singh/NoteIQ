import logging
import time

import httpx
from pydantic import ValidationError

from app.activity import INSIGHTS_READY, queue_notification
from app.adaptive_cards import build_card
from app.graph_client import GraphClient, retryable
from app.models import Insight, InsightEvent, MeetingSync, age_seconds
from app.store import Store, digest

log = logging.getLogger(__name__)


async def process_insight(event: InsightEvent, graph: GraphClient, store: Store) -> str:
    started = time.monotonic()
    status = "PROCESSING"
    user_id = str(event.user_id)
    publish_lag = None
    try:
        user = store.user(user_id)
        if not user or not user["enabled"]:
            status = "SKIPPED_NOT_ENROLLED"
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
            # Seconds between the meeting ending and Graph serving its
            # insight: Microsoft's publication lag plus at most one poll.
            publish_lag = age_seconds(insight.endDateTime)
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
                    store, user_id, f"insight:{event.meeting_id}", subject, INSIGHTS_READY
                )
                if store.user(user_id)["status"] not in {"MISSED_EVENTS", "ACCESS_REQUIRED"}:
                    store.status(user_id, "LISTENING")
    except httpx.HTTPStatusError as error:
        if retryable(error) or error.response.status_code == 404:
            log.warning(
                "Insight retrieval will retry user=%s meeting=%s http_status=%s",
                user_id,
                digest(event.meeting_id)[:8],
                error.response.status_code,
            )
            raise RuntimeError("Retry meeting follow-up") from error
        status = "FAILED_PERMANENT"
        store.status(user_id, "ACCESS_REQUIRED")
        # GraphClient already recorded a sanitized upstream diagnostic. Do not
        # attach this exception: an HTTPStatusError message can contain an
        # upstream response body supplied by the provider.
        log.warning(
            "Insight retrieval permanently failed user=%s meeting=%s http_status=%s",
            user_id,
            digest(event.meeting_id)[:8],
            error.response.status_code,
        )
    except (ValidationError, ValueError) as error:
        status = "NEEDS_REVIEW"
        log.exception(
            "Insight data needs review user=%s meeting=%s error_type=%s error=%s",
            user_id,
            digest(event.meeting_id)[:8],
            type(error).__name__,
            error,
        )
    finally:
        log.info(
            "Insight user=%s meeting=%s status=%s publish_lag_s=%s latency_ms=%d",
            user_id,
            digest(event.meeting_id)[:8],
            status,
            "unknown" if publish_lag is None else round(publish_lag),
            (time.monotonic() - started) * 1000,
        )
    return status
