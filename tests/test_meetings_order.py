"""Meetings must sort by when they actually happened, not by when we last touched the row."""

from tests.conftest import USER


def seed(store, meeting_id, transcript_created_at):
    local_id = store.save_transcript(USER, meeting_id, "t-" + meeting_id, "hello")
    store.save_meeting(
        USER,
        meeting_id,
        {
            "meeting_id": meeting_id,
            "transcript": {
                "id": "t-" + meeting_id,
                "local_id": local_id,
                "createdDateTime": transcript_created_at,
                "contentCorrelationId": None,
            },
        },
    )


def test_meetings_sort_by_occurrence_regardless_of_write_order(store):
    # Written newest-first, but the older meeting's transcript arrived (in
    # real time) before the newer one's — the list must still read newest-first.
    seed(store, "newer", "2026-09-15T10:00:00Z")
    seed(store, "older", "2026-09-01T10:00:00Z")

    ids = [m["content"]["meeting_id"] for m in store.meetings(USER)]
    assert ids == ["newer", "older"]


def test_regenerating_an_old_meeting_does_not_move_it_to_the_top(store):
    seed(store, "older", "2026-09-01T10:00:00Z")
    seed(store, "newer", "2026-09-15T10:00:00Z")
    assert [m["content"]["meeting_id"] for m in store.meetings(USER)] == ["newer", "older"]

    # Re-saving "older" (e.g. a Regenerate click, or Copilot delivering a late
    # insight) touches its row right now, long after "newer" was last touched.
    store.save_meeting(
        USER,
        "older",
        {
            "meeting_id": "older",
            "insight": {
                "id": "openrouter:t-older",
                "meetingNotes": [],
                "actionItems": [],
                "provider": "openrouter",
            },
            "card": {"body": []},
        },
    )
    assert [m["content"]["meeting_id"] for m in store.meetings(USER)] == ["newer", "older"]


def test_meetings_without_any_date_fall_back_to_when_they_were_saved(store):
    store.save_meeting(USER, "first", {"meeting_id": "first"})
    store.save_meeting(USER, "second", {"meeting_id": "second"})
    assert [m["content"]["meeting_id"] for m in store.meetings(USER)] == ["second", "first"]
