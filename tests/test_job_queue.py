"""Queue ordering: a published artifact must not wait behind polling churn."""

import sqlite3

from app.models import InsightEvent, MeetingSync, SessionInsightEvent, TranscriptEvent, UserSync
from app.store import Store, job_priority
from tests.conftest import USER


def payloads():
    return {
        "insight": InsightEvent(user_id=USER, meeting_id="m", insight_id="i").model_dump_json(),
        "transcript": TranscriptEvent(
            user_id=USER, meeting_id="m", transcript_id="t"
        ).model_dump_json(),
        "metadata_repair": TranscriptEvent(
            user_id=USER, meeting_id="m", transcript_id="t", metadata_only=True
        ).model_dump_json(),
        "session_insight": SessionInsightEvent(
            user_id=USER, meeting_id="m", occurrence_id="call:c", provider="openai"
        ).model_dump_json(),
        "meeting_sync": MeetingSync(user_id=USER, meeting_id="m").model_dump_json(),
        "user_sync": UserSync(user_id=USER).model_dump_json(),
    }


def test_content_fetches_outrank_polling():
    kinds = payloads()
    assert job_priority(kinds["insight"]) == 0
    assert job_priority(kinds["transcript"]) == 0
    assert job_priority(kinds["meeting_sync"]) == 1
    assert job_priority(kinds["user_sync"]) == 1
    assert job_priority(kinds["metadata_repair"]) == 2
    assert job_priority(kinds["session_insight"]) == 2


def test_unparseable_payload_is_treated_as_low_priority():
    # A malformed row must never jump the queue, and must never raise here.
    assert job_priority("not json") == 1
    assert job_priority("") == 1


def test_insight_is_claimed_before_polling_queued_earlier(store):
    """The production case: an insight queued last, behind a minute of polling."""
    kinds = payloads()
    store.enqueue([kinds["meeting_sync"], kinds["user_sync"]])
    store.enqueue([kinds["insight"]])
    assert store.claim_job()["payload"] == kinds["insight"]
    assert store.claim_job()["payload"] == kinds["meeting_sync"]
    assert store.claim_job()["payload"] == kinds["user_sync"]
    assert store.claim_job() is None


def test_next_job_uses_the_same_order_as_claim(store):
    kinds = payloads()
    store.enqueue([kinds["meeting_sync"], kinds["transcript"]])
    assert store.next_job()["payload"] == kinds["transcript"]


def test_equal_priority_stays_first_in_first_out(store):
    first = TranscriptEvent(user_id=USER, meeting_id="m", transcript_id="a").model_dump_json()
    second = TranscriptEvent(user_id=USER, meeting_id="m", transcript_id="b").model_dump_json()
    store.enqueue([first])
    store.enqueue([second])
    assert store.claim_job()["payload"] == first
    assert store.claim_job()["payload"] == second


def test_database_predating_the_priority_column_is_migrated(config, tmp_path):
    """Existing deployments have a jobs table without the column."""
    path = tmp_path / "legacy.sqlite3"
    legacy = sqlite3.connect(path)
    legacy.execute(
        """CREATE TABLE jobs (
            id INTEGER PRIMARY KEY, payload TEXT NOT NULL, attempts INTEGER DEFAULT 0,
            due REAL NOT NULL, status TEXT NOT NULL DEFAULT 'pending')"""
    )
    legacy.execute("INSERT INTO jobs(payload, due) VALUES ('{\"stale\": 1}', 0)")
    legacy.commit()
    legacy.close()

    store = Store(path)
    store.enroll(USER, "Demo organizer")
    with store.connect() as db:
        columns = {row[1] for row in db.execute("PRAGMA table_info(jobs)").fetchall()}
    assert "priority" in columns
    # The pre-existing row defaults to polling priority and still runs last.
    store.enqueue([payloads()["insight"]])
    assert store.claim_job()["payload"] == payloads()["insight"]
    assert store.claim_job()["payload"] == '{"stale": 1}'


def test_retry_keeps_the_fetch_ahead_of_polling(store):
    """A failed insight fetch must not lose its place to polling on retry."""
    kinds = payloads()
    store.enqueue([kinds["insight"]])
    job = store.claim_job()
    store.retry_job(job)
    store.enqueue([kinds["meeting_sync"]])
    with store.connect() as db:
        db.execute("UPDATE jobs SET due=0 WHERE payload=?", (kinds["insight"],))
    assert store.claim_job()["payload"] == kinds["insight"]
