import os
from pathlib import Path
from uuid import uuid4

import pytest

from app.activity import queue_notification
from app.store import Store


@pytest.mark.skipif(not os.getenv("TEST_DATABASE_URL"), reason="PostgreSQL is not configured")
def test_postgres_store_round_trip():
    store = Store(Path("/tmp/unused.sqlite3"), database_url=os.environ["TEST_DATABASE_URL"])
    user_id = str(uuid4())
    store.enroll(user_id, "PostgreSQL test")
    token = store.session(user_id)
    assert store.session_user(token)["id"] == user_id

    store.save_meeting(user_id, "First", {"meeting_id": "meeting", "insight": {"id": "i1"}})
    store.save_meeting(user_id, "Updated", {"meeting_id": "meeting", "insight": {"id": "i2"}})
    meeting = store.meetings(user_id)[0]
    assert meeting["subject"] == "Updated"
    assert len(meeting["content"]["insights"]) == 2

    transcript_id = store.save_transcript(user_id, "meeting", "transcript", "hello")
    assert store.transcript(user_id, transcript_id) == "hello"

    store.enqueue(['{"type":"test"}'])
    job = store.claim_job()
    assert job["payload"] == '{"type":"test"}'
    store.finish_job(job["id"], "done")

    queue_notification(store, user_id, "transcript:meeting", "First", "Ready")
    assert store.next_notification()["event_key"] == "transcript:meeting"
    store.disconnect(user_id)


@pytest.mark.skipif(not os.getenv("TEST_DATABASE_URL"), reason="PostgreSQL is not configured")
def test_postgres_reports_rowcount_for_a_conditional_update():
    """The duplicate cleanup reads rowcount to detect a row changed underneath.

    Production runs PostgreSQL, so a cursor that silently lacks rowcount would
    only surface there -- the SQLite cursor has it natively.
    """
    store = Store(Path("/tmp/unused.sqlite3"), database_url=os.environ["TEST_DATABASE_URL"])
    user_id = str(uuid4())
    store.enroll(user_id, "PostgreSQL test")
    store.save_meeting(user_id, "Subject", {"meeting_id": "meeting"})
    with store.connect() as db:
        row = db.execute(
            "SELECT id, content FROM meetings WHERE user_id=?", (user_id,)
        ).fetchone()
        matched = db.execute(
            "UPDATE meetings SET subject=? WHERE id=? AND content=?",
            ("Cleaned", row["id"], row["content"]),
        ).rowcount
        stale = db.execute(
            "UPDATE meetings SET subject=? WHERE id=? AND content=?",
            ("Ignored", row["id"], '{"stale": true}'),
        ).rowcount
    assert (matched, stale) == (1, 0)
    assert store.meetings(user_id)[0]["subject"] == "Cleaned"
    store.disconnect(user_id)
