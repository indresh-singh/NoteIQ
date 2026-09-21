import json
import os
import time
from datetime import datetime, timezone
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

    queue_notification(store, user_id, "insight:meeting", "First", "Ready")
    assert store.next_notification()["event_key"] == "insight:meeting"
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
        row = db.execute("SELECT id, content FROM meetings WHERE user_id=?", (user_id,)).fetchone()
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


@pytest.mark.skipif(not os.getenv("TEST_DATABASE_URL"), reason="PostgreSQL is not configured")
def test_postgres_projections_and_retention():
    """The paths where the two backends use different SQL.

    transcript_aliases projects the transcripts array with a jsonb operator
    rather than SQLite's json_extract, and psycopg decodes the result to a list
    where SQLite hands back text. sync_candidates and prune run partial-index
    and COALESCE predicates that only production exercises.
    """
    store = Store(Path("/tmp/unused.sqlite3"), database_url=os.environ["TEST_DATABASE_URL"])
    user_id = str(uuid4())
    store.enroll(user_id, "PostgreSQL test")

    store.save_meeting(
        user_id,
        "Waiting",
        {
            "meeting_id": "waiting",
            "transcript": {
                "id": "listing-name",
                "source_id": "detail-name",
                "createdDateTime": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            },
        },
    )
    store.save_meeting(user_id, "Upload", {"meeting_id": "upload", "source": "upload"})

    assert store.transcript_aliases(user_id) == {
        ("waiting", "listing-name"),
        ("waiting", "detail-name"),
    }
    assert store.sync_candidates(user_id, since=0) == ["waiting"]
    assert store.find_meeting(user_id, "waiting")["transcript"]["id"] == "listing-name"

    with store.connect() as db:
        db.execute("UPDATE meetings SET settled=1 WHERE meeting_id='waiting'")
    assert store.sync_candidates(user_id, since=0) == []
    assert store.sync_candidates(user_id, since=0, only_unsettled=False) == ["waiting"]

    store.save_transcript(user_id, "waiting", "t", "hello")
    with store.connect() as db:
        db.execute("UPDATE meetings SET created=0, occurred_at=NULL WHERE user_id=?", (user_id,))
    removed = store.prune(meeting_days=7)
    assert (removed["meetings"], removed["transcripts"]) == (2, 1)

    store.disconnect(user_id)


@pytest.mark.skipif(not os.getenv("TEST_DATABASE_URL"), reason="PostgreSQL is not configured")
def test_postgres_backfills_rows_written_before_the_columns():
    store = Store(Path("/tmp/unused.sqlite3"), database_url=os.environ["TEST_DATABASE_URL"])
    user_id = str(uuid4())
    store.enroll(user_id, "PostgreSQL test")
    content = {"meeting_id": "legacy", "transcript": {"id": "t"}, "insight": {"id": "i"}}
    with store.connect() as db:
        db.execute(
            "INSERT INTO meetings(user_id, subject, content, created) VALUES (?, ?, ?, ?)",
            (user_id, "Legacy", json.dumps(content), time.time()),
        )
    # Reopening runs the backfill, as a new revision's first start does.
    Store(Path("/tmp/unused.sqlite3"), database_url=os.environ["TEST_DATABASE_URL"])
    with store.connect() as db:
        row = db.execute(
            "SELECT meeting_id, settled FROM meetings WHERE user_id=?", (user_id,)
        ).fetchone()
    assert (row["meeting_id"], row["settled"]) == ("legacy", 1)
    store.disconnect(user_id)
