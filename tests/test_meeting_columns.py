"""The columns the polling sweep reads instead of every saved card.

queue_sync runs once a minute per user. Answering "is this meeting still worth a
Graph call?" from the JSON meant loading every stored summary and Adaptive Card
to look at four fields, so the answer is computed once per write into columns.
These lock the columns to the predicates they replaced: if the two ever disagree,
the sweep either abandons a meeting early or polls one forever.
"""

import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from app.store import Store, meeting_facts, newest_transcript_at, settled
from app.sync import RECENT_SECONDS, past_publication_window, queue_sync
from tests.conftest import USER


def stamp(hours_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def segment(id_, correlation="x", hours_ago=0.1):
    return {
        "transcript": {
            "id": id_,
            "contentCorrelationId": correlation,
            "createdDateTime": stamp(hours_ago),
        }
    }


def copilot(id_, correlation="x"):
    return {"insight": {"id": id_, "provider": "copilot", "contentCorrelationId": correlation}}


def columns(store, meeting_id):
    with store.connect() as db:
        return dict(
            db.execute(
                """SELECT meeting_id, source, settled, newest_transcript_at FROM meetings
                WHERE meeting_id=?""",
                (meeting_id,),
            ).fetchone()
        )


def candidates(store, **kwargs):
    return store.sync_candidates(USER, since=0, **kwargs)


class TestDerivedColumnsMatchTheContent:
    def test_an_unsettled_meeting_is_recorded_as_unsettled(self, store):
        store.save_meeting(USER, "Waiting", {"meeting_id": "m", "transcripts": [segment("t")]})
        assert columns(store, "m")["settled"] == 0

    def test_the_insight_that_settles_a_meeting_updates_the_column(self, store):
        store.save_meeting(USER, "Waiting", {"meeting_id": "m", "transcripts": [segment("t")]})
        store.save_meeting(USER, "Done", {"meeting_id": "m", "insight": copilot("i")["insight"]})
        assert columns(store, "m")["settled"] == 1

    def test_a_second_transcript_reopens_the_column(self, store):
        """Graph makes one insight per transcript; a restart is not finished."""
        store.save_meeting(
            USER,
            "Done",
            {
                "meeting_id": "m",
                "transcripts": [segment("t1", "a")],
                "insights": [copilot("i", "a")],
            },
        )
        assert columns(store, "m")["settled"] == 1
        store.save_meeting(
            USER, "Restarted", {"meeting_id": "m", "transcript": segment("t2", "b")["transcript"]}
        )
        assert columns(store, "m")["settled"] == 0

    def test_an_upload_records_its_source(self, store):
        store.save_meeting(USER, "Upload", {"meeting_id": "u", "source": "upload"})
        assert columns(store, "u")["source"] == "upload"

    def test_the_newest_segment_sets_the_publication_clock(self, store):
        content = {
            "meeting_id": "m",
            "transcripts": [segment("t1", hours_ago=9), segment("t2", hours_ago=1)],
        }
        store.save_meeting(USER, "Two", content)
        # The newest segment, not the oldest: an hour ago, so still in window.
        assert columns(store, "m")["newest_transcript_at"] == pytest.approx(
            newest_transcript_at(content), abs=1
        )
        assert not past_publication_window(content)


class TestUnreadableTimestampsKeepPolling:
    """The window is an optimisation; abandoning a meeting early is the one
    failure it must not have."""

    def test_a_meeting_with_no_transcript_yet_has_no_clock(self):
        assert newest_transcript_at({"meeting_id": "m"}) is None

    def test_one_unparseable_timestamp_discards_the_whole_answer(self):
        content = {
            "transcripts": [
                segment("t1", hours_ago=1),
                {"transcript": {"id": "t2", "createdDateTime": "not a date"}},
            ]
        }
        assert newest_transcript_at(content) is None
        assert not past_publication_window(content)

    def test_a_missing_timestamp_discards_the_whole_answer(self):
        content = {"transcripts": [segment("t1", hours_ago=1), {"transcript": {"id": "t2"}}]}
        assert newest_transcript_at(content) is None

    def test_a_naive_timestamp_is_read_as_utc(self):
        """.timestamp() would otherwise read it as local time and skew the window."""
        naive = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
        content = {"transcripts": [{"transcript": {"id": "t", "createdDateTime": naive}}]}
        assert newest_transcript_at(content) == pytest.approx(
            datetime.now(timezone.utc).timestamp(), abs=5
        )


class TestSweepSelectsTheSameMeetingsAsBefore:
    def test_a_settled_meeting_is_not_polled(self, store):
        store.save_meeting(
            USER,
            "Done",
            {"meeting_id": "done", "transcripts": [segment("t")], "insights": [copilot("i")]},
        )
        store.save_meeting(
            USER, "Waiting", {"meeting_id": "waiting", "transcripts": [segment("t")]}
        )
        assert candidates(store) == ["waiting"]

    def test_an_upload_is_never_polled(self, store):
        store.save_meeting(USER, "Upload", {"meeting_id": "u", "source": "upload"})
        assert candidates(store) == []

    def test_a_meeting_past_the_publication_window_is_dropped(self, store):
        content = {"meeting_id": "old", "transcripts": [segment("t", hours_ago=9)]}
        store.save_meeting(USER, "Old", content)
        assert past_publication_window(content)
        assert candidates(store) == []

    def test_recovery_reaches_past_the_window(self, store):
        store.save_meeting(
            USER, "Old", {"meeting_id": "old", "transcripts": [segment("t", hours_ago=9)]}
        )
        assert candidates(store, within_window=False) == ["old"]

    def test_refresh_reaches_settled_meetings(self, store):
        """The background sweep backs off; a person clicking Refresh does not."""
        store.save_meeting(
            USER,
            "Done",
            {"meeting_id": "done", "transcripts": [segment("t")], "insights": [copilot("i")]},
        )
        assert candidates(store) == []
        assert candidates(store, within_window=False, only_unsettled=False) == ["done"]

    def test_a_meeting_with_no_transcript_yet_keeps_polling(self, store):
        store.save_meeting(USER, "Seeded", {"meeting_id": "m"})
        assert candidates(store) == ["m"]

    def test_meetings_older_than_the_window_are_dropped(self, store):
        store.save_meeting(USER, "Ancient", {"meeting_id": "m"})
        assert store.sync_candidates(USER, since=0) == ["m"]
        with store.connect() as db:
            db.execute("UPDATE meetings SET created=0 WHERE meeting_id='m'")
        assert store.sync_candidates(USER, since=1) == []

    def test_duplicate_rows_for_one_meeting_are_polled_once(self, store):
        """Historical duplicates are retained deliberately; they are still one poll."""
        store.save_meeting(USER, "First", {"meeting_id": "m"})
        with store.connect() as db:
            db.execute(
                """INSERT INTO meetings(user_id, subject, content, created, meeting_id, settled)
                VALUES (?, 'Duplicate', '{"meeting_id": "m"}', ?, 'm', 0)""",
                (USER, 9999999999.0),
            )
        assert candidates(store) == ["m"]

    def test_queue_sync_queues_one_job_per_candidate(self, store):
        store.save_meeting(
            USER, "Waiting", {"meeting_id": "waiting", "transcripts": [segment("t")]}
        )
        store.save_meeting(USER, "Upload", {"meeting_id": "u", "source": "upload"})
        assert queue_sync(store, USER) == 1
        assert json.loads(store.claim_job()["payload"])["meeting_id"] == "waiting"
        assert store.claim_job() is None


class TestProjectionsReplaceWholeRowReads:
    def test_find_meeting_returns_the_row_save_meeting_would_write_to(self, store):
        store.save_meeting(USER, "First", {"meeting_id": "m", "transcripts": [segment("t")]})
        store.save_meeting(USER, "Second", {"meeting_id": "other"})
        assert store.find_meeting(USER, "m")["transcripts"][0]["transcript"]["id"] == "t"
        assert store.find_meeting(USER, "missing") is None

    def test_find_meeting_is_scoped_to_its_user(self, store):
        other = "22222222-2222-2222-2222-222222222222"
        store.enroll(other, "Someone else")
        store.save_meeting(USER, "Mine", {"meeting_id": "m"})
        assert store.find_meeting(other, "m") is None

    def test_transcript_aliases_covers_every_name_a_transcript_has(self, store):
        store.save_meeting(
            USER,
            "Meeting",
            {"meeting_id": "m", "transcript": {"id": "listing-name", "source_id": "detail-name"}},
        )
        assert store.transcript_aliases(USER) == {("m", "listing-name"), ("m", "detail-name")}

    def test_transcript_aliases_is_empty_before_a_transcript_lands(self, store):
        store.save_meeting(USER, "Seeded", {"meeting_id": "m"})
        assert store.transcript_aliases(USER) == set()

    def test_transcript_aliases_is_scoped_to_its_user(self, store):
        other = "22222222-2222-2222-2222-222222222222"
        store.enroll(other, "Someone else")
        store.save_meeting(USER, "Mine", {"meeting_id": "m", "transcript": {"id": "t"}})
        assert store.transcript_aliases(other) == set()


class TestExistingRowsAreBackfilled:
    def test_a_database_predating_the_columns_is_migrated(self, config, tmp_path):
        path = tmp_path / "legacy.sqlite3"
        legacy = sqlite3.connect(path)
        legacy.execute(
            """CREATE TABLE meetings (
                id INTEGER PRIMARY KEY, user_id TEXT NOT NULL, subject TEXT NOT NULL,
                content TEXT NOT NULL, created REAL NOT NULL)"""
        )
        content = {"meeting_id": "m", "transcripts": [segment("t")], "insights": [copilot("i")]}
        legacy.execute(
            "INSERT INTO meetings(user_id, subject, content, created) VALUES (?, ?, ?, ?)",
            (USER, "Legacy", json.dumps(content), 9999999999.0),
        )
        legacy.commit()
        legacy.close()

        store = Store(path)
        store.enroll(USER, "Demo organizer")
        assert columns(store, "m") == {
            "meeting_id": "m",
            "source": None,
            "settled": 1,
            "newest_transcript_at": pytest.approx(newest_transcript_at(content), abs=1),
        }

    def test_a_legacy_row_without_a_meeting_id_is_not_rescanned(self, config, tmp_path):
        """It cannot be polled either way; writing it back stops the scan finding it."""
        path = tmp_path / "legacy.sqlite3"
        legacy = sqlite3.connect(path)
        legacy.execute(
            """CREATE TABLE meetings (
                id INTEGER PRIMARY KEY, user_id TEXT NOT NULL, subject TEXT NOT NULL,
                content TEXT NOT NULL, created REAL NOT NULL)"""
        )
        legacy.execute(
            "INSERT INTO meetings(user_id, subject, content, created) VALUES (?, ?, '{}', ?)",
            (USER, "Nameless", 9999999999.0),
        )
        legacy.commit()
        legacy.close()

        store = Store(path)
        with store.connect() as db:
            assert (
                db.execute("SELECT COUNT(*) FROM meetings WHERE meeting_id IS NULL").fetchone()[0]
                == 0
            )
        assert store.sync_candidates(USER, since=0) == []


def test_meeting_facts_reports_what_the_columns_store():
    content = {"meeting_id": "m", "source": "upload", "transcripts": [segment("t")]}
    facts = meeting_facts(content)
    assert facts["meeting_id"] == "m"
    assert facts["source"] == "upload"
    assert facts["settled"] == (1 if settled(content) else 0)
    assert facts["newest_transcript_at"] == newest_transcript_at(content)


def test_recent_window_matches_graph_discovery():
    """Discovery asks Graph for seven days; the sweep must not look further."""
    assert RECENT_SECONDS == 7 * 86400
