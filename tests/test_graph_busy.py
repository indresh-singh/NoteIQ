"""Buttons that call Graph say "try again in N seconds" when Microsoft 365 throttles."""

from unittest.mock import AsyncMock

import pytest

from app.graph_client import GraphBusy, interactive_requests, retry
from app.planner import Planner
from tests.conftest import USER
from tests.test_planner import FakePlanner, add_plan
from tests.test_throttling import graph_429

LIMITED = "Microsoft 365 is limiting requests right now. Try again in about"


class TestPlanner:
    async def test_a_throttled_planner_call_is_not_a_generic_failure(self, config):
        graph = AsyncMock()
        graph.request.side_effect = graph_429("30")
        with pytest.raises(GraphBusy) as caught:
            await Planner(config, graph).plan_name("plan")
        assert str(caught.value) == f"{LIMITED} 30 seconds."
        assert caught.value.retry_after == 30

    async def test_task_exists_reports_throttling(self, config):
        graph = AsyncMock()
        graph.request.side_effect = graph_429("1")
        with pytest.raises(GraphBusy, match="about 1 second\\."):
            await Planner(config, graph).task_exists("task")

    async def test_throttled_notes_do_not_fail_a_task_that_was_created(self, config):
        """Otherwise trying again would create the same task twice."""
        graph = AsyncMock()
        graph.request.side_effect = [{"id": "created-task"}, graph_429("30")]
        task = await Planner(config, graph).create_task("plan", "Title", "Notes")
        assert task["id"] == "created-task"


class TestButtons:
    def test_check_meeting_reports_throttling_instead_of_a_server_error(
        self, client, signed_in, graph
    ):
        graph.list.side_effect = graph_429("20")
        response = client.post(
            "/api/recover-meeting",
            headers=signed_in,
            json={"meeting_url": "https://teams.microsoft.com/meet/123456789"},
        )
        assert response.status_code == 429
        assert response.headers["Retry-After"] == "20"
        assert response.json()["detail"] == f"{LIMITED} 20 seconds."

    def test_while_paused_buttons_answer_without_calling_graph(
        self, client, store, signed_in, graph
    ):
        fake = FakePlanner()
        client.app.state.planner = fake
        add_plan(client, signed_in)
        store.pause_graph(40)
        response = client.post(
            "/api/recover-meeting",
            headers=signed_in,
            json={"meeting_url": "https://teams.microsoft.com/meet/123456789"},
        )
        assert response.status_code == 429
        assert LIMITED in response.json()["detail"]
        graph.list.assert_not_called()
        for path in ("/api/planner/available-plans", "/api/planner/tasks?plan_id=123"):
            assert client.get(path, headers=signed_in).status_code == 429
        # Endpoints that never touch Graph are unaffected by the pause.
        assert client.get("/api/planner", headers=signed_in).status_code == 200

    def test_a_partly_sent_export_says_what_was_sent(self, client, store, signed_in, samples):
        class ThrottledAfterOne(FakePlanner):
            throttle = True

            async def create_task(self, plan_id, title, description):
                if self.created and self.throttle:
                    raise GraphBusy(30)
                return await super().create_task(plan_id, title, description)

        fake = ThrottledAfterOne()
        client.app.state.planner = fake
        add_plan(client, signed_in)
        store.save_meeting(USER, "Budget", {"meeting_id": "meeting", "insight": samples["insight"]})
        meeting_id = store.meetings(USER)[0]["id"]
        response = client.post(f"/api/meetings/{meeting_id}/planner", headers=signed_in, json={})
        assert response.status_code == 429
        assert response.json()["detail"] == (
            f"Sent 1 task to Planner. {LIMITED} 30 seconds. Tasks already sent won't be sent twice."
        )
        # Trying again sends only what is left.
        fake.throttle = False
        response = client.post(f"/api/meetings/{meeting_id}/planner", headers=signed_in, json={})
        assert response.json() == {"created": 2, "skipped": 1}
        assert len(fake.created) == 3


class TestWaitCap:
    async def test_an_interactive_request_does_not_wait_out_a_long_retry_after(self, monkeypatch):
        sleeps = []

        async def sleep(seconds):
            sleeps.append(seconds)

        monkeypatch.setattr("app.graph_client.asyncio.sleep", sleep)
        calls = 0

        async def throttled():
            nonlocal calls
            calls += 1
            raise graph_429("30")

        with interactive_requests(), pytest.raises(Exception):
            await retry(throttled)
        assert (calls, sleeps) == (1, [])
        # The worker still waits it out, as before.
        calls = 0
        with pytest.raises(Exception):
            await retry(throttled)
        assert calls == 3 and sleeps == [30, 30]
