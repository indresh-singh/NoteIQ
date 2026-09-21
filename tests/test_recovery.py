"""Pasting a Teams join link must still reach every meeting state.

Recovery is the manual escape hatch an administrator reaches for when polling
or webhooks missed something, so the filtering that keeps polling cheap must
never quietly turn it into a no-op.
"""

import time

import pytest

from app.sync import recover_from_link, settled
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

    async def test_a_complete_meeting_reports_found_with_nothing_to_fetch(self, store, graph):
        """Honest answer: polling cannot see a Copilot revision, which reuses its id."""
        store.save_meeting(USER, "Done", {"meeting_id": "m1", "transcript": {"id": "t"}})
        store.save_meeting(USER, "Done", {"meeting_id": "m1", "insight": {"id": "i"}, "card": {}})
        graph.list.return_value = [{"id": "m1", "subject": "Done"}]
        assert await recover_from_link(store, graph, USER, LINK) == {"found": 1, "queued": 0}

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

    @pytest.mark.parametrize(
        "link",
        ["http://teams.microsoft.com/meet/1", "https://evil.example.com/meet/1", "not-a-url"],
    )
    async def test_a_link_that_is_not_a_teams_join_link_is_rejected(self, store, graph, link):
        with pytest.raises(ValueError):
            await recover_from_link(store, graph, USER, link)
        graph.list.assert_not_called()


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
