import asyncio
import contextlib
import logging
import time

from app.activity import send_next_notification
from app.config import settings
from app.graph_client import GraphClient, graph_throttle_seconds
from app.insights import process_insight
from app.models import MeetingSync, TranscriptEvent, UserSync, parse_event
from app.observability import log_context
from app.store import Store
from app.subscriptions import renew_subscriptions
from app.sync import discover_meetings, queue_session_repairs, queue_sync, sync_meeting
from app.transcripts import process_transcript

log = logging.getLogger(__name__)

SWEEP_SECONDS = 60
RENEWAL_SECONDS = 15 * 60
PRUNE_SECONDS = 3600
HOUSEKEEPING_SECONDS = 2
# Checking for lapsing subscriptions is one query, but it has no urgency: the
# warning margin below is ten minutes, so every two minutes is ample and keeps
# the loop's time for jobs.
DELAY_CHECK_SECONDS = 120
# Warn when a subscription is this close to expiry (or past it). Renewal starts
# 30 minutes out, so this is ~20 minutes of failed renewal.
SUBSCRIPTION_RISK_SECONDS = 600


async def execute_job(store: Store, graph: GraphClient, job: dict) -> None:
    """Run one already-claimed job to completion, recording how long it took.

    Several of these run at once, which is safe because every store call is
    synchronous: a read-modify-write like save_meeting cannot be interleaved
    with another job's, since it contains no await for the loop to switch on.
    Making the store async would break that and require explicit locking.
    """
    started = time.monotonic()
    with log_context(job_id=job["id"]):
        event = None
        try:
            event = parse_event(job["payload"])
            process = process_transcript if isinstance(event, TranscriptEvent) else process_insight
            if isinstance(event, MeetingSync):
                process = sync_meeting
            elif isinstance(event, UserSync):
                process = discover_meetings
            log.info(
                "Job started id=%s type=%s attempt=%s due=%s payload_bytes=%s",
                job["id"],
                type(event).__name__,
                job["attempts"] + 1,
                job.get("due", "-"),
                len(job["payload"]),
            )
            status = await process(event, graph, store)
            store.finish_job(job["id"], status)
            log.info(
                "Job completed id=%s type=%s result=%s attempt=%s duration_ms=%d",
                job["id"],
                type(event).__name__,
                status,
                job["attempts"] + 1,
                (time.monotonic() - started) * 1000,
            )
        except Exception as error:
            wait = graph_throttle_seconds(error)
            try:
                if wait is None:
                    store.retry_job(job)
                else:
                    # GraphClient normally records the pause itself; repeating
                    # it here covers a client built without the hook. It only
                    # ever extends, so the job waits for the longest pause.
                    store.pause_graph(wait)
                    store.retry_job(job, throttled_until=store.graph_paused_until())
            except Exception:
                log.exception(
                    "Job retry persistence failed id=%s original_error_type=%s original_error=%s",
                    job.get("id"),
                    type(error).__name__,
                    error,
                )
                raise
            if wait is not None:
                # Throttling is expected under load and says nothing is wrong
                # with the job; a traceback per throttled job is noise.
                log.warning(
                    "Job throttled by Graph id=%s type=%s attempt=%s retry_after_s=%.1f "
                    "duration_ms=%d",
                    job.get("id"),
                    type(event).__name__ if event is not None else "unparseable",
                    job.get("attempts", 0) + 1,
                    wait,
                    (time.monotonic() - started) * 1000,
                )
                return
            log.exception(
                "Job failed id=%s type=%s attempt=%s duration_ms=%d error_type=%s error=%s",
                job.get("id"),
                type(event).__name__ if event is not None else "unparseable",
                job.get("attempts", 0) + 1,
                (time.monotonic() - started) * 1000,
                type(error).__name__,
                error,
            )


async def run_job(store: Store, graph: GraphClient) -> bool:
    """Claim and run a single job. Returns whether there was one to run."""
    job = store.claim_job()
    if not job:
        return False
    await execute_job(store, graph, job)
    return True


async def _renew(
    store: Store, graph: GraphClient, repair: asyncio.Event, next_renewal: float
) -> float:
    """Renew subscriptions when due or asked to; returns when the next check is due."""
    # Both signals are taken together so an in-process request, which sets
    # each of them, cannot cause two consecutive repairs.
    force = repair.is_set()
    repair.clear()
    force = store.take_repair() or force
    if not force and time.monotonic() < next_renewal:
        return next_renewal
    try:
        deferred = await renew_subscriptions(graph, store, force=force)
    except Exception as error:
        wait = graph_throttle_seconds(error)
        if wait is not None:
            # Throttling is not an outage: nobody's connection is broken, so no
            # one is shown a connection error. The pause holds renewal back
            # until Graph allows it, and a forced repair is kept for then.
            store.pause_graph(wait)
            if force:
                repair.set()
            log.warning(
                "Subscription check throttled by Graph retry_after_s=%.1f force=%s "
                "user_status=unchanged",
                wait,
                force,
            )
            return time.monotonic() + 60
        for user_id in store.users():
            store.status(user_id, "CONNECTION_ERROR")
        log.exception(
            "Subscription check failed retry_delay_s=60 force=%s "
            "user_count=%s error_type=%s error=%s",
            force,
            len(store.users()),
            type(error).__name__,
            error,
        )
        return time.monotonic() + 60
    # Users deferred by throttling are retried in a minute, not fifteen, so a
    # subscription due for renewal is not left to run out; a forced repair that
    # did not reach them is asked for again.
    if deferred and force:
        repair.set()
    return time.monotonic() + (60 if deferred else RENEWAL_SECONDS)


def _flag_delayed_updates(store: Store, paused: bool) -> None:
    try:
        delayed = store.flag_delayed_updates(SUBSCRIPTION_RISK_SECONDS)
    except Exception as error:
        log.exception(
            "Subscription expiry check failed error_type=%s error=%s",
            type(error).__name__,
            error,
        )
        return
    if delayed:
        log.warning(
            "Live meeting updates delayed: subscriptions lapsing user_count=%s "
            "graph_paused=%s within_s=%s",
            len(delayed),
            paused,
            SUBSCRIPTION_RISK_SECONDS,
        )


async def run_worker(store: Store, graph: GraphClient, repair: asyncio.Event):
    concurrency = settings().job_concurrency
    retention = settings().meeting_retention_days
    next_renewal = 0.0
    next_sync = 0.0
    next_prune = 0.0
    next_housekeeping = 0.0
    next_delay_check = 0.0
    running: set[asyncio.Task] = set()
    paused = False
    log.info("Worker started concurrency=%s meeting_retention_days=%s", concurrency, retention)
    try:
        while True:
            # Graph throttles the whole tenant, so a 429 anywhere -- this
            # worker, another replica, a Refresh -- pauses every Graph call made
            # from here: no new jobs, sweep, renewal or notifications until
            # Retry-After has passed. Jobs already running finish on their own.
            paused_until = store.graph_paused_until()
            if paused_until > time.time():
                if not paused:
                    log.warning(
                        "Graph work paused by throttling remaining_s=%.1f running=%s pending=%s",
                        paused_until - time.time(),
                        len(running),
                        store.pending_job_count(),
                    )
                paused = True
            elif paused:
                paused = False
                log.info("Graph work resumed after throttling")
            if not paused and time.monotonic() >= next_sync:
                next_sync = time.monotonic() + SWEEP_SECONDS
                queued = 0
                for user_id in store.users():
                    queued += queue_sync(store, user_id, discover=True)
                repairs = queue_session_repairs(store)
                queued += repairs
                # Queue depth trending up across a day is the saturation signal:
                # it says the sweep is enqueueing faster than this worker drains.
                log.info(
                    "Sweep queued=%s session_repairs=%s pending=%s running=%s",
                    queued,
                    repairs,
                    store.pending_job_count(),
                    len(running),
                )
            # Top the pool back up first, and on every pass: the loop wakes as
            # soon as any job finishes, and refilling the freed slot is the whole
            # point of waking. One slow OpenRouter summary must not hold the
            # other slots idle waiting for a batch to drain.
            while not paused and len(running) < concurrency:
                job = store.claim_job()
                if not job:
                    break
                running.add(asyncio.create_task(execute_job(store, graph, job)))
            # The rest costs a query each, and the loop iterates once per job
            # completion rather than once per second while work is flowing, so
            # it is held to the idle cadence instead of spinning the database.
            sent = False
            if time.monotonic() >= next_housekeeping:
                # Keep this independent of job-completion frequency. Two seconds
                # is prompt for notifications/repair while leaving room for the
                # extra diagnostic I/O enabled in production.
                next_housekeeping = time.monotonic() + HOUSEKEEPING_SECONDS
                # Skipped entirely while paused, so a pending repair request is
                # left in place for when the pause ends rather than consumed.
                if not paused:
                    next_renewal = await _renew(store, graph, repair, next_renewal)
                # Deliberately runs while paused too: that is exactly when
                # renewal is not running and a lapse would otherwise go unseen.
                # After renewal, so one that just succeeded is not flagged.
                if time.monotonic() >= next_delay_check:
                    next_delay_check = time.monotonic() + DELAY_CHECK_SECONDS
                    _flag_delayed_updates(store, paused)
                if time.monotonic() >= next_prune:
                    next_prune = time.monotonic() + PRUNE_SECONDS
                    try:
                        removed = store.prune(meeting_days=retention)
                    except Exception as error:
                        removed = {}
                        log.exception(
                            "Pruning finished work failed retry_delay_s=%s retention_days=%s "
                            "error_type=%s error=%s",
                            PRUNE_SECONDS,
                            retention,
                            type(error).__name__,
                            error,
                        )
                    if any(removed.values()):
                        log.info(
                            "Pruned %s",
                            " ".join(f"{name}={count}" for name, count in sorted(removed.items())),
                        )
                if not paused:
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
