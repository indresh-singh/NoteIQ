"""Manual Refresh: what it announces, how it is bounded, and one at a time per person."""

import asyncio

import httpx
import pytest

from app.models import parse_event
from app.sync import REFRESH_LANES, sync_now
from tests.conftest import USER

OTHER = "22222222-2222-2222-2222-222222222222"


def organizer(user_id=USER):
    return {"participants": {"organizer": {"identity": {"user": {"id": user_id}}}}}


def lists(*, discovered=(), transcripts=(), insights=()):
    """A graph.list side effect answering each Graph collection separately."""

    async def answer(path):
        if "getAllTranscripts" in path:
            return list(discovered)
        if path.endswith("/aiInsights"):
            return list(insights)
        if path.endswith("/transcripts"):
            return list(transcripts)
        return []

    return answer


def throttled_error():
    request = httpx.Request("GET", "https://graph.microsoft.com/v1.0/users")
    return httpx.HTTPStatusError(
        "throttled", request=request, response=httpx.Response(429, request=request)
    )


def test_claim_is_exclusive_until_released(store):
    first, second = {"run": "a"}, {"run": "b"}
    assert store.claim("refresh_lock", USER, first, ttl=120)
    assert not store.claim("refresh_lock", USER, second, ttl=120)
    # Other people are never blocked by someone else's Refresh.
    assert store.claim("refresh_lock", OTHER, second, ttl=120)
    assert store.release("refresh_lock", USER, first)
    assert store.claim("refresh_lock", USER, second, ttl=120)


def test_expired_claim_is_taken_over(store):
    """A request that crashed mid-Refresh must not lock the button for good."""
    assert store.claim("refresh_lock", USER, {"run": "crashed"}, ttl=-1)
    assert store.claim("refresh_lock", USER, {"run": "next"}, ttl=120)


def test_late_finisher_cannot_release_its_successor(store):
    store.claim("refresh_lock", USER, {"run": "slow"}, ttl=-1)
    store.claim("refresh_lock", USER, {"run": "next"}, ttl=120)
    assert not store.release("refresh_lock", USER, {"run": "slow"})
    assert store.get("refresh_lock", USER) == {"run": "next"}


async def test_new_meeting_is_announced(store, graph):
    graph.list.side_effect = lists(discovered=[{"id": "t", "meetingId": "brand-new"}])
    result = await sync_now(store, graph, USER)
    assert (result["new_meetings"], result["new_insights"]) == (1, 0)


async def test_new_transcript_for_a_shown_meeting_is_fetched_but_not_announced(store, graph):
    store.save_meeting(USER, "Shown", {"meeting_id": "m"})
    graph.list.side_effect = lists(
        discovered=[{"id": "t2", "meetingId": "m"}], transcripts=[{"id": "t2"}]
    )
    result = await sync_now(store, graph, USER)
    assert (result["new_meetings"], result["new_insights"]) == (0, 0)
    job = parse_event(store.next_job()["payload"])
    assert (job.meeting_id, job.transcript_id) == ("m", "t2")


async def test_new_insight_is_announced_and_a_known_one_is_not(store, graph):
    store.save_meeting(USER, "Known", {"meeting_id": "m", "insights": [{"insight": {"id": "old"}}]})
    graph.list.side_effect = lists(insights=[{"id": "old"}, {"id": "new"}])
    result = await sync_now(store, graph, USER)
    assert (result["new_meetings"], result["new_insights"]) == (0, 1)


async def test_meetings_are_checked_in_parallel_up_to_the_lane_limit(store, graph):
    for index in range(10):
        store.save_meeting(USER, f"M{index}", {"meeting_id": f"m{index}"})
    running = peak = 0

    async def slow_meeting(method, path):
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0.01)
        running -= 1
        return organizer()

    graph.request.side_effect = slow_meeting
    result = await sync_now(store, graph, USER)
    assert peak == REFRESH_LANES
    assert (result["checked"], result["total"], result["complete"]) == (10, 10, True)


async def test_shared_slots_cap_everyone_refreshing_at_once(store, graph):
    store.enroll(OTHER, "Other organizer")
    for user_id in (USER, OTHER):
        for index in range(6):
            store.save_meeting(user_id, f"M{index}", {"meeting_id": f"{user_id}-{index}"})
    running = peak = 0

    async def slow_meeting(method, path):
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0.01)
        running -= 1
        return organizer(path.split("/")[2])

    graph.request.side_effect = slow_meeting
    slots = asyncio.Semaphore(3)
    await asyncio.gather(
        sync_now(store, graph, USER, slots=slots), sync_now(store, graph, OTHER, slots=slots)
    )
    assert peak == 3


async def test_deadline_returns_what_was_checked_so_far(store, graph):
    for index in range(20):
        store.save_meeting(USER, f"M{index}", {"meeting_id": f"m{index}"})

    async def slow_meeting(method, path):
        await asyncio.sleep(0.1)
        return organizer()

    graph.request.side_effect = slow_meeting
    result = await sync_now(store, graph, USER, deadline=0.25)
    assert not result["complete"] and not result["throttled"]
    assert 0 < result["checked"] < result["total"] == 20


async def test_throttling_stops_the_refresh_early(store, graph):
    for index in range(20):
        store.save_meeting(USER, f"M{index}", {"meeting_id": f"m{index}"})
    graph.request.side_effect = throttled_error()
    result = await sync_now(store, graph, USER)
    assert result["throttled"] and not result["complete"]
    # Each lane stops after the call that was throttled instead of working
    # through all twenty meetings against a tenant Graph asked to slow down.
    assert graph.request.call_count <= REFRESH_LANES
    assert result["checked"] <= REFRESH_LANES


def test_second_refresh_is_told_to_wait(client, store, signed_in, graph):
    store.claim("refresh_lock", USER, {"run": "other-tab"}, ttl=120)
    assert client.post("/api/sync", headers=signed_in, json={}).json() == {
        "status": "already_running"
    }
    graph.list.assert_not_called()
    assert client.get("/api/sync/status", headers=signed_in).json()["running"] is True


def test_status_reports_the_last_refresh(client, store, signed_in, graph):
    graph.list.side_effect = lists(discovered=[{"id": "t", "meetingId": "brand-new"}])
    client.post("/api/sync", headers=signed_in, json={})
    status = client.get("/api/sync/status", headers=signed_in).json()
    assert status["running"] is False
    assert status["result"]["new_meetings"] == 1
    assert client.get("/api/sync/status").status_code == 401


def test_lock_is_released_when_refresh_fails(client, store, signed_in, monkeypatch):
    async def broken(*args, **kwargs):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr("app.sync.sync_now", broken)
    with pytest.raises(RuntimeError):
        client.post("/api/sync", headers=signed_in, json={})
    assert store.get("refresh_lock", USER) is None
