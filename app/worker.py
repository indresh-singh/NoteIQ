import asyncio
import logging
import time

from app.activity import send_next_notification
from app.graph_client import GraphClient
from app.insights import process_insight
from app.models import MeetingSync, TranscriptEvent, parse_event
from app.store import Store
from app.subscriptions import renew_subscriptions
from app.sync import queue_sync, sync_meeting
from app.transcripts import process_transcript

log = logging.getLogger(__name__)


async def run_job(store: Store, graph: GraphClient) -> bool:
    job = store.claim_job()
    if not job:
        return False
    try:
        event = parse_event(job["payload"])
        process = process_transcript if isinstance(event, TranscriptEvent) else process_insight
        if isinstance(event, MeetingSync):
            process = sync_meeting
        status = await process(event, graph, store)
        store.finish_job(job["id"], status)
        log.info("Job id=%s type=%s result=%s", job["id"], type(event).__name__, status)
    except Exception:
        store.retry_job(job)
        log.warning("Job id=%s attempt=%s failed", job["id"], job["attempts"] + 1)
    return True


async def run_worker(store: Store, graph: GraphClient, repair: asyncio.Event):
    next_renewal = 0.0
    next_sync = 0.0
    while True:
        if time.monotonic() >= next_sync:
            next_sync = time.monotonic() + 300
            for user_id in store.users():
                queue_sync(store, user_id)
        if repair.is_set() or time.monotonic() >= next_renewal:
            force = repair.is_set()
            repair.clear()
            next_renewal = time.monotonic() + 15 * 60
            try:
                await renew_subscriptions(graph, store, force=force)
            except Exception:
                next_renewal = time.monotonic() + 60
                for user_id in store.users():
                    store.status(user_id, "CONNECTION_ERROR")
                log.warning("Subscription check failed; retrying in one minute")
        worked = await run_job(store, graph)
        sent = await send_next_notification(store, graph)
        if not worked and not sent:
            await asyncio.sleep(1)
