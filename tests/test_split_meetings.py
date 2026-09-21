"""A meeting whose transcription was stopped and restarted.

Microsoft documents that "each transcript event of the meeting creates an
associated AI insight object", so a restart yields two transcripts and two
Copilot insights, each covering only its own half. These lock how NoteIQ waits
for the pair and how it summarises across the seam.
"""

from datetime import datetime, timedelta, timezone

from app.sync import bodies, past_publication_window, queue_sync, settled
from app.transcripts import meeting_transcript_text
from tests.conftest import USER


def stamp(hours_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def segment(id_, correlation, hours_ago=0.1):
    return {
        "transcript": {
            "id": id_,
            "contentCorrelationId": correlation,
            "createdDateTime": stamp(hours_ago),
        }
    }


def copilot(id_, correlation):
    return {"insight": {"id": id_, "provider": "copilot", "contentCorrelationId": correlation}}


class TestPairingByCorrelationId:
    def test_the_matching_insight_settles_its_transcript(self):
        content = {"transcripts": [segment("a", "x")], "insights": [copilot("i", "x")]}
        assert settled(content)

    def test_an_insight_for_the_other_segment_does_not_settle_this_one(self):
        """Counting alone would call this finished; pairing knows better."""
        content = {
            "transcripts": [segment("a", "x"), segment("b", "y")],
            "insights": [copilot("i1", "x"), copilot("i2", "x")],
        }
        assert len(content["insights"]) >= len(content["transcripts"])
        assert not settled(content)

    def test_both_segments_matched_is_settled(self):
        content = {
            "transcripts": [segment("a", "x"), segment("b", "y")],
            "insights": [copilot("i1", "x"), copilot("i2", "y")],
        }
        assert settled(content)

    def test_an_insight_without_a_correlation_id_cannot_cover_a_transcript(self):
        content = {
            "transcripts": [segment("a", "x")],
            "insights": [{"insight": {"id": "i", "provider": "copilot"}}],
        }
        assert not settled(content)

    def test_rows_predating_correlation_ids_fall_back_to_counting(self):
        content = {
            "transcripts": [{"transcript": {"id": "a"}}],
            "insights": [{"insight": {"id": "i", "provider": "copilot"}}],
        }
        assert settled(content)

    def test_an_openrouter_summary_never_settles_a_transcript(self):
        content = {
            "transcripts": [segment("a", "x")],
            "insights": [
                {
                    "insight": {
                        "id": "openrouter:m",
                        "provider": "openrouter",
                        "contentCorrelationId": "x",
                    }
                }
            ],
        }
        assert not settled(content)


class TestPublicationWindow:
    def test_a_fresh_transcript_is_still_within_the_window(self):
        assert not past_publication_window({"transcripts": [segment("a", "x", hours_ago=1)]})

    def test_an_old_transcript_is_past_it(self):
        assert past_publication_window({"transcripts": [segment("a", "x", hours_ago=9)]})

    def test_a_recent_restart_keeps_the_whole_meeting_in_the_window(self):
        """The newest segment governs: its insight may still be coming."""
        content = {"transcripts": [segment("a", "x", hours_ago=9), segment("b", "y", hours_ago=1)]}
        assert not past_publication_window(content)

    def test_a_meeting_with_no_transcript_is_never_abandoned(self):
        assert not past_publication_window({"meeting_id": "m"})

    def test_an_unreadable_timestamp_keeps_polling(self):
        content = {"transcripts": [{"transcript": {"id": "a", "createdDateTime": "not a date"}}]}
        assert not past_publication_window(content)


class TestSweepStopsWaitingForever:
    def seed(self, store, hours_ago):
        store.save_meeting(
            USER,
            "Stuck",
            {"meeting_id": "m1", "transcript": segment("a", "x", hours_ago)["transcript"]},
        )

    def test_a_transcript_copilot_never_summarised_leaves_the_sweep(self, store):
        self.seed(store, hours_ago=9)
        assert queue_sync(store, USER) == 0

    def test_the_same_meeting_is_polled_while_the_window_is_open(self, store):
        self.seed(store, hours_ago=1)
        assert queue_sync(store, USER) == 1

    def test_recovery_still_reaches_a_meeting_the_sweep_gave_up_on(self, store):
        """The window bounds the background sweep, not the manual escape hatch."""
        self.seed(store, hours_ago=9)
        assert queue_sync(store, USER, within_window=False) == 1

    def test_discovery_still_runs_when_every_meeting_is_past_the_window(self, store):
        self.seed(store, hours_ago=9)
        assert queue_sync(store, USER, discover=True) == 1


class TestWholeMeetingText:
    def test_segments_are_joined_oldest_first(self, store):
        first = store.save_transcript(USER, "m1", "a", "part one")
        second = store.save_transcript(USER, "m1", "b", "part two")
        content = {
            "transcripts": [
                {"transcript": {"id": "b", "local_id": second, "createdDateTime": stamp(1)}},
                {"transcript": {"id": "a", "local_id": first, "createdDateTime": stamp(2)}},
            ]
        }
        assert meeting_transcript_text(store, USER, content) == "part one\n\npart two"

    def test_a_single_transcript_is_unchanged(self, store):
        local = store.save_transcript(USER, "m1", "a", "only part")
        content = {"transcripts": [{"transcript": {"id": "a", "local_id": local}}]}
        assert meeting_transcript_text(store, USER, content) == "only part"

    def test_a_segment_whose_text_is_missing_is_skipped(self, store):
        local = store.save_transcript(USER, "m1", "a", "kept")
        content = {
            "transcripts": [
                {"transcript": {"id": "a", "local_id": local}},
                {"transcript": {"id": "b", "local_id": 9999}},
                {"transcript": {"id": "c"}},
            ]
        }
        assert meeting_transcript_text(store, USER, content) == "kept"

    def test_another_users_transcript_is_never_read(self, store):
        """local_id is a row id, so it must stay scoped to the signed-in user."""
        store.enroll("22222222-2222-2222-2222-222222222222", "Other")
        other = store.save_transcript("22222222-2222-2222-2222-222222222222", "m9", "x", "private")
        content = {"transcripts": [{"transcript": {"id": "x", "local_id": other}}]}
        assert meeting_transcript_text(store, USER, content) == ""

    def test_no_transcripts_yields_nothing(self, store):
        assert meeting_transcript_text(store, USER, {"meeting_id": "m"}) == ""


class TestSecondSegmentEndToEnd:
    """Processing a restarted meeting's second transcript."""

    async def test_openrouter_summarises_both_halves_as_one_meeting(
        self, monkeypatch, store, samples
    ):
        from unittest.mock import AsyncMock

        from app.config import settings
        from app.models import Insight, TranscriptEvent
        from app.transcripts import process_transcript

        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
        monkeypatch.setenv("OPENROUTER_MODEL", "test/model")
        settings.cache_clear()

        seen = {}

        class FakeOpenRouter:
            def __init__(self, config):
                pass

            async def summarize(self, key, subject, text):
                seen["key"], seen["text"] = key, text
                return Insight(id=f"openrouter:{key}", meetingNotes=[{"text": "Whole meeting."}])

        monkeypatch.setattr("app.transcripts.OpenRouter", FakeOpenRouter)

        # First half already saved, exactly as an earlier run would have left it.
        first = store.save_transcript(USER, "sample-meeting", "t1", "WEBVTT\nfirst half")
        store.save_meeting(
            USER,
            "Split",
            {
                "meeting_id": "sample-meeting",
                "transcript": {"id": "t1", "local_id": first, "createdDateTime": stamp(2)},
            },
        )

        graph = AsyncMock()
        graph.request.side_effect = [
            samples["meeting"],
            {"id": "t2", "createdDateTime": stamp(1)},
            "WEBVTT\nsecond half",
        ]
        second = TranscriptEvent(user_id=USER, meeting_id="sample-meeting", transcript_id="t2")
        assert await process_transcript(second, graph, store) == "TRANSCRIPT_SAVED"

        # The model saw the whole meeting, in order, not just the new segment.
        assert seen["text"] == "WEBVTT\nfirst half\n\nWEBVTT\nsecond half"
        assert seen["key"] == "sample-meeting"

        content = store.meetings(USER)[0]["content"]
        assert len(content["transcripts"]) == 2
        # One summary covering the meeting, not one per segment.
        openrouter = [i for i in content["insights"] if i["insight"]["provider"] == "openrouter"]
        assert len(openrouter) == 1


class TestBodies:
    def test_the_array_shape_is_unwrapped(self):
        assert bodies({"transcripts": [{"transcript": {"id": "a"}}]}, "transcript") == [{"id": "a"}]

    def test_the_singular_shape_still_reads(self):
        assert bodies({"insight": {"id": "i"}}, "insight") == [{"id": "i"}]

    def test_absent_is_empty(self):
        assert bodies({}, "transcript") == []
