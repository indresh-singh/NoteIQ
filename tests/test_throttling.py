"""Graph throttling (HTTP 429): honour Retry-After, pause the tenant, and never call it an outage."""

import asyncio
import time
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from unittest.mock import AsyncMock

import httpx
import pytest

from app.activity import INSIGHTS_READY, queue_notification, send_next_notification
from app.config import settings
from app.graph_client import (
    DEFAULT_THROTTLE_SECONDS,
    GraphClient,
    graph_throttle_seconds,
    retry_after_seconds,
)
from app.models import MeetingSync
from app.subscriptions import renew_subscriptions
from app.sync import sync_now
from app.worker import _renew, execute_job, run_worker
from tests.conftest import USER

OTHER = "22222222-2222-2222-2222-222222222222"


def graph_429(retry_after="300", host="graph.microsoft.com"):
    request = httpx.Request("GET", f"https://{host}/v1.0/users")
    headers = {"Retry-After": retry_after} if retry_after is not None else {}
    response = httpx.Response(429, headers=headers, request=request)
    return httpx.HTTPStatusError("throttled", request=request, response=response)


def wrapped(error):
    """How process_transcript/process_insight re-raise a retryable Graph error."""
    try:
        raise RuntimeError("Retry transcript retrieval") from error
    except RuntimeError as outer:
        return outer


class TestRetryAfter:
    def test_seconds_and_http_date_forms(self):
        assert retry_after_seconds(graph_429("120").response) == 120
        later = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=90), usegmt=True)
        assert 85 <= retry_after_seconds(graph_429(later).response) <= 90
        assert retry_after_seconds(graph_429(None).response) is None

    def test_a_graph_429_is_found_behind_a_job_error(self):
        assert graph_throttle_seconds(graph_429("300")) == 300
        assert graph_throttle_seconds(wrapped(graph_429("300"))) == 300
        assert graph_throttle_seconds(graph_429(None)) == DEFAULT_THROTTLE_SECONDS

    def test_other_errors_are_not_throttling(self):
        assert graph_throttle_seconds(RuntimeError("boom")) is None
        request = httpx.Request("GET", "https://graph.microsoft.com/v1.0/users")
        denied = httpx.Response(403, request=request)
        assert (
            graph_throttle_seconds(httpx.HTTPStatusError("x", request=request, response=denied))
            is None
        )
        # An AI provider's rate limit throttles that provider, not the tenant.
        assert graph_throttle_seconds(graph_429(host="api.openai.com")) is None

    def test_a_suppressed_cause_is_not_followed(self):
        try:
            try:
                raise graph_429()
            except httpx.HTTPStatusError:
                raise ValueError("rate-limited") from None
        except ValueError as error:
            assert graph_throttle_seconds(error) is None

    async def test_client_reports_every_429_to_the_pause_hook(self, monkeypatch):
        monkeypatch.setattr("app.graph_client.graph_token", AsyncMock(return_value="token"))
        paused = []
        graph = GraphClient(on_throttle=paused.append)
        graph._client = httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(429, headers={"Retry-After": "120"})
            )
        )
        with pytest.raises(httpx.HTTPStatusError):
            await graph.request("GET", "/users", retries=False)
        assert paused == [120]
        await graph.aclose()


class TestPause:
    def test_pause_is_extended_but_never_shortened(self, store):
        assert store.graph_paused_until() == 0
        store.pause_graph(300)
        long_pause = store.graph_paused_until()
        store.pause_graph(5)
        assert store.graph_paused_until() == long_pause
        store.pause_graph(600)
        assert store.graph_paused_until() > long_pause

    def test_pause_ends_on_its_own(self, store):
        store.pause_graph(-1)
        assert store.graph_paused_until() == 0


class TestJobQueue:
    def test_throttled_retry_waits_for_retry_after_without_using_an_attempt(self, store):
        store.enqueue([MeetingSync(user_id=USER, meeting_id="m").model_dump_json()])
        job = store.claim_job()
        until = time.time() + 300
        store.retry_job(job, throttled_until=until)
        with store.connect() as db:
            row = db.execute("SELECT attempts, due, status FROM jobs").fetchone()
        assert (row["attempts"], row["status"]) == (0, "pending")
        assert row["due"] == pytest.approx(until)

    def test_a_long_throttle_cannot_exhaust_a_job(self, store):
        store.enqueue([MeetingSync(user_id=USER, meeting_id="m").model_dump_json()])
        job = store.claim_job()
        for _ in range(10):
            store.retry_job(job, throttled_until=time.time())
        with store.connect() as db:
            assert db.execute("SELECT status FROM jobs").fetchone()["status"] == "pending"

    async def test_job_throttled_by_graph_is_deferred_and_pauses_the_tenant(
        self, store, graph, monkeypatch
    ):
        async def throttled(event, graph, store):
            raise wrapped(graph_429("300"))

        monkeypatch.setattr("app.worker.sync_meeting", throttled)
        store.enqueue([MeetingSync(user_id=USER, meeting_id="m").model_dump_json()])
        await execute_job(store, graph, store.claim_job())
        with store.connect() as db:
            row = db.execute("SELECT attempts, due FROM jobs").fetchone()
        assert row["attempts"] == 0
        assert row["due"] == pytest.approx(time.time() + 300, abs=5)
        assert store.graph_paused_until() == pytest.approx(time.time() + 300, abs=5)


class TestWorkerPause:
    async def test_worker_makes_no_graph_calls_while_paused(self, store, graph, monkeypatch):
        calls = []

        async def record(name, result=None):
            calls.append(name)
            return result

        async def renew(*args, **kwargs):
            return await record("renew", 0)

        async def notify(*args, **kwargs):
            return await record("notify", False)

        async def job(event, graph, store):
            return await record("job", "SYNCED")

        monkeypatch.setattr("app.worker.renew_subscriptions", renew)
        monkeypatch.setattr("app.worker.send_next_notification", notify)
        monkeypatch.setattr("app.worker.sync_meeting", job)
        sweeps = []
        monkeypatch.setattr("app.worker.queue_sync", lambda *a, **k: sweeps.append(1) or 0)
        store.enqueue([MeetingSync(user_id=USER, meeting_id="m").model_dump_json()])
        store.pause_graph(60)
        worker = asyncio.create_task(run_worker(store, graph, asyncio.Event()))
        await asyncio.sleep(0.3)
        worker.cancel()
        with pytest.raises(asyncio.CancelledError):
            await worker
        assert calls == [] and sweeps == []
        assert store.pending_job_count() == 1

    async def test_worker_resumes_when_the_pause_ends(self, store, graph, monkeypatch):
        done = asyncio.Event()

        async def job(event, graph, store):
            done.set()
            return "SYNCED"

        async def nothing(*args, **kwargs):
            return 0

        async def no_notification(*args, **kwargs):
            return False

        monkeypatch.setattr("app.worker.renew_subscriptions", nothing)
        monkeypatch.setattr("app.worker.send_next_notification", no_notification)
        monkeypatch.setattr("app.worker.queue_sync", lambda *a, **k: 0)
        monkeypatch.setattr("app.worker.sync_meeting", job)
        store.enqueue([MeetingSync(user_id=USER, meeting_id="m").model_dump_json()])
        store.pause_graph(0.5)
        worker = asyncio.create_task(run_worker(store, graph, asyncio.Event()))
        try:
            await asyncio.wait_for(done.wait(), timeout=5)
        finally:
            worker.cancel()
            with pytest.raises(asyncio.CancelledError):
                await worker


class TestRenewal:
    async def test_throttled_renewal_is_not_shown_as_a_connection_error(
        self, store, graph, monkeypatch
    ):
        store.status(USER, "LISTENING")

        async def throttled(*args, **kwargs):
            raise graph_429("120")

        monkeypatch.setattr("app.worker.renew_subscriptions", throttled)
        repair = asyncio.Event()
        repair.set()
        next_renewal = await _renew(store, graph, repair, 0.0)
        assert store.user(USER)["status"] == "LISTENING"
        assert store.graph_paused_until() == pytest.approx(time.time() + 120, abs=5)
        # The forced repair was not carried out, so it is still asked for.
        assert repair.is_set()
        assert next_renewal <= time.monotonic() + 60

    async def test_other_renewal_failures_still_report_a_connection_error(
        self, store, graph, monkeypatch
    ):
        async def broken(*args, **kwargs):
            raise httpx.ConnectError("offline")

        monkeypatch.setattr("app.worker.renew_subscriptions", broken)
        await _renew(store, graph, asyncio.Event(), 0.0)
        assert store.user(USER)["status"] == "CONNECTION_ERROR"

    async def test_throttled_subscription_leaves_status_and_defers_the_rest(
        self, config, store, monkeypatch
    ):
        monkeypatch.setenv("NOTEIQ_SUBSCRIPTION_CONCURRENCY", "1")
        settings.cache_clear()
        store.enroll(OTHER, "Other organizer")
        store.status(USER, "LISTENING")
        store.status(OTHER, "LISTENING")
        graph = AsyncMock()
        graph.list.return_value = []
        graph.request.side_effect = graph_429("120")
        assert await renew_subscriptions(graph, store) == 2
        assert store.user(USER)["status"] == "LISTENING"
        assert store.user(OTHER)["status"] == "LISTENING"
        # One throttled call pauses the tenant; nobody else is asked after it.
        assert graph.request.call_count == 1

    async def test_deferred_users_are_retried_in_a_minute(self, store, graph, monkeypatch):
        async def partly_deferred(*args, **kwargs):
            return 1

        monkeypatch.setattr("app.worker.renew_subscriptions", partly_deferred)
        assert await _renew(store, graph, asyncio.Event(), 0.0) <= time.monotonic() + 60


async def test_throttled_notification_waits_without_using_an_attempt(store):
    queue_notification(store, USER, "insight:meeting", "Meeting", INSIGHTS_READY)
    graph = AsyncMock()
    graph.request.side_effect = graph_429("300")
    assert await send_next_notification(store, graph)
    with store.connect() as db:
        row = db.execute("SELECT attempts, due, status FROM activity_outbox").fetchone()
    assert (row["attempts"], row["status"]) == (0, "pending")
    assert row["due"] == pytest.approx(time.time() + 300, abs=5)


async def test_refresh_makes_no_graph_calls_while_paused(store, graph):
    store.save_meeting(USER, "Meeting", {"meeting_id": "m"})
    store.pause_graph(120)
    result = await sync_now(store, graph, USER)
    graph.list.assert_not_called()
    graph.request.assert_not_called()
    assert result["throttled"] and not result["complete"]
    assert 115 <= result["retry_after"] <= 120


async def test_refresh_stops_when_a_pause_starts_mid_way(store, graph):
    for index in range(20):
        store.save_meeting(USER, f"M{index}", {"meeting_id": f"m{index}"})

    async def meeting(method, path):
        # Another replica is throttled while this Refresh is running.
        store.pause_graph(60)
        return {"participants": {"organizer": {"identity": {"user": {"id": USER}}}}}

    graph.request.side_effect = meeting
    result = await sync_now(store, graph, USER)
    assert result["throttled"] and result["checked"] < 20


class TestDelayedUpdates:
    """Throttled renewal stays quiet only while the subscription is still live."""

    def subscribe(self, store, minutes, user_id=USER, kind="insights"):
        store.save_subscription(user_id, kind, f"sub-{kind}", time.time() + minutes * 60)

    def test_lapsing_subscription_is_shown_as_delayed(self, store):
        store.status(USER, "LISTENING")
        self.subscribe(store, minutes=5)
        assert store.flag_delayed_updates(600) == [USER]
        assert store.user(USER)["status"] == "UPDATES_DELAYED"

    def test_an_expired_subscription_is_delayed_too(self, store):
        store.status(USER, "LISTENING")
        self.subscribe(store, minutes=-30, kind="transcripts")
        assert store.flag_delayed_updates(600) == [USER]

    def test_a_live_subscription_stays_quiet_even_while_graph_is_paused(self, store):
        store.status(USER, "LISTENING")
        store.pause_graph(300)
        self.subscribe(store, minutes=45)
        assert store.flag_delayed_updates(600) == []
        assert store.user(USER)["status"] == "LISTENING"

    def test_a_more_specific_problem_is_not_overwritten(self, store):
        store.status(USER, "ACCESS_REQUIRED")
        self.subscribe(store, minutes=5)
        assert store.flag_delayed_updates(600) == []
        assert store.user(USER)["status"] == "ACCESS_REQUIRED"

    async def test_successful_renewal_clears_it(self, config, store):
        store.status(USER, "UPDATES_DELAYED")
        self.subscribe(store, minutes=5)
        graph = AsyncMock()
        graph.list.return_value = []
        graph.request.return_value = {"id": "renewed"}
        assert await renew_subscriptions(graph, store) == 0
        assert store.user(USER)["status"] == "LISTENING"

    async def test_throttled_renewal_does_not_clear_it(self, config, store):
        store.status(USER, "UPDATES_DELAYED")
        self.subscribe(store, minutes=5)
        graph = AsyncMock()
        graph.list.return_value = []
        graph.request.side_effect = graph_429("120")
        await renew_subscriptions(graph, store)
        assert store.user(USER)["status"] == "UPDATES_DELAYED"

    async def test_a_saved_insight_does_not_clear_it(self, samples, store):
        from app.insights import process_insight
        from app.models import InsightEvent

        store.status(USER, "UPDATES_DELAYED")
        graph = AsyncMock()
        graph.request.side_effect = [samples["meeting"], samples["insight"]]
        event = InsightEvent.from_resource(samples["notification"]["value"][0]["resource"])
        assert str(event.user_id) == USER
        assert await process_insight(event, graph, store) == "SAVED"
        assert store.user(USER)["status"] == "UPDATES_DELAYED"

    async def test_worker_checks_while_paused_and_only_every_two_minutes(
        self, store, graph, monkeypatch
    ):
        from app.worker import DELAY_CHECK_SECONDS

        assert DELAY_CHECK_SECONDS == 120

        async def nothing(*args, **kwargs):
            return 0

        async def no_notification(*args, **kwargs):
            return False

        monkeypatch.setattr("app.worker.renew_subscriptions", nothing)
        monkeypatch.setattr("app.worker.send_next_notification", no_notification)
        monkeypatch.setattr("app.worker.queue_sync", lambda *a, **k: 0)
        monkeypatch.setattr("app.worker.HOUSEKEEPING_SECONDS", 0)
        checks = []
        original = store.flag_delayed_updates
        monkeypatch.setattr(
            store, "flag_delayed_updates", lambda within: checks.append(within) or original(within)
        )
        store.status(USER, "LISTENING")
        self.subscribe(store, minutes=5)
        store.pause_graph(60)
        worker = asyncio.create_task(run_worker(store, graph, asyncio.Event()))
        await asyncio.sleep(2.5)
        worker.cancel()
        with pytest.raises(asyncio.CancelledError):
            await worker
        # Housekeeping ran on every pass, but the expiry check only once.
        assert checks == [600]
        assert store.user(USER)["status"] == "UPDATES_DELAYED"


def test_me_reports_the_throttle_pause_until_it_ends(client, store, signed_in):
    assert client.get("/api/me", headers=signed_in).json()["graph_throttled_seconds"] == 0
    store.pause_graph(45)
    assert 44 <= client.get("/api/me", headers=signed_in).json()["graph_throttled_seconds"] <= 45
    with store.connect() as db:
        db.execute("DELETE FROM temporary WHERE kind='graph'")
    assert client.get("/api/me", headers=signed_in).json()["graph_throttled_seconds"] == 0
