import asyncio
import contextlib
import logging
import time

from app.activity import send_next_notification
from app.config import settings
from app.graph_client import GraphClient
from app.insights import process_insight
from app.models import MeetingSync, TranscriptEvent, UserSync, parse_event
from app.store import Store
from app.subscriptions import renew_subscriptions
from app.sync import discover_meetings, queue_sync, sync_meeting
from app.transcripts import process_transcript

log = logging.getLogger(__name__)

SWEEP_SECONDS = 60
RENEWAL_SECONDS = 15 * 60
PRUNE_SECONDS = 3600


async def execute_job(store: Store, graph: GraphClient, job: dict) -> None:
    """Run one already-claimed job to completion, recording how long it took.

    Several of these run at once, which is safe because every store call is
    synchronous: a read-modify-write like save_meeting cannot be interleaved
    with another job's, since it contains no await for the loop to switch on.
    Making the store async would break that and require explicit locking.
    """
    started = time.monotonic()
    try:
        event = parse_event(job["payload"])
        process = process_transcript if isinstance(event, TranscriptEvent) else process_insight
        if isinstance(event, MeetingSync):
            process = sync_meeting
        elif isinstance(event, UserSync):
            process = discover_meetings
        status = await process(event, graph, store)
        store.finish_job(job["id"], status)
        log.info(
            "Job id=%s type=%s result=%s duration_ms=%d",
            job["id"],
            type(event).__name__,
            status,
            (time.monotonic() - started) * 1000,
        )
    except Exception:
        store.retry_job(job)
        log.warning(
            "Job id=%s attempt=%s failed duration_ms=%d",
            job["id"],
            job["attempts"] + 1,
            (time.monotonic() - started) * 1000,
        )


async def run_job(store: Store, graph: GraphClient) -> bool:
    """Claim and run a single job. Returns whether there was one to run."""
    job = store.claim_job()
    if not job:
        return False
    await execute_job(store, graph, job)
    return True


async def run_worker(store: Store, graph: GraphClient, repair: asyncio.Event):
    concurrency = settings().job_concurrency
    retention = settings().meeting_retention_days
    next_renewal = 0.0
    next_sync = 0.0
    next_prune = 0.0
    next_housekeeping = 0.0
    running: set[asyncio.Task] = set()
    log.info("Worker started concurrency=%s meeting_retention_days=%s", concurrency, retention)
    try:
        while True:
            if time.monotonic() >= next_sync:
                next_sync = time.monotonic() + SWEEP_SECONDS
                queued = 0
                for user_id in store.users():
                    queued += queue_sync(store, user_id, discover=True)
                # Queue depth trending up across a day is the saturation signal:
                # it says the sweep is enqueueing faster than this worker drains.
                log.info(
                    "Sweep queued=%s pending=%s running=%s",
                    queued,
                    store.pending_job_count(),
                    len(running),
                )
            # Top the pool back up first, and on every pass: the loop wakes as
            # soon as any job finishes, and refilling the freed slot is the whole
            # point of waking. One slow OpenRouter summary must not hold the
            # other slots idle waiting for a batch to drain.
            while len(running) < concurrency:
                job = store.claim_job()
                if not job:
                    break
                running.add(asyncio.create_task(execute_job(store, graph, job)))
            # The rest costs a query each, and the loop iterates once per job
            # completion rather than once per second while work is flowing, so
            # it is held to the idle cadence instead of spinning the database.
            sent = False
            if time.monotonic() >= next_housekeeping:
                next_housekeeping = time.monotonic() + 1
                # Both signals are taken together so an in-process request, which
                # sets each of them, cannot cause two consecutive repairs.
                force = repair.is_set()
                repair.clear()
                force = store.take_repair() or force
                if force or time.monotonic() >= next_renewal:
                    next_renewal = time.monotonic() + RENEWAL_SECONDS
                    try:
                        await renew_subscriptions(graph, store, force=force)
                    except Exception:
                        next_renewal = time.monotonic() + 60
                        for user_id in store.users():
                            store.status(user_id, "CONNECTION_ERROR")
                        log.warning("Subscription check failed; retrying in one minute")
                if time.monotonic() >= next_prune:
                    next_prune = time.monotonic() + PRUNE_SECONDS
                    try:
                        removed = store.prune(meeting_days=retention)
                    except Exception:
                        removed = {}
                        log.warning("Pruning finished work failed; retrying next hour")
                    if any(removed.values()):
                        log.info(
                            "Pruned %s",
                            " ".join(f"{name}={count}" for name, count in sorted(removed.items())),
                        )
                sent = await send_next_notification(store, graph)
            if running:
                _, running = await asyncio.wait(
                    running, timeout=1, return_when=asyncio.FIRST_COMPLETED
                )
            elif not sent:
                await asyncio.sleep(1)
    finally:
        for task in running:
            task.cancel()
        for task in running:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
