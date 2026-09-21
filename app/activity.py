"""Notify the organizer in Teams Activity as NoteIQ, using app-only Graph access."""

import hashlib
import logging
import time

from app.config import settings

TRANSCRIPT_READY = "Your transcript is ready. We'll notify you when Copilot's summary and action items are available."
INSIGHTS_READY = "Your meeting summary and action items are ready. Open NoteIQ to review them."
log = logging.getLogger(__name__)


def queue_notification(store, user_id, event_key, subject, message):
    with store.connect() as db:
        db.execute(
            """INSERT OR IGNORE INTO activity_outbox(user_id, event_key, subject, message, due)
            SELECT id, ?, ?, ?, ? FROM users WHERE id=? AND enabled=1""",
            (event_key, subject, message, time.time(), user_id),
        )


async def send_next_notification(store, graph):
    job = store.next_notification()
    if not job:
        return False
    user = store.user(job["user_id"])
    status = "cancelled"
    if user and user["enabled"]:
        config = settings()
        app_id = config.teams_app_id or config.graph_client_id
        # Stable chain IDs update the same activity if a send succeeds before a retry/crash.
        chain_id = int(
            hashlib.sha256(f"{job['user_id']}:{job['event_key']}".encode()).hexdigest()[:13], 16
        )
        activity_type = (
            "transcriptReady" if job["event_key"].startswith("transcript:") else "insightsReady"
        )
        try:
            await graph.request(
                "POST",
                f"/users/{job['user_id']}/teamwork/sendActivityNotification",
                retries=False,
                json={
                    "topic": {
                        "source": "text",
                        "value": job["subject"][:200],
                        "webUrl": f"https://teams.microsoft.com/l/entity/{app_id}/meetings",
                    },
                    "activityType": activity_type,
                    "chainId": chain_id,
                    "previewText": {"content": job["message"][:150]},
                },
            )
            status = "sent"
        except Exception as error:
            status = "failed" if job["attempts"] >= 4 else "pending"
            log.exception(
                "Activity notification failed id=%s user=%s event_type=%s attempt=%s "
                "next_status=%s error_type=%s error=%s",
                job["id"],
                job["user_id"],
                activity_type,
                job["attempts"] + 1,
                status,
                type(error).__name__,
                error,
            )
    with store.connect() as db:
        db.execute(
            "UPDATE activity_outbox SET status=?, attempts=attempts+1, due=? WHERE id=?",
            (status, time.time() + min(60 * 2 ** job["attempts"], 900), job["id"]),
        )
    return True
