"""Pasting a Teams join link must still reach every meeting state.

Recovery is the manual escape hatch an administrator reaches for when polling
or webhooks missed something, so the filtering that keeps polling cheap must
never quietly turn it into a no-op.
"""

import time

import httpx
import pytest

from app.models import MeetingSync, TranscriptEvent, parse_event
from app.sync import queue_session_repairs, recover_from_link, settled, sync_meeting
from tests.conftest import USER

LINK = "https://teams.microsoft.com/meet/123456789"


class TestSettled:
    def test_a_meeting_awaiting_copilot_is_not_settled(self):
        assert not settled({"transcripts": [{"transcript": {"id": "t"}}]})

    def test_transcript_and_copilot_insight_is_settled(self):
        assert settled(
            {
                "transcripts": [{"transcript": {"id": "t"}}],
                "insights": [{"insight": {"id": "i", "provider": "copilot"}}],
            }
        )

    def test_an_untagged_insight_counts_as_copilot(self):
        """Rows predate the provider tag; the UI reads them as Copilot too."""
        assert settled(
            {"transcripts": [{"transcript": {"id": "t"}}], "insights": [{"insight": {"id": "i"}}]}
        )

    def test_an_openrouter_summary_alone_does_not_settle_a_meeting(self):
        """OpenRouter runs the moment a transcript lands, minutes before Copilot.

        Counting it would stop polling before Copilot -- the thing polling is
        waiting for -- ever publishes.
        """
        assert not settled(
            {
                "transcripts": [{"transcript": {"id": "t"}}],
                "insights": [{"insight": {"id": "openrouter:t", "provider": "openrouter"}}],
            }
        )

    def test_a_second_transcript_reopens_polling(self):
        assert not settled(
            {
                "transcripts": [{"transcript": {"id": "a"}}, {"transcript": {"id": "b"}}],
                "insights": [{"insight": {"id": "i", "provider": "copilot"}}],
            }
        )

    def test_a_meeting_with_no_transcript_is_never_settled(self):
        assert not settled({"meeting_id": "m"})


class TestRecoverFromLink:
    async def test_an_unknown_meeting_is_seeded_and_queued(self, store, graph):
        graph.list.return_value = [{"id": "m1", "subject": "Recovered"}]
        assert await recover_from_link(store, graph, USER, LINK) == {"found": 1, "queued": 1}
        assert store.meetings(USER)[0]["subject"] == "Recovered"

    async def test_a_meeting_stuck_without_its_insight_is_queued(self, store, graph):
        """The commonest reason an administrator pastes a link."""
        store.save_meeting(USER, "Half", {"meeting_id": "m1", "transcript": {"id": "t"}})
        graph.list.return_value = [{"id": "m1", "subject": "Half"}]
        assert (await recover_from_link(store, graph, USER, LINK))["queued"] >= 1

    async def test_a_meeting_with_only_an_openrouter_summary_is_queued(self, store, graph):
        """settled() must not let OpenRouter mask a missing Copilot insight."""
        store.save_meeting(USER, "Half", {"meeting_id": "m1", "transcript": {"id": "t"}})
        store.save_meeting(
            USER,
            "Half",
            {
                "meeting_id": "m1",
                "insight": {"id": "openrouter:t", "provider": "openrouter"},
                "card": {},
            },
        )
        graph.list.return_value = [{"id": "m1", "subject": "Half"}]
        assert (await recover_from_link(store, graph, USER, LINK))["queued"] >= 1

    async def test_a_meeting_older_than_the_polling_window_is_still_recoverable(self, store, graph):
        """Recovery is for old meetings; the 7-day polling cutoff must not block it."""
        store.save_meeting(USER, "Old", {"meeting_id": "m1"})
        with store.connect() as db:
            db.execute(
                "UPDATE meetings SET created=? WHERE user_id=?", (time.time() - 30 * 86400, USER)
            )
        graph.list.return_value = [{"id": "m1", "subject": "Old"}]
        assert (await recover_from_link(store, graph, USER, LINK))["queued"] >= 1

    async def test_a_complete_meeting_is_rechecked_for_missing_session_metadata(
        self, store, graph
    ):
        """Settled artifacts can predate callId capture and still need repair."""
        store.save_meeting(USER, "Done", {"meeting_id": "m1", "transcript": {"id": "t"}})
        store.save_meeting(USER, "Done", {"meeting_id": "m1", "insight": {"id": "i"}, "card": {}})
        graph.list.return_value = [{"id": "m1", "subject": "Done"}]
        assert await recover_from_link(store, graph, USER, LINK) == {"found": 1, "queued": 1}
        assert '"meeting_id":"m1"' in store.next_job()["payload"]

    async def test_recovery_queues_only_the_meetings_resolved_from_the_link(self, store, graph):
        store.save_meeting(USER, "Other", {"meeting_id": "other"})
        graph.list.return_value = [{"id": "m1", "subject": "Recovered"}]

        assert await recover_from_link(store, graph, USER, LINK) == {"found": 1, "queued": 1}

        jobs = []
        while job := store.next_job():
            jobs.append(job["payload"])
            store.finish_job(job["id"], "done")
        assert len(jobs) == 1
        assert '"meeting_id":"m1"' in jobs[0]
        assert "other" not in jobs[0]

    async def test_every_meeting_behind_one_link_is_seeded(self, store, graph):
        graph.list.return_value = [{"id": "m1", "subject": "A"}, {"id": "m2", "subject": "B"}]
        assert (await recover_from_link(store, graph, USER, LINK))["found"] == 2
        assert {m["content"]["meeting_id"] for m in store.meetings(USER)} == {"m1", "m2"}

    async def test_a_meeting_with_no_subject_gets_a_readable_fallback(self, store, graph):
        graph.list.return_value = [{"id": "m1"}]
        await recover_from_link(store, graph, USER, LINK)
        assert store.meetings(USER)[0]["subject"] == "Teams meeting"

    async def test_recovering_the_same_link_twice_does_not_duplicate_the_meeting(
        self, store, graph
    ):
        graph.list.return_value = [{"id": "m1", "subject": "Recovered"}]
        await recover_from_link(store, graph, USER, LINK)
        await recover_from_link(store, graph, USER, LINK)
        assert len(store.meetings(USER)) == 1

    async def test_recovery_preserves_artifacts_already_saved(self, store, graph):
        """Seeding writes only meeting_id; it must not blank the row."""
        store.save_meeting(USER, "Held", {"meeting_id": "m1", "transcript": {"id": "t"}})
        graph.list.return_value = [{"id": "m1", "subject": "Held"}]
        await recover_from_link(store, graph, USER, LINK)
        assert store.meetings(USER)[0]["content"]["transcripts"][0]["transcript"]["id"] == "t"

    async def test_an_unresolvable_link_finds_nothing(self, store, graph):
        graph.list.return_value = []
        assert await recover_from_link(store, graph, USER, LINK) == {"found": 0, "queued": 0}

    async def test_a_non_organizer_meeting_is_not_saved_or_scheduled(self, store, graph, caplog):
        other = "99999999-9999-9999-9999-999999999999"
        graph.list.return_value = [
            {
                "id": "m1",
                "subject": "Someone else's meeting",
                "participants": {"organizer": {"identity": {"user": {"id": other}}}},
            }
        ]

        result = await recover_from_link(store, graph, USER, LINK)

        assert result["found"] == 1
        assert result["eligible"] == 0
        assert result["queued"] == 0
        assert result["skipped_not_organizer"] == 1
        assert "not its organizer" in result["message"]
        assert store.meetings(USER) == []
        assert store.next_job() is None
        assert "reason=requested_user_is_not_organizer" in caplog.text

    async def test_recovery_access_denial_is_reported_without_saving(self, store, graph):
        request = httpx.Request("GET", "https://graph.microsoft.com/v1.0/test")
        response = httpx.Response(
            403,
            request=request,
            headers={"request-id": "recovery-denied"},
            json={"error": {"code": "Forbidden"}},
        )
        graph.list.return_value = [{"id": "m1", "subject": "Restricted"}]
        graph.request.side_effect = httpx.HTTPStatusError(
            "denied", request=request, response=response
        )

        result = await recover_from_link(store, graph, USER, LINK)

        assert result["eligible"] == 0
        assert result["skipped_access_denied"] == 1
        assert "denied access" in result["message"]
        assert store.meetings(USER) == []

    @pytest.mark.parametrize(
        "link",
        ["http://teams.microsoft.com/meet/1", "https://evil.example.com/meet/1", "not-a-url"],
    )
    async def test_a_link_that_is_not_a_teams_join_link_is_rejected(self, store, graph, link):
        with pytest.raises(ValueError):
            await recover_from_link(store, graph, USER, link)
        graph.list.assert_not_called()


class TestStagedSessionRepair:
    def save(self, store, meeting_id, *, meeting_type="recurring", checked=None, parts=1):
        for number in range(parts):
            transcript = {"id": f"{meeting_id}-t{number}"}
            if checked is not None:
                transcript["session_metadata_checked"] = checked
            store.save_meeting(
                USER,
                meeting_id,
                {
                    "meeting_id": meeting_id,
                    "meeting_metadata": {"meeting_type": meeting_type},
                    "transcript": transcript,
                },
            )

    def test_the_sweep_stages_a_bounded_batch_and_moves_to_the_next_records(self, store):
        for number in range(7):
            self.save(store, f"legacy-{number}")

        assert queue_session_repairs(store, limit=3) == 3
        first = set()
        while job := store.claim_job():
            event = parse_event(job["payload"])
            first.add(event.meeting_id)
            # Leave these pending as an in-flight worker would.
        assert len(first) == 3

        assert queue_session_repairs(store, limit=3) == 3
        with store.connect() as db:
            queued = {
                parse_event(row["payload"]).meeting_id
                for row in db.execute("SELECT payload FROM jobs WHERE status='pending'")
            }
        assert len(queued) == 6

    def test_only_meetings_that_need_session_boundaries_are_selected(self, store):
        self.save(store, "recurring")
        self.save(store, "split-scheduled", meeting_type="scheduled", parts=2)
        self.save(store, "single-scheduled", meeting_type="scheduled")
        self.save(store, "already-checked", checked=True)
        self.save(store, "terminal")
        store.set_meeting_sync_state(USER, "terminal", "SKIPPED_ACCESS_DENIED")

        candidates = set(store.session_repair_candidates(20))

        assert (USER, "recurring") in candidates
        assert (USER, "split-scheduled") in candidates
        assert (USER, "single-scheduled") not in candidates
        assert (USER, "already-checked") not in candidates
        assert (USER, "terminal") not in candidates

    async def test_graph_unavailable_history_is_marked_once(self, store, graph, samples):
        self.save(store, "legacy", parts=2)
        graph.request.return_value = {**samples["meeting"], "meetingType": "recurring"}
        graph.list.side_effect = [[], []]

        assert await sync_meeting(MeetingSync(user_id=USER, meeting_id="legacy"), graph, store) == "SYNCED"

        saved = store.find_meeting(USER, "legacy")
        assert all(
            item["transcript"]["session_metadata_checked"] is True
            and item["transcript"]["session_metadata_status"] == "unavailable"
            for item in saved["transcripts"]
        )
        assert store.session_repair_candidates(20) == []

    async def test_a_changed_graph_id_is_matched_by_correlation(self, store, graph, samples):
        store.save_meeting(
            USER,
            "Legacy",
            {
                "meeting_id": "legacy",
                "meeting_metadata": {"meeting_type": "recurring"},
                "transcript": {"id": "old-id", "contentCorrelationId": "same-recording"},
            },
        )
        graph.request.return_value = {**samples["meeting"], "meetingType": "recurring"}
        graph.list.side_effect = [
            [{"id": "new-id", "contentCorrelationId": "same-recording"}],
            [],
        ]

        await sync_meeting(MeetingSync(user_id=USER, meeting_id="legacy"), graph, store)

        event = parse_event(store.next_job()["payload"])
        assert isinstance(event, TranscriptEvent)
        assert event.transcript_id == "new-id"
        assert "session_metadata_status" not in store.find_meeting(USER, "legacy")["transcript"]


class TestRecoveryEndpoint:
    def test_the_endpoint_reports_what_it_queued(self, client, store, signed_in, graph):
        graph.list.return_value = [{"id": "m1", "subject": "Recovered"}]
        response = client.post(
            "/api/recover-meeting", headers=signed_in, json={"meeting_url": LINK}
        )
        assert response.status_code == 200
        assert response.json() == {"found": 1, "queued": 1}

    def test_a_meeting_the_organizer_does_not_own_is_a_404(self, client, signed_in, graph):
        graph.list.return_value = []
        response = client.post(
            "/api/recover-meeting", headers=signed_in, json={"meeting_url": LINK}
        )
        assert response.status_code == 404

    def test_a_found_meeting_owned_by_someone_else_is_a_clear_403(self, client, signed_in, graph):
        graph.list.return_value = [
            {
                "id": "m1",
                "participants": {
                    "organizer": {
                        "identity": {"user": {"id": "99999999-9999-9999-9999-999999999999"}}
                    }
                },
            }
        ]
        response = client.post(
            "/api/recover-meeting", headers=signed_in, json={"meeting_url": LINK}
        )
        assert response.status_code == 403
        assert "not its organizer" in response.json()["detail"]

    def test_a_bad_link_is_a_400_not_a_500(self, client, signed_in, graph):
        response = client.post(
            "/api/recover-meeting",
            headers=signed_in,
            json={"meeting_url": "https://evil.example.com/meet/1"},
        )
        assert response.status_code == 400

    def test_recovery_requires_a_signed_in_organizer(self, client, graph):
        response = client.post("/api/recover-meeting", json={"meeting_url": LINK})
        assert response.status_code in {401, 403}
        graph.list.assert_not_called()
