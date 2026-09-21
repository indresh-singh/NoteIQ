"""Worker throughput: the job pool, retention, and reaching a split-out worker.

One job at a time meant a single OpenRouter summary held up every queued
transcript behind it, and the minute's worth of polling the sweep enqueues ran
end to end before any of it. These cover running several at once, removing the
finished rows that polling leaves behind, and the repair signal that has to
cross a process boundary once web and worker are separate containers.
"""

import asyncio
import json
import time

import pytest

from app.config import settings
from app.models import MeetingSync
from app.store import Store
from app.worker import execute_job, run_job, run_worker
from tests.conftest import USER


async def _nothing(*args, **kwargs):
    return None


def meeting_sync(meeting_id="m"):
    return MeetingSync(user_id=USER, meeting_id=meeting_id).model_dump_json()


@pytest.fixture
def quiet_worker(monkeypatch):
    """Silence the timers so a test sees only the job pool."""

    async def nothing(*args, **kwargs):
        return None

    async def no_notification(*args, **kwargs):
        return False

    monkeypatch.setattr("app.worker.renew_subscriptions", nothing)
    monkeypatch.setattr("app.worker.queue_sync", lambda *args, **kwargs: 0)
    monkeypatch.setattr("app.worker.send_next_notification", no_notification)


class TestJobsRunConcurrently:
    async def test_several_jobs_are_in_flight_at_once(
        self, store, graph, quiet_worker, monkeypatch
    ):
        peak = 0
        running = 0
        release = asyncio.Event()

        async def slow_sync(event, graph, store):
            nonlocal peak, running
            running += 1
            peak = max(peak, running)
            await release.wait()
            running -= 1
            return "SYNCED"

        monkeypatch.setattr("app.worker.sync_meeting", slow_sync)
        store.enqueue([meeting_sync(f"m{i}") for i in range(4)])

        worker = asyncio.create_task(run_worker(store, graph, asyncio.Event()))
        for _ in range(200):
            await asyncio.sleep(0.01)
            if peak >= settings().job_concurrency:
                break
        release.set()
        worker.cancel()
        with pytest.raises(asyncio.CancelledError):
            await worker
        assert peak == settings().job_concurrency

    async def test_the_pool_tops_up_instead_of_waiting_for_a_batch(
        self, store, graph, quiet_worker, monkeypatch
    ):
        """One slow summary must not hold the other slots idle."""
        started = []
        blocked = asyncio.Event()

        async def sync(event, graph, store):
            started.append(event.meeting_id)
            if event.meeting_id == "slow":
                await blocked.wait()
            return "SYNCED"

        monkeypatch.setattr("app.worker.sync_meeting", sync)
        store.enqueue([meeting_sync("slow")])

        worker = asyncio.create_task(run_worker(store, graph, asyncio.Event()))
        for _ in range(200):
            await asyncio.sleep(0.01)
            if "slow" in started:
                break
        # Queued while the slow job holds its slot; the others must still run.
        store.enqueue([meeting_sync(f"m{i}") for i in range(3)])
        for _ in range(200):
            await asyncio.sleep(0.01)
            if len(started) >= 4:
                break
        blocked.set()
        worker.cancel()
        with pytest.raises(asyncio.CancelledError):
            await worker
        assert set(started) == {"slow", "m0", "m1", "m2"}

    async def test_fast_jobs_do_not_spin_the_database(self, store, graph, monkeypatch):
        """The loop wakes per job completion; the per-second work must not follow.

        Refilling a freed slot is why the loop wakes, but the repair check and
        the notification attempt are a query each. Running them per completion
        would trade the throughput this pool buys for database chatter.
        """
        done = 0
        housekeeping = 0

        async def sync(event, graph, store):
            nonlocal done
            done += 1
            return "SYNCED"

        async def notification(*args, **kwargs):
            nonlocal housekeeping
            housekeeping += 1
            return False

        monkeypatch.setattr("app.worker.sync_meeting", sync)
        monkeypatch.setattr("app.worker.send_next_notification", notification)
        monkeypatch.setattr("app.worker.queue_sync", lambda *args, **kwargs: 0)
        monkeypatch.setattr("app.worker.renew_subscriptions", _nothing)
        store.enqueue([meeting_sync(f"m{i}") for i in range(60)])

        worker = asyncio.create_task(run_worker(store, graph, asyncio.Event()))
        for _ in range(200):
            await asyncio.sleep(0.01)
            if done >= 60:
                break
        worker.cancel()
        with pytest.raises(asyncio.CancelledError):
            await worker
        # Sixty jobs churn through in well under a second, so the per-second
        # work should run a couple of times at most -- not once per batch.
        assert done >= 40
        assert housekeeping <= 3

    async def test_concurrency_is_configurable(self, monkeypatch):
        monkeypatch.setenv("NOTEIQ_JOB_CONCURRENCY", "8")
        settings.cache_clear()
        assert settings().job_concurrency == 8

    async def test_an_unusable_concurrency_is_rejected(self, monkeypatch):
        monkeypatch.setenv("NOTEIQ_JOB_CONCURRENCY", "0")
        settings.cache_clear()
        with pytest.raises(ValueError, match="NOTEIQ_JOB_CONCURRENCY"):
            settings()

    async def test_one_job_failing_does_not_disturb_the_others(self, store, graph, monkeypatch):
        async def sync(event, graph, store):
            if event.meeting_id == "bad":
                raise RuntimeError("Graph is down")
            return "SYNCED"

        monkeypatch.setattr("app.worker.sync_meeting", sync)
        store.enqueue([meeting_sync("bad"), meeting_sync("good")])
        jobs = [store.claim_job(), store.claim_job()]
        await asyncio.gather(*(execute_job(store, graph, job) for job in jobs))
        with store.connect() as db:
            rows = {
                json.loads(row["payload"])["meeting_id"]: (row["status"], row["attempts"])
                for row in db.execute("SELECT payload, status, attempts FROM jobs")
            }
        assert rows["good"][0] == "SYNCED"
        assert rows["bad"] == ("pending", 1)

    async def test_run_job_reports_whether_there_was_work(self, store, graph, monkeypatch):
        monkeypatch.setattr("app.worker.sync_meeting", lambda *a: _synced())
        assert not await run_job(store, graph)
        store.enqueue([meeting_sync()])
        assert await run_job(store, graph)


async def _synced():
    return "SYNCED"


class TestRetention:
    def test_finished_jobs_are_removed(self, store):
        store.enqueue([meeting_sync()])
        job = store.claim_job()
        store.finish_job(job["id"], "SYNCED")
        with store.connect() as db:
            db.execute("UPDATE jobs SET due=0 WHERE id=?", (job["id"],))
        assert store.prune()["jobs"] == 1
        with store.connect() as db:
            assert db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0

    def test_pending_jobs_are_never_removed(self, store):
        store.enqueue([meeting_sync()])
        with store.connect() as db:
            db.execute("UPDATE jobs SET due=0")
        assert store.prune()["jobs"] == 0

    def test_a_recently_finished_job_is_kept(self, store):
        store.enqueue([meeting_sync()])
        job = store.claim_job()
        store.finish_job(job["id"], "SYNCED")
        assert store.prune()["jobs"] == 0

    def test_failed_jobs_are_kept_longer_for_diagnosis(self, store):
        """Two days old: past the ordinary window, inside the one failures get."""
        store.enqueue([meeting_sync("failed"), meeting_sync("done")])
        for status in ("failed", "SYNCED"):
            job = store.claim_job()
            store.finish_job(job["id"], status)
        with store.connect() as db:
            db.execute("UPDATE jobs SET due=?", (time.time() - 2 * 86400,))
        removed = store.prune()
        assert (removed["jobs"], removed["failed_jobs"]) == (1, 0)
        with store.connect() as db:
            assert db.execute("SELECT status FROM jobs").fetchone()["status"] == "failed"
        assert store.prune(failed_job_days=1)["failed_jobs"] == 1

    def test_meetings_are_kept_unless_an_operator_asks(self, store):
        store.save_meeting(USER, "Old", {"meeting_id": "m"})
        with store.connect() as db:
            db.execute("UPDATE meetings SET created=0, occurred_at=NULL")
        assert store.prune()["meetings"] == 0
        assert len(store.meetings(USER)) == 1

    def test_an_expired_meeting_takes_its_transcript_with_it(self, store):
        store.save_transcript(USER, "m", "t", "hello")
        store.save_meeting(USER, "Old", {"meeting_id": "m"})
        with store.connect() as db:
            db.execute("UPDATE meetings SET created=0, occurred_at=NULL")
        removed = store.prune(meeting_days=7)
        assert (removed["meetings"], removed["transcripts"]) == (1, 1)
        with store.connect() as db:
            assert db.execute("SELECT COUNT(*) FROM transcripts").fetchone()[0] == 0

    def test_a_transcript_saved_before_its_meeting_row_survives(self, store):
        """process_transcript writes the transcript first; retention must not race it."""
        store.save_transcript(USER, "m", "t", "hello")
        assert store.prune(meeting_days=7)["transcripts"] == 0
        assert store.transcript(USER, 1) == "hello"

    def test_a_recent_meeting_is_kept(self, store):
        store.save_meeting(USER, "Recent", {"meeting_id": "m"})
        assert store.prune(meeting_days=7)["meetings"] == 0

    def test_a_retention_below_the_discovery_window_is_rejected(self, monkeypatch):
        """Otherwise retention deletes what the next sweep re-fetches."""
        monkeypatch.setenv("NOTEIQ_MEETING_RETENTION_DAYS", "1")
        settings.cache_clear()
        with pytest.raises(ValueError, match="NOTEIQ_MEETING_RETENTION_DAYS"):
            settings()


class TestRepairCrossesProcesses:
    def test_a_stored_request_is_taken_once(self, store):
        assert not store.take_repair()
        store.request_repair()
        assert store.take_repair()
        assert not store.take_repair()

    def test_the_endpoint_records_it_for_a_worker_elsewhere(self, client, store, signed_in):
        client.post("/api/reconnect", headers=signed_in)
        assert store.take_repair()

    async def test_the_worker_consumes_both_signals_together(
        self, store, graph, quiet_worker, monkeypatch
    ):
        """An in-process request sets both; that must not repair twice."""
        calls = []

        async def renew(graph, store, force=False):
            calls.append(force)

        monkeypatch.setattr("app.worker.renew_subscriptions", renew)
        event = asyncio.Event()
        event.set()
        store.request_repair()

        worker = asyncio.create_task(run_worker(store, graph, event))
        await asyncio.sleep(0.2)
        worker.cancel()
        with pytest.raises(asyncio.CancelledError):
            await worker
        assert calls == [True]


class TestRoles:
    def test_the_default_runs_both_halves(self, config):
        assert config.role == "all"
        assert config.runs_worker

    def test_a_web_role_does_not_start_the_worker(self, monkeypatch):
        monkeypatch.setenv("NOTEIQ_ROLE", "web")
        settings.cache_clear()
        assert not settings().runs_worker

    def test_a_worker_role_runs_it(self, monkeypatch):
        monkeypatch.setenv("NOTEIQ_ROLE", "worker")
        settings.cache_clear()
        assert settings().runs_worker

    def test_an_unknown_role_is_rejected(self, monkeypatch):
        monkeypatch.setenv("NOTEIQ_ROLE", "everything")
        settings.cache_clear()
        with pytest.raises(ValueError):
            settings()

    def test_the_web_role_serves_without_a_worker_task(self, monkeypatch, config, graph):
        from fastapi.testclient import TestClient

        from app.web import create_app

        started = []
        monkeypatch.setattr("app.web.run_worker", lambda *a: started.append(a))
        web_only = config.model_copy(update={"role": "web"})
        with TestClient(create_app(web_only, graph), base_url=config.public_url) as client:
            assert client.get("/healthz").json() == {"status": "ok"}
        assert started == []


def test_store_close_is_safe_without_a_pool(config):
    Store(config.database).close()
