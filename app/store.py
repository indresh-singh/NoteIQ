"""Persistent storage backed by PostgreSQL in Azure or SQLite for local work."""

import hashlib
import json
import logging
import os
import secrets
import shutil
import sqlite3
import tempfile
import threading
import time
from contextlib import contextmanager, suppress
from datetime import datetime, timezone
from pathlib import Path

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from app.occurrences import select_session, transcript_version

log = logging.getLogger(__name__)
SESSION_IDLE_SECONDS = 8 * 3600

# Microsoft documents that insights "might take up to four hours to be available
# after the call ends". Past that, with a margin, a poll is no longer waiting for
# anything on a published schedule, and the webhook remains the way a straggler
# arrives. Without a bound, one transcript Copilot never summarises keeps its
# meeting in the sweep for the full seven-day retention.
PUBLICATION_WINDOW_HOURS = 6

# A terminal meeting sync result is "settled" in the polling sense: another
# minute-by-minute Graph call cannot make it actionable. Manual Refresh still
# deliberately rechecks settled meetings, so a later ownership/access change
# can recover without a database edit.
TERMINAL_MEETING_SYNC_STATUSES = {
    "SKIPPED_ACCESS_DENIED",
    "SKIPPED_GRAPH_REJECTED",
    "SKIPPED_NOT_ORGANIZER",
    "SKIPPED_ORGANIZER_UNVERIFIED",
}


def _meeting_occurred_at(content: dict) -> float | None:
    """Best-effort real-world timestamp for a meeting.

    Used to sort the meetings list by when the meeting actually happened,
    not by when we last wrote to its row — otherwise regenerating an old
    meeting's insight (or Copilot delivering one late) would bump it to the
    top of the list ahead of meetings that happened more recently.
    """
    dates = []
    metadata = content.get("meeting_metadata") or {}
    dates.extend(
        value
        for value in (metadata.get("start_date_time"), metadata.get("end_date_time"))
        if value
    )
    transcripts = content.get("transcripts") or (
        [{"transcript": content["transcript"]}] if content.get("transcript") else []
    )
    for item in transcripts:
        transcript = item.get("transcript") or {}
        # endDateTime describes the session. createdDateTime is only a fallback
        # for older records that did not retain the session's own timestamps.
        occurred = transcript.get("endDateTime") or transcript.get("createdDateTime")
        if occurred:
            dates.append(occurred)
    parsed = []
    for value in dates:
        try:
            stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=timezone.utc)
            parsed.append(stamp.timestamp())
        except ValueError:
            continue
    return max(parsed) if parsed else None


def bodies(content: dict, kind: str) -> list[dict]:
    """The stored records for one artifact kind, whichever shape they are in."""
    entries = content.get(kind + "s")
    if entries:
        return [entry.get(kind) or {} for entry in entries]
    return [content[kind]] if content.get(kind) else []


def settled(content: dict) -> bool:
    """True once every transcript has its insight, leaving polling nothing to find.

    Graph creates one insight per transcript event -- "each transcript event of
    the meeting creates an associated AI insight object" -- so a meeting whose
    transcription was stopped and restarted is not finished at its first
    insight. Where both sides carry contentCorrelationId, which Graph defines as
    correlating an insight to the transcript it came from, pair on it: that says
    *which* transcript is still unsummarised rather than merely how many are.
    Rows written before we recorded it fall back to counting.

    Revisions are a separate matter -- Copilot rewrites an insight under its
    existing id, which a poll reports as already known, so only the webhook ever
    delivers those.
    """
    if content.get("sync_status") in TERMINAL_MEETING_SYNC_STATUSES:
        return True
    transcripts = bodies(content, "transcript")
    if not transcripts:
        return False
    # Only Copilot insights count. OpenRouter summarises locally the moment a
    # transcript lands, so counting it would mark every meeting finished before
    # Copilot -- the thing polling is actually waiting for -- ever publishes.
    copilot = [
        body
        for body in bodies(content, "insight")
        if (body.get("provider") or "copilot") == "copilot"
    ]
    wanted = [body.get("contentCorrelationId") for body in transcripts]
    if all(wanted):
        # An insight missing its correlation id simply fails to match, which
        # keeps polling alive rather than declaring a transcript covered.
        have = {body.get("contentCorrelationId") for body in copilot}
        return set(wanted) <= have
    return len(copilot) >= len(transcripts)


def newest_transcript_at(content: dict) -> float | None:
    """When the most recent transcript segment was created, as an epoch time.

    None -- meaning "keep polling" to every caller -- whenever the answer cannot
    be trusted: no transcript yet, or a timestamp we cannot read. The publication
    window is an optimisation, and abandoning a meeting early is the one failure
    it must not have.
    """
    stamps = []
    for body in bodies(content, "transcript"):
        value = body.get("createdDateTime")
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
        # A naive timestamp is UTC, as everywhere else Graph values are read;
        # .timestamp() would otherwise read it as local time.
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        stamps.append(parsed.timestamp())
    return max(stamps) if stamps else None


def meeting_facts(content: dict) -> dict:
    """The columns derived from a meeting's content.

    The polling sweep asks "which meetings are still worth a Graph call?" once a
    minute per user. Answering that from the JSON means reading every saved card
    and summary to look at four fields, so the answer is computed once per write
    and stored beside the row instead.
    """
    return {
        "meeting_id": content.get("meeting_id"),
        "source": content.get("source"),
        "settled": 1 if settled(content) else 0,
        "newest_transcript_at": newest_transcript_at(content),
        "occurred_at": _meeting_occurred_at(content),
    }


# Valid on both backends, so the two schemas cannot drift apart. meetings_recent
# indexes the expression the listing actually orders by: an index on `created`
# alone cannot serve `COALESCE(occurred_at, created)`, so every listing sorted
# the user's whole history to return its first hundred rows.
INDEXES = (
    "CREATE INDEX IF NOT EXISTS jobs_ready ON jobs(status, due, id)",
    """CREATE UNIQUE INDEX IF NOT EXISTS jobs_pending_payload
    ON jobs(payload) WHERE status='pending'""",
    "CREATE INDEX IF NOT EXISTS activity_ready ON activity_outbox(status, due, id)",
    """CREATE INDEX IF NOT EXISTS meetings_recent
    ON meetings(user_id, COALESCE(occurred_at, created) DESC, id DESC)""",
    "CREATE INDEX IF NOT EXISTS meetings_lookup ON meetings(user_id, meeting_id)",
    """CREATE INDEX IF NOT EXISTS meetings_sync ON meetings(user_id, created)
    WHERE settled=0""",
)


class Row(dict):
    """A small row type compatible with SQLite's named and numeric access."""

    def __getitem__(self, key):
        if isinstance(key, int):
            return tuple(self.values())[key]
        return super().__getitem__(key)


class PostgresCursor:
    def __init__(self, cursor):
        self.cursor = cursor

    def fetchone(self):
        row = self.cursor.fetchone()
        return Row(row) if row else None

    def fetchall(self):
        return [Row(row) for row in self.cursor.fetchall()]

    @property
    def rowcount(self):
        """How many rows the statement matched, as sqlite3 already reports.

        A conditional UPDATE reads as "did my version still hold?", which is how
        the duplicate cleanup avoids overwriting a row the worker just changed.
        """
        return self.cursor.rowcount

    def __iter__(self):
        return iter(self.fetchall())


class PostgresConnection:
    """Expose the tiny DB-API surface used by NoteIQ."""

    def __init__(self, connection):
        self.connection = connection

    @staticmethod
    def sql(statement: str) -> str:
        statement = statement.replace("?", "%s")
        if "INSERT OR IGNORE" in statement:
            statement = statement.replace("INSERT OR IGNORE", "INSERT")
            statement = statement.rstrip().rstrip(";") + " ON CONFLICT DO NOTHING"
        return statement

    def execute(self, statement: str, parameters=()):
        cursor = self.connection.execute(self.sql(statement), parameters)
        return PostgresCursor(cursor)

    def executemany(self, statement: str, parameters):
        cursor = self.connection.cursor()
        cursor.executemany(self.sql(statement), parameters)
        return PostgresCursor(cursor)


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def artifact_aliases(body: dict) -> set[str]:
    """Every name one transcript or insight is known by.

    Graph hands out two ids for the same transcript -- getAllTranscripts and a
    meeting's own /transcripts list disagree -- so an artifact is a duplicate
    when *either* name matches, not only the one we happened to store first.
    Insights carry a content fingerprint too: an OpenRouter summary is keyed by
    the transcript that produced it, so two aliased transcripts yield two
    insights that share no id at all and are otherwise identical.
    """
    aliases = {str(body[key]) for key in ("id", "source_id") if body.get(key)}
    notes, actions = body.get("meetingNotes"), body.get("actionItems")
    if notes or actions:
        # Scoped by provider: Copilot and OpenRouter summarising the same
        # meeting alike is the comparison the product exists to show, not a
        # duplicate, so their fingerprints must never collide.
        provider = body.get("provider") or "copilot"
        fingerprint = json.dumps(
            [provider, body.get("occurrence_id"), body.get("contentCorrelationId"), notes, actions],
            sort_keys=True,
            default=str,
        )
        aliases.add("sha:" + digest(fingerprint))
    return aliases


def dedupe_items(items: list) -> list:
    """Drop repeated notes or action items from within one summary.

    A model can list the same point several times in a single reply -- more
    likely on the free tiers OpenRouter falls back to -- and nothing upstream
    removes it, so the repetition reaches the card verbatim. Matching is exact:
    anything looser risks deleting two genuinely different points that happen
    to read alike.
    """
    seen, kept = set(), []
    for item in items:
        fingerprint = json.dumps(item, sort_keys=True, default=str)
        if fingerprint not in seen:
            seen.add(fingerprint)
            kept.append(item)
    return kept


def dedupe_content(content: dict) -> tuple[dict, dict]:
    """Collapse aliased transcripts and insights, and repetition within each.

    Pure: no database access, so the same logic guards every write in
    save_meeting and backfills rows written before that guard existed.
    """
    cleaned, report = dict(content), {}
    for kind, field in (("insight", "insights"), ("transcript", "transcripts")):
        entries = content.get(field) or []
        kept: list[dict] = []
        index: dict[str, int] = {}
        for entry in entries:
            aliases = artifact_aliases(entry.get(kind) or {})
            at = next((index[alias] for alias in aliases if alias in index), None)
            if at is None:
                at = len(kept)
                kept.append(entry)
            else:
                # Later write wins: Copilot revises an insight in place, and the
                # freshest copy is the one worth keeping.
                kept[at] = entry
            index.update(dict.fromkeys(aliases, at))
        if len(kept) != len(entries):
            report[field] = {"before": len(entries), "after": len(kept)}
        if kind == "insight":
            kept = _dedupe_within(kept, report)
        if entries:
            cleaned[field] = kept
            # The singular mirror must name a survivor or _meeting_occurred_at
            # reads a record that is no longer in the array.
            if kind in cleaned:
                cleaned[kind] = kept[-1][kind]
    return cleaned, report


def _dedupe_within(entries: list[dict], report: dict) -> list[dict]:
    """Clean each surviving insight's own note and action lists.

    Copies rather than edits in place: dedupe_content is pure, and its callers
    still hold the dicts these entries came from.
    """
    result = []
    for entry in entries:
        body = entry.get("insight") or {}
        replacements = {}
        for name in ("meetingNotes", "actionItems"):
            items = body.get(name)
            if not items:
                continue
            kept = dedupe_items(items)
            if len(kept) != len(items):
                counts = report.setdefault(name, {"before": 0, "after": 0})
                counts["before"] += len(items)
                counts["after"] += len(kept)
                replacements[name] = kept
        result.append({**entry, "insight": {**body, **replacements}} if replacements else entry)
    return result


def job_priority(payload: str) -> int:
    """Fetching content outranks polling for it.

    A queued transcript or insight is work Graph has already published; a
    MeetingSync or UserSync is only a question about whether anything exists.
    Without this, one artifact waits behind a minute's worth of polling for
    every meeting the user has, which is how a five-second fetch becomes a
    two-minute one.
    """
    try:
        fields = json.loads(payload)
    except ValueError:
        return 1
    return 0 if "insight_id" in fields or "transcript_id" in fields else 1


class Store:
    def __init__(self, path: Path, backup_path: Path | None = None, database_url=None):
        self.path = path
        self.backup_path = backup_path
        self.database_url = (
            database_url.get_secret_value()
            if hasattr(database_url, "get_secret_value")
            else database_url
        )
        self._backup_lock = threading.Lock()
        if self.database_url:
            self.backup_path = None
            self.pool = ConnectionPool(
                self.database_url,
                min_size=1,
                max_size=5,
                kwargs={"row_factory": dict_row},
                open=True,
            )
            self._create_postgres_schema()
            log.info(
                "Storage initialized backend=postgresql pool_min=%s pool_max=%s backup_enabled=false",
                1,
                5,
            )
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        self._restore_if_needed()
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS users (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'CONNECTING', enabled INTEGER NOT NULL DEFAULT 1
                );
                CREATE TABLE IF NOT EXISTS temporary (
                    kind TEXT, key TEXT, value TEXT NOT NULL, expires REAL NOT NULL,
                    PRIMARY KEY (kind, key)
                );
                CREATE TABLE IF NOT EXISTS sessions (
                    token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL, expires REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS jobs (
                    id INTEGER PRIMARY KEY, payload TEXT NOT NULL, attempts INTEGER DEFAULT 0,
                    due REAL NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
                    priority INTEGER NOT NULL DEFAULT 1
                );
                CREATE TABLE IF NOT EXISTS meetings (
                    id INTEGER PRIMARY KEY, user_id TEXT NOT NULL, subject TEXT NOT NULL,
                    content TEXT NOT NULL, created REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS transcripts (
                    id INTEGER PRIMARY KEY, user_id TEXT NOT NULL, meeting_id TEXT NOT NULL,
                    transcript_id TEXT NOT NULL, content TEXT NOT NULL,
                    UNIQUE(user_id, meeting_id, transcript_id)
                );
                CREATE TABLE IF NOT EXISTS activity_outbox (
                    id INTEGER PRIMARY KEY, user_id TEXT NOT NULL, event_key TEXT NOT NULL,
                    subject TEXT NOT NULL, message TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
                    due REAL NOT NULL, UNIQUE(user_id, event_key)
                );
                CREATE TABLE IF NOT EXISTS subscriptions (
                    user_id TEXT NOT NULL, resource_kind TEXT NOT NULL,
                    subscription_id TEXT NOT NULL, expires_at REAL NOT NULL,
                    PRIMARY KEY (user_id, resource_kind)
                );
                CREATE TABLE IF NOT EXISTS clickup_connections (
                    user_id TEXT PRIMARY KEY, token TEXT NOT NULL, list_id TEXT, workspaces TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS clickup_lists (
                    user_id TEXT NOT NULL, list_id TEXT NOT NULL, list_name TEXT NOT NULL,
                    PRIMARY KEY (user_id, list_id)
                );
                CREATE TABLE IF NOT EXISTS clickup_tasks (
                    user_id TEXT NOT NULL, action_key TEXT NOT NULL, task_id TEXT NOT NULL,
                    task_url TEXT, PRIMARY KEY (user_id, action_key)
                );
                -- Optional encrypted delegated MSAL cache, separate from saved plan targets.
                CREATE TABLE IF NOT EXISTS planner_connections (
                    user_id TEXT PRIMARY KEY, cache TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS planner_defaults (
                    user_id TEXT PRIMARY KEY, plan_id TEXT, plan_name TEXT
                );
                CREATE TABLE IF NOT EXISTS planner_plans (
                    user_id TEXT NOT NULL, plan_id TEXT NOT NULL, plan_name TEXT NOT NULL,
                    PRIMARY KEY (user_id, plan_id)
                );
                CREATE TABLE IF NOT EXISTS planner_tasks (
                    user_id TEXT NOT NULL, action_key TEXT NOT NULL, task_id TEXT NOT NULL,
                    task_url TEXT, PRIMARY KEY (user_id, action_key)
                );
                -- Retire delegated chat credentials and pending sends from the prior prototype.
                DROP TABLE IF EXISTS chat_connections;
                DROP TABLE IF EXISTS outbox;
            """)
            # SQLite has no ADD COLUMN IF NOT EXISTS; older databases lack these columns.
            for statement in (
                "ALTER TABLE clickup_connections ADD COLUMN list_name TEXT",
                "ALTER TABLE meetings ADD COLUMN occurred_at REAL",
                "ALTER TABLE jobs ADD COLUMN priority INTEGER NOT NULL DEFAULT 1",
                # Derived from content; see meeting_facts. Defaulting settled to 0
                # keeps a row nothing has backfilled yet in the sweep rather than
                # silently dropping it.
                "ALTER TABLE meetings ADD COLUMN meeting_id TEXT",
                "ALTER TABLE meetings ADD COLUMN source TEXT",
                "ALTER TABLE meetings ADD COLUMN settled INTEGER NOT NULL DEFAULT 0",
                "ALTER TABLE meetings ADD COLUMN newest_transcript_at REAL",
            ):
                with suppress(sqlite3.OperationalError):
                    db.execute(statement)
            for statement in INDEXES:
                db.execute(statement)
        self.backfill_meeting_facts()
        # Some managed volume drivers set permissions at mount time and do not implement chmod.
        with suppress(OSError):
            path.chmod(0o600)
        log.info(
            "Storage initialized backend=sqlite path=%s backup_enabled=%s",
            path,
            self.backup_path is not None,
        )

    def _create_postgres_schema(self):
        statements = (
            """CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY, name TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'CONNECTING', enabled INTEGER NOT NULL DEFAULT 1)""",
            """CREATE TABLE IF NOT EXISTS temporary (
                kind TEXT, key TEXT, value TEXT NOT NULL, expires DOUBLE PRECISION NOT NULL,
                PRIMARY KEY (kind, key))""",
            """CREATE TABLE IF NOT EXISTS sessions (
                token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL, expires DOUBLE PRECISION NOT NULL)""",
            """CREATE TABLE IF NOT EXISTS jobs (
                id BIGSERIAL PRIMARY KEY, payload TEXT NOT NULL, attempts INTEGER DEFAULT 0,
                due DOUBLE PRECISION NOT NULL, status TEXT NOT NULL DEFAULT 'pending')""",
            "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS priority INTEGER NOT NULL DEFAULT 1",
            """CREATE TABLE IF NOT EXISTS meetings (
                id BIGSERIAL PRIMARY KEY, user_id TEXT NOT NULL, subject TEXT NOT NULL,
                content TEXT NOT NULL, created DOUBLE PRECISION NOT NULL)""",
            "ALTER TABLE meetings ADD COLUMN IF NOT EXISTS occurred_at DOUBLE PRECISION",
            # Derived from content; see meeting_facts. Defaulting settled to 0
            # keeps a row nothing has backfilled yet in the sweep rather than
            # silently dropping it.
            "ALTER TABLE meetings ADD COLUMN IF NOT EXISTS meeting_id TEXT",
            "ALTER TABLE meetings ADD COLUMN IF NOT EXISTS source TEXT",
            "ALTER TABLE meetings ADD COLUMN IF NOT EXISTS settled INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE meetings ADD COLUMN IF NOT EXISTS newest_transcript_at DOUBLE PRECISION",
            """CREATE TABLE IF NOT EXISTS transcripts (
                id BIGSERIAL PRIMARY KEY, user_id TEXT NOT NULL, meeting_id TEXT NOT NULL,
                transcript_id TEXT NOT NULL, content TEXT NOT NULL,
                UNIQUE(user_id, meeting_id, transcript_id))""",
            """CREATE TABLE IF NOT EXISTS activity_outbox (
                id BIGSERIAL PRIMARY KEY, user_id TEXT NOT NULL, event_key TEXT NOT NULL,
                subject TEXT NOT NULL, message TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
                due DOUBLE PRECISION NOT NULL, UNIQUE(user_id, event_key))""",
            """CREATE TABLE IF NOT EXISTS subscriptions (
                user_id TEXT NOT NULL, resource_kind TEXT NOT NULL,
                subscription_id TEXT NOT NULL, expires_at DOUBLE PRECISION NOT NULL,
                PRIMARY KEY (user_id, resource_kind))""",
            """CREATE TABLE IF NOT EXISTS clickup_connections (
                user_id TEXT PRIMARY KEY, token TEXT NOT NULL, list_id TEXT, workspaces TEXT NOT NULL)""",
            "ALTER TABLE clickup_connections ADD COLUMN IF NOT EXISTS list_name TEXT",
            """CREATE TABLE IF NOT EXISTS clickup_lists (
                user_id TEXT NOT NULL, list_id TEXT NOT NULL, list_name TEXT NOT NULL,
                PRIMARY KEY (user_id, list_id))""",
            """CREATE TABLE IF NOT EXISTS clickup_tasks (
                user_id TEXT NOT NULL, action_key TEXT NOT NULL, task_id TEXT NOT NULL,
                task_url TEXT, PRIMARY KEY (user_id, action_key))""",
            """CREATE TABLE IF NOT EXISTS planner_connections (
                user_id TEXT PRIMARY KEY, cache TEXT NOT NULL)""",
            """CREATE TABLE IF NOT EXISTS planner_defaults (
                user_id TEXT PRIMARY KEY, plan_id TEXT, plan_name TEXT)""",
            """CREATE TABLE IF NOT EXISTS planner_plans (
                user_id TEXT NOT NULL, plan_id TEXT NOT NULL, plan_name TEXT NOT NULL,
                PRIMARY KEY (user_id, plan_id))""",
            """CREATE TABLE IF NOT EXISTS planner_tasks (
                user_id TEXT NOT NULL, action_key TEXT NOT NULL, task_id TEXT NOT NULL,
                task_url TEXT, PRIMARY KEY (user_id, action_key))""",
            "DROP TABLE IF EXISTS chat_connections",
            "DROP TABLE IF EXISTS outbox",
            # Superseded by meetings_recent, which matches the listing's ORDER BY.
            "DROP INDEX IF EXISTS meetings_user",
            *INDEXES,
        )
        with self.connect() as db:
            for statement in statements:
                db.execute(statement)
        self.backfill_meeting_facts()

    def backfill_meeting_facts(self):
        """Populate the derived columns for rows written before they existed.

        Keyed on meeting_id being NULL, so this is a no-op on every start after
        the first. A row whose content has no meeting_id at all cannot be matched
        by the sweep either way; it is written back with its other facts so the
        scan does not keep finding it.
        """
        with self.connect() as db:
            rows = db.execute(
                "SELECT id, content FROM meetings WHERE meeting_id IS NULL"
            ).fetchall()
            if not rows:
                return
            db.executemany(
                """UPDATE meetings SET meeting_id=?, source=?, settled=?,
                newest_transcript_at=?, occurred_at=COALESCE(occurred_at, ?) WHERE id=?""",
                [
                    (
                        facts["meeting_id"] or "",
                        facts["source"],
                        facts["settled"],
                        facts["newest_transcript_at"],
                        facts["occurred_at"],
                        row["id"],
                    )
                    for row in rows
                    for facts in [meeting_facts(json.loads(row["content"]))]
                ],
            )
        log.info("Backfilled meeting sync columns for row_count=%s", len(rows))

    @contextmanager
    def connect(self):
        if self.database_url:
            with self.pool.connection() as connection:
                yield PostgresConnection(connection)
            return
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
            if db.total_changes:
                self._save_backup(db)
        finally:
            db.close()

    def close(self):
        if self.database_url:
            self.pool.close()
        log.info("Storage closed backend=%s", "postgresql" if self.database_url else "sqlite")

    def _restore_if_needed(self):
        """Start a new container from the last consistent mounted snapshot."""
        if not self.backup_path or self.path.exists() or not self.backup_path.exists():
            return
        temporary = self.path.with_name(self.path.name + ".restore")
        try:
            shutil.copyfile(self.backup_path, temporary)
            # A connection context manager commits/rolls back but does not
            # close the handle, which makes replace/unlink fail on Windows.
            db = sqlite3.connect(temporary)
            try:
                if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise RuntimeError("database integrity check failed")
                if not db.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='users'"
                ).fetchone():
                    raise RuntimeError("database snapshot has no NoteIQ schema")
            finally:
                db.close()
            os.replace(temporary, self.path)
            log.info("Restored NoteIQ database snapshot")
        except Exception as error:
            temporary.unlink(missing_ok=True)
            raise RuntimeError("Unable to restore NoteIQ database snapshot") from error

    def _save_backup(self, db: sqlite3.Connection):
        """Copy an SQLite-consistent snapshot to the mounted Azure Files volume."""
        if not self.backup_path:
            return
        with self._backup_lock:
            self.backup_path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=self.path.parent, delete=False) as file:
                temporary = Path(file.name)
            try:
                # sqlite3.Connection.__exit__ commits but does not close. That
                # unnoticed distinction prevents unlinking this file on Windows.
                copy = sqlite3.connect(temporary)
                try:
                    db.backup(copy)
                finally:
                    copy.close()
                staged = self.backup_path.with_name(self.backup_path.name + ".next")
                shutil.copyfile(temporary, staged)
                os.replace(staged, self.backup_path)
            except OSError:
                log.exception("NoteIQ database snapshot failed")
            finally:
                temporary.unlink(missing_ok=True)

    def healthy(self) -> bool:
        with self.connect() as db:
            return db.execute("SELECT 1").fetchone()[0] == 1

    def put(self, kind: str, key: str, value: dict, ttl: int = 600):
        with self.connect() as db:
            db.execute("DELETE FROM temporary WHERE expires < ?", (time.time(),))
            if self.database_url:
                db.execute(
                    """INSERT INTO temporary(kind, key, value, expires) VALUES (?, ?, ?, ?)
                    ON CONFLICT(kind, key) DO UPDATE SET value=excluded.value,
                    expires=excluded.expires""",
                    (kind, key, json.dumps(value), time.time() + ttl),
                )
            else:
                db.execute(
                    "INSERT OR REPLACE INTO temporary VALUES (?, ?, ?, ?)",
                    (kind, key, json.dumps(value), time.time() + ttl),
                )

    def get(self, kind: str, key: str) -> dict | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT value FROM temporary WHERE kind=? AND key=? AND expires>?",
                (kind, key, time.time()),
            ).fetchone()
        return json.loads(row["value"]) if row else None

    def pop(self, kind: str, key: str) -> dict | None:
        with self.connect() as db:
            row = db.execute(
                "DELETE FROM temporary WHERE kind=? AND key=? AND expires>? RETURNING value",
                (kind, key, time.time()),
            ).fetchone()
        return json.loads(row["value"]) if row else None

    def claim(self, kind: str, key: str, value: dict, ttl: int) -> bool:
        """Take kind/key only while nobody holds a live one: a lock shared by replicas.

        Check and write are one statement, so two requests arriving together
        cannot both see it free. An expired holder is taken over rather than
        waited on, which is what keeps a crashed request from locking the key
        forever.
        """
        now = time.time()
        with self.connect() as db:
            row = db.execute(
                """INSERT INTO temporary(kind, key, value, expires) VALUES (?, ?, ?, ?)
                ON CONFLICT(kind, key) DO UPDATE SET value=excluded.value,
                expires=excluded.expires WHERE temporary.expires < ?
                RETURNING value""",
                (kind, key, json.dumps(value), now + ttl, now),
            ).fetchone()
        return row is not None

    def release(self, kind: str, key: str, value: dict) -> bool:
        """Drop a claim, but only the one this caller took.

        A holder that overran its ttl may already have been replaced; matching
        on the value it wrote keeps it from releasing its successor's claim.
        """
        with self.connect() as db:
            return (
                db.execute(
                    "DELETE FROM temporary WHERE kind=? AND key=? AND value=?",
                    (kind, key, json.dumps(value)),
                ).rowcount
                > 0
            )

    def pause_graph(self, seconds: float) -> None:
        """Record that Graph throttled the tenant, for every worker and replica to honour.

        Graph's limits are per tenant, so the pause is too. A pause is only ever
        extended: a short Retry-After arriving after a long one must not cut
        the longer one short.
        """
        until = time.time() + seconds
        with self.connect() as db:
            db.execute(
                """INSERT INTO temporary(kind, key, value, expires) VALUES (?, ?, ?, ?)
                ON CONFLICT(kind, key) DO UPDATE SET value=excluded.value,
                expires=excluded.expires WHERE temporary.expires < excluded.expires""",
                ("graph", "throttled", json.dumps({"until": until}), until),
            )

    def graph_paused_until(self) -> float:
        """When the current Graph throttle pause ends, or 0 when there is none."""
        pause = self.get("graph", "throttled")
        return pause["until"] if pause else 0.0

    def enroll(self, user_id: str, name: str):
        with self.connect() as db:
            db.execute(
                """INSERT INTO users(id, name) VALUES (?, ?)
                ON CONFLICT(id) DO UPDATE SET name=excluded.name, enabled=1,
                status=CASE WHEN users.enabled=0 THEN 'CONNECTING' ELSE users.status END""",
                (user_id, name),
            )

    def users(self) -> list[str]:
        with self.connect() as db:
            return [row[0] for row in db.execute("SELECT id FROM users WHERE enabled=1")]

    def user(self, user_id: str) -> dict | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        return dict(row) if row else None

    def status(self, user_id: str, status: str):
        with self.connect() as db:
            db.execute("UPDATE users SET status=? WHERE id=?", (status, user_id))

    def session(self, user_id: str) -> str:
        token = secrets.token_urlsafe(32)
        with self.connect() as db:
            db.execute("DELETE FROM sessions WHERE expires < ?", (time.time(),))
            db.execute(
                "INSERT INTO sessions VALUES (?, ?, ?)",
                (digest(token), user_id, time.time() + SESSION_IDLE_SECONDS),
            )
        return token

    def session_user(self, token: str) -> dict | None:
        with self.connect() as db:
            row = db.execute(
                """SELECT users.* FROM sessions JOIN users ON users.id=user_id
                WHERE token_hash=? AND expires>? AND enabled=1""",
                (digest(token), time.time()),
            ).fetchone()
        return dict(row) if row else None

    def logout(self, token: str):
        with self.connect() as db:
            db.execute("DELETE FROM sessions WHERE token_hash=?", (digest(token),))

    def renew_session(self, token: str) -> bool:
        """Extend a live session only; never resurrect expired or revoked access."""
        now = time.time()
        with self.connect() as db:
            return db.execute(
                """UPDATE sessions SET expires=? WHERE token_hash=? AND expires>?
                AND user_id IN (SELECT id FROM users WHERE enabled=1)""",
                (now + SESSION_IDLE_SECONDS, digest(token), now),
            ).rowcount == 1

    def disconnect(self, user_id: str):
        with self.connect() as db:
            db.execute("UPDATE users SET enabled=0, status='DISCONNECTED' WHERE id=?", (user_id,))
            db.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
            db.execute("DELETE FROM meetings WHERE user_id=?", (user_id,))
            db.execute("DELETE FROM transcripts WHERE user_id=?", (user_id,))
            db.execute("DELETE FROM activity_outbox WHERE user_id=?", (user_id,))
            db.execute("DELETE FROM subscriptions WHERE user_id=?", (user_id,))
            db.execute("DELETE FROM clickup_connections WHERE user_id=?", (user_id,))
            db.execute("DELETE FROM clickup_lists WHERE user_id=?", (user_id,))
            db.execute("DELETE FROM clickup_tasks WHERE user_id=?", (user_id,))
            db.execute("DELETE FROM planner_connections WHERE user_id=?", (user_id,))
            db.execute("DELETE FROM planner_defaults WHERE user_id=?", (user_id,))
            db.execute("DELETE FROM planner_plans WHERE user_id=?", (user_id,))
            db.execute("DELETE FROM planner_tasks WHERE user_id=?", (user_id,))

    def reconcile_subscriptions(self, rows: list[tuple[str, str, str, float]]):
        """Mirror Graph's own subscription list into the local table.

        Called once per renewal cycle with every currently-live subscription
        this app owns. An upsert rather than a replace, so a row this same
        cycle's renewal writes moments later (see save_subscription) is not
        clobbered by the listing that ran before it. Self-healing: a row lost
        to a crash, a fresh deploy, or drift from a manual change in Graph is
        rebuilt from the next listing rather than causing a duplicate create.
        """
        if not rows:
            return
        with self.connect() as db:
            db.executemany(
                """INSERT INTO subscriptions(user_id, resource_kind, subscription_id, expires_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(user_id, resource_kind) DO UPDATE SET
                    subscription_id=excluded.subscription_id, expires_at=excluded.expires_at""",
                rows,
            )

    def save_subscription(
        self, user_id: str, resource_kind: str, subscription_id: str, expires_at: float
    ):
        """Record the result of one successful create/renew, ahead of the next reconciliation."""
        self.reconcile_subscriptions([(user_id, resource_kind, subscription_id, expires_at)])

    def subscription_user(self, subscription_id: str, resource_kind: str) -> str | None:
        """Return the enrolled user that owns a known Graph subscription."""
        with self.connect() as db:
            row = db.execute(
                """SELECT u.id FROM subscriptions s
                JOIN users u ON u.id=s.user_id
                WHERE s.subscription_id=? AND s.resource_kind=? AND u.enabled=1""",
                (subscription_id, resource_kind),
            ).fetchone()
        return row["id"] if row else None

    def due_subscriptions(self, force: bool = False, within_minutes: float = 30) -> list[dict]:
        """Enrolled users' subscriptions worth a Graph call: missing, expiring soon, or forced.

        One indexed query in place of walking every enrolled user in Python and
        rescanning Graph's own subscription listing for each -- the O(users)
        cost that made a 1500-user renewal cycle take minutes instead of
        seconds. A user with no row here yet (never subscribed) is included via
        the LEFT JOIN, the same as one whose row has simply gone stale.
        """
        with self.connect() as db:
            rows = db.execute(
                """SELECT u.id AS user_id, k.kind AS resource_kind,
                    s.subscription_id AS subscription_id
                FROM users u
                CROSS JOIN (SELECT 'insights' AS kind UNION ALL SELECT 'transcripts') AS k
                LEFT JOIN subscriptions s ON s.user_id = u.id AND s.resource_kind = k.kind
                WHERE u.enabled = 1
                  AND (? OR s.expires_at IS NULL OR s.expires_at <= ?)""",
                (force, time.time() + within_minutes * 60),
            ).fetchall()
        return [dict(row) for row in rows]

    def flag_delayed_updates(self, within_seconds: float) -> list[str]:
        """Mark users UPDATES_DELAYED once a subscription has lapsed or is about to.

        Read from the stored expiry rather than from renewal errors, so it holds
        even while throttling keeps renewal from running at all. Normal renewal
        keeps every subscription at least 45 minutes from expiry, so reaching
        this means renewal has been failing for a while. Only the healthy
        statuses are replaced: a more specific problem already on screen stays.
        A successful renewal sets LISTENING again.
        """
        with self.connect() as db:
            rows = db.execute(
                """UPDATE users SET status='UPDATES_DELAYED'
                WHERE enabled=1 AND status IN ('LISTENING', 'CONNECTING')
                AND id IN (SELECT user_id FROM subscriptions WHERE expires_at <= ?)
                RETURNING id""",
                (time.time() + within_seconds,),
            ).fetchall()
        return [row["id"] for row in rows]

    def save_clickup(self, user_id: str, token: str, workspaces: list[dict]):
        with self.connect() as db:
            db.execute(
                """INSERT INTO clickup_connections(user_id, token, workspaces) VALUES (?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET token=excluded.token, workspaces=excluded.workspaces""",
                (user_id, token, json.dumps(workspaces)),
            )

    def clickup(self, user_id: str) -> dict | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM clickup_connections WHERE user_id=?", (user_id,)
            ).fetchone()
        if not row:
            return None
        result = dict(row)
        result["workspaces"] = json.loads(result["workspaces"])
        return result

    def set_clickup_list(self, user_id: str, list_id: str, list_name: str):
        with self.connect() as db:
            db.execute(
                "UPDATE clickup_connections SET list_id=?, list_name=? WHERE user_id=?",
                (list_id, list_name, user_id),
            )

    def add_clickup_list(self, user_id: str, list_id: str, list_name: str):
        with self.connect() as db:
            db.execute(
                """INSERT INTO clickup_lists(user_id, list_id, list_name) VALUES (?, ?, ?)
                ON CONFLICT(user_id, list_id) DO UPDATE SET list_name=excluded.list_name""",
                (user_id, list_id, list_name),
            )

    def clickup_lists(self, user_id: str) -> list[dict]:
        with self.connect() as db:
            return [
                dict(row)
                for row in db.execute(
                    "SELECT list_id, list_name FROM clickup_lists WHERE user_id=? "
                    "ORDER BY list_name",
                    (user_id,),
                ).fetchall()
            ]

    def remove_clickup_list(self, user_id: str, list_id: str):
        with self.connect() as db:
            db.execute(
                "DELETE FROM clickup_lists WHERE user_id=? AND list_id=?", (user_id, list_id)
            )
            db.execute(
                "UPDATE clickup_connections SET list_id=NULL, list_name=NULL "
                "WHERE user_id=? AND list_id=?",
                (user_id, list_id),
            )

    # Shared by every export target's task table (currently clickup_tasks and
    # planner_tasks): both need exactly the same "have I already sent this
    # action item?" bookkeeping, so the logic lives once and each provider
    # gets thin, differently-named wrappers below. `table` is always one of
    # our own hardcoded names, never user input, so interpolating it is safe.
    def _export_task_id(self, table: str, user_id: str, action_key: str) -> str | None:
        with self.connect() as db:
            row = db.execute(
                f"SELECT task_id FROM {table} WHERE user_id=? AND action_key=?",
                (user_id, action_key),
            ).fetchone()
        return row["task_id"] if row and row["task_id"] else None

    def _forget_export_task(self, table: str, user_id: str, action_key: str):
        """Drop a stale record so a task deleted on the provider's side can be re-exported."""
        with self.connect() as db:
            db.execute(
                f"DELETE FROM {table} WHERE user_id=? AND action_key=?", (user_id, action_key)
            )

    def _reserve_export_task(self, table: str, user_id: str, action_key: str) -> bool:
        """Atomically claim an action item before calling the provider's API.

        The check-then-act window between "was this already sent?" and the
        network call to create the task is otherwise wide enough for
        concurrent export requests to both pass the check and both create a
        duplicate task. Reserving the row first, inside a single statement,
        closes that window: only one concurrent caller can win the insert.
        Returns True if this call claimed it and should proceed to create the
        task; False if another call already claimed (or completed) it.
        """
        with self.connect() as db:
            if self.database_url:
                # ON CONFLICT must precede RETURNING; the generic "INSERT OR IGNORE"
                # translation appends ON CONFLICT at the end, which is invalid here.
                row = db.execute(
                    f"INSERT INTO {table}(user_id, action_key, task_id) VALUES (?, ?, '') "
                    "ON CONFLICT DO NOTHING RETURNING 1",
                    (user_id, action_key),
                ).fetchone()
            else:
                row = db.execute(
                    f"INSERT OR IGNORE INTO {table}(user_id, action_key, task_id) "
                    "VALUES (?, ?, '') RETURNING 1",
                    (user_id, action_key),
                ).fetchone()
        return row is not None

    def _release_export_task(self, table: str, user_id: str, action_key: str):
        """Undo a reservation whose API call failed, so a retry isn't
        permanently skipped as "already sent"."""
        with self.connect() as db:
            db.execute(
                f"DELETE FROM {table} WHERE user_id=? AND action_key=? AND task_id=''",
                (user_id, action_key),
            )

    def _save_export_task(self, table: str, user_id: str, action_key: str, task: dict):
        with self.connect() as db:
            db.execute(
                f"UPDATE {table} SET task_id=?, task_url=? WHERE user_id=? AND action_key=?",
                (str(task["id"]), task.get("url"), user_id, action_key),
            )

    def clickup_task_id(self, user_id: str, action_key: str) -> str | None:
        return self._export_task_id("clickup_tasks", user_id, action_key)

    def forget_clickup_task(self, user_id: str, action_key: str):
        self._forget_export_task("clickup_tasks", user_id, action_key)

    def reserve_clickup_task(self, user_id: str, action_key: str) -> bool:
        return self._reserve_export_task("clickup_tasks", user_id, action_key)

    def release_clickup_task(self, user_id: str, action_key: str):
        self._release_export_task("clickup_tasks", user_id, action_key)

    def save_clickup_task(self, user_id: str, action_key: str, task: dict):
        self._save_export_task("clickup_tasks", user_id, action_key, task)

    def planner_task_id(self, user_id: str, action_key: str) -> str | None:
        return self._export_task_id("planner_tasks", user_id, action_key)

    def forget_planner_task(self, user_id: str, action_key: str):
        self._forget_export_task("planner_tasks", user_id, action_key)

    def reserve_planner_task(self, user_id: str, action_key: str) -> bool:
        return self._reserve_export_task("planner_tasks", user_id, action_key)

    def release_planner_task(self, user_id: str, action_key: str):
        self._release_export_task("planner_tasks", user_id, action_key)

    def save_planner_task(self, user_id: str, action_key: str, task: dict):
        self._save_export_task("planner_tasks", user_id, action_key, task)

    def planner_default(self, user_id: str) -> dict | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT plan_id, plan_name FROM planner_defaults WHERE user_id=?", (user_id,)
            ).fetchone()
        return dict(row) if row and row["plan_id"] else None

    def set_planner_default(self, user_id: str, plan_id: str, plan_name: str):
        with self.connect() as db:
            db.execute(
                """INSERT INTO planner_defaults(user_id, plan_id, plan_name) VALUES (?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    plan_id=excluded.plan_id, plan_name=excluded.plan_name""",
                (user_id, plan_id, plan_name),
            )

    def add_planner_plan(self, user_id: str, plan_id: str, plan_name: str):
        with self.connect() as db:
            db.execute(
                """INSERT INTO planner_plans(user_id, plan_id, plan_name) VALUES (?, ?, ?)
                ON CONFLICT(user_id, plan_id) DO UPDATE SET plan_name=excluded.plan_name""",
                (user_id, plan_id, plan_name),
            )

    def planner_plans(self, user_id: str) -> list[dict]:
        with self.connect() as db:
            return [
                dict(row)
                for row in db.execute(
                    "SELECT plan_id, plan_name FROM planner_plans WHERE user_id=? "
                    "ORDER BY plan_name",
                    (user_id,),
                ).fetchall()
            ]

    def remove_planner_plan(self, user_id: str, plan_id: str):
        with self.connect() as db:
            db.execute(
                "DELETE FROM planner_plans WHERE user_id=? AND plan_id=?", (user_id, plan_id)
            )
            db.execute(
                "UPDATE planner_defaults SET plan_id=NULL, plan_name=NULL "
                "WHERE user_id=? AND plan_id=?",
                (user_id, plan_id),
            )

    def enqueue(self, payloads: list[str]):
        if not payloads:
            return
        types = {}
        for payload in payloads:
            try:
                fields = json.loads(payload)
                kind = (
                    "InsightEvent"
                    if "insight_id" in fields
                    else "TranscriptEvent"
                    if "transcript_id" in fields
                    else "UserSync"
                    if fields.get("type") == "user_sync"
                    else "MeetingSync"
                )
            except (TypeError, ValueError):
                kind = "invalid"
            types[kind] = types.get(kind, 0) + 1
        with self.connect() as db:
            if self.database_url:
                db.executemany(
                    "INSERT INTO jobs(payload, due, priority) VALUES (?, ?, ?) "
                    "ON CONFLICT DO NOTHING",
                    [(payload, time.time(), job_priority(payload)) for payload in payloads],
                )
            else:
                db.executemany(
                    "INSERT INTO jobs(payload, due, priority) SELECT ?, ?, ? WHERE NOT EXISTS "
                    "(SELECT 1 FROM jobs WHERE payload=? AND status='pending')",
                    [
                        (payload, time.time(), job_priority(payload), payload)
                        for payload in payloads
                    ],
                )
        log.info("Jobs enqueued requested=%s types=%s", len(payloads), types)

    def pending_job_count(self) -> int:
        with self.connect() as db:
            return db.execute("SELECT COUNT(*) FROM jobs WHERE status='pending'").fetchone()[0]

    def next_job(self) -> dict | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM jobs WHERE status='pending' AND due<=? "
                "ORDER BY priority, id LIMIT 1",
                (time.time(),),
            ).fetchone()
        return dict(row) if row else None

    def claim_job(self) -> dict | None:
        with self.connect() as db:
            if self.database_url:
                row = db.execute(
                    """WITH candidate AS (
                        SELECT id FROM jobs WHERE status='pending' AND due<=?
                        ORDER BY priority, id LIMIT 1 FOR UPDATE SKIP LOCKED
                    )
                    UPDATE jobs SET due=? FROM candidate
                    WHERE jobs.id=candidate.id RETURNING jobs.*""",
                    (time.time(), time.time() + 300),
                ).fetchone()
            else:
                row = db.execute(
                    """UPDATE jobs SET due=? WHERE id=(
                        SELECT id FROM jobs WHERE status='pending' AND due<=?
                        ORDER BY priority, id LIMIT 1
                    ) RETURNING *""",
                    (time.time() + 300, time.time()),
                ).fetchone()
        return dict(row) if row else None

    def finish_job(self, job_id: int, status: str):
        with self.connect() as db:
            db.execute("UPDATE jobs SET status=? WHERE id=?", (status, job_id))
        log.info("Job persisted id=%s status=%s", job_id, status)

    def retry_job(self, job: dict, *, throttled_until: float | None = None):
        """Put a failed job back with backoff, or give up after five attempts.

        throttled_until means Graph throttled the job rather than the job
        failing: it runs again exactly when Graph said it may, and the attempt
        is not counted, so a long throttle cannot exhaust a job that has
        nothing wrong with it.
        """
        if throttled_until is not None:
            attempts = job["attempts"]
            due = max(throttled_until, time.time())
        else:
            attempts = job["attempts"] + 1
            due = time.time() + min(30 * 2**attempts, 900)
        status = "failed" if attempts >= 5 else "pending"
        with self.connect() as db:
            db.execute(
                "UPDATE jobs SET attempts=?, due=?, status=? WHERE id=?",
                (attempts, due, status, job["id"]),
            )
        log.warning(
            "Job retry persisted id=%s attempts=%s status=%s delay_s=%.1f throttled=%s",
            job["id"],
            attempts,
            status,
            due - time.time(),
            throttled_until is not None,
        )

    def next_notification(self) -> dict | None:
        with self.connect() as db:
            if self.database_url:
                row = db.execute(
                    """WITH candidate AS (
                        SELECT id FROM activity_outbox WHERE status='pending' AND due<=?
                        ORDER BY id LIMIT 1 FOR UPDATE SKIP LOCKED
                    )
                    UPDATE activity_outbox SET due=? FROM candidate
                    WHERE activity_outbox.id=candidate.id RETURNING activity_outbox.*""",
                    (time.time(), time.time() + 300),
                ).fetchone()
            else:
                row = db.execute(
                    """UPDATE activity_outbox SET due=? WHERE id=(
                        SELECT id FROM activity_outbox WHERE status='pending' AND due<=?
                        ORDER BY id LIMIT 1
                    ) RETURNING *""",
                    (time.time() + 300, time.time()),
                ).fetchone()
        return dict(row) if row else None

    def _lock_meeting_writes(self, db, user_id: str):
        # Serialize JSON read/merge/write across workers, including the first
        # insert. A meeting-row lock alone cannot protect a not-yet-created row.
        if self.database_url:
            db.execute("SELECT id FROM users WHERE id=? FOR UPDATE", (user_id,)).fetchone()
        else:
            db.execute("BEGIN IMMEDIATE")

    def save_meeting(self, user_id: str, subject: str, content: dict):
        with self.connect() as db:
            self._lock_meeting_writes(db, user_id)
            occurred_at = _meeting_occurred_at(content)
            if content.get("meeting_id"):
                row = db.execute(
                    """SELECT id, content FROM meetings WHERE user_id=? AND meeting_id=?
                    ORDER BY id DESC LIMIT 1""",
                    (user_id, content["meeting_id"]),
                ).fetchone()
                merged = json.loads(row["content"]) if row else {}
                insight = content.get("insight") or {}
                if insight.get("transcript_version"):
                    try:
                        current = select_session(merged, insight.get("occurrence_id"))
                    except (KeyError, ValueError):
                        return False
                    if transcript_version(current) != insight["transcript_version"]:
                        # A new segment arrived while the AI was running. Its
                        # newer generation owns this call's result.
                        return False
                if content.get("meeting_metadata"):
                    content = {
                        **content,
                        "meeting_metadata": {
                            **merged.get("meeting_metadata", {}),
                            **{
                                k: v
                                for k, v in content["meeting_metadata"].items()
                                if v is not None
                            },
                        },
                    }
                # Keep every transcript and insight segment under its meeting, in either arrival order.
                for kind, field in (("insight", "insights"), ("transcript", "transcripts")):
                    if kind in content:
                        items = merged.get(field, [])
                        if not items and kind in merged:
                            items = [{kind: merged[kind], "card": merged.get("card")}]
                        item = {kind: content[kind]}
                        if kind == "insight":
                            item["card"] = content.get("card")
                        # Drop any entry naming the same artifact under one of
                        # its other ids, not just the id this write happens to use.
                        aliases = artifact_aliases(content[kind])
                        items = [x for x in items if not artifact_aliases(x[kind]) & aliases]
                        merged[field] = [*items, item]
                merged.update(content)
                merged, _ = dedupe_content(merged)
                occurred_at = _meeting_occurred_at(merged) or occurred_at
                if row:
                    facts = meeting_facts(merged)
                    db.execute(
                        """UPDATE meetings SET subject=?, content=?, occurred_at=?,
                        source=?, settled=?,
                        newest_transcript_at=? WHERE id=?
                        AND EXISTS (SELECT 1 FROM users WHERE id=? AND enabled=1)""",
                        (
                            subject,
                            json.dumps(merged),
                            occurred_at,
                            facts["source"],
                            facts["settled"],
                            facts["newest_transcript_at"],
                            row["id"],
                            user_id,
                        ),
                    )
                    log.info(
                        "Meeting updated user=%s meeting=%s row_id=%s settled=%s "
                        "transcripts=%s insights=%s",
                        user_id,
                        digest(str(facts["meeting_id"]))[:8],
                        row["id"],
                        facts["settled"],
                        len(bodies(merged, "transcript")),
                        len(bodies(merged, "insight")),
                    )
                    return
                content = merged
            facts = meeting_facts(content)
            # Recheck enrollment in case the user disconnected while Graph was responding.
            db.execute(
                """INSERT INTO meetings(user_id, subject, content, created, occurred_at,
                meeting_id, source, settled, newest_transcript_at)
                SELECT id, ?, ?, ?, ?, ?, ?, ?, ? FROM users WHERE id=? AND enabled=1""",
                (
                    subject,
                    json.dumps(content),
                    time.time(),
                    occurred_at,
                    facts["meeting_id"] or "",
                    facts["source"],
                    facts["settled"],
                    facts["newest_transcript_at"],
                    user_id,
                ),
            )
        log.info(
            "Meeting inserted user=%s meeting=%s source=%s settled=%s transcripts=%s insights=%s",
            user_id,
            digest(str(facts["meeting_id"] or "unknown"))[:8],
            facts["source"] or "graph",
            facts["settled"],
            len(bodies(content, "transcript")),
            len(bodies(content, "insight")),
        )

    def set_meeting_sync_state(
        self,
        user_id: str,
        meeting_id: str,
        status: str | None,
        message: str | None = None,
        metadata: dict | None = None,
    ) -> bool:
        """Persist a meeting-level polling result without changing its title or age.

        Keeping this state in the existing content document makes it available
        to both SQLite/PostgreSQL and to the current meetings API without a
        schema migration. A successful recheck clears the prior terminal state.
        """
        with self.connect() as db:
            self._lock_meeting_writes(db, user_id)
            row = db.execute(
                """SELECT id, content FROM meetings WHERE user_id=? AND meeting_id=?
                ORDER BY id DESC LIMIT 1""",
                (user_id, meeting_id),
            ).fetchone()
            if not row:
                return False
            content = json.loads(row["content"])
            if status:
                content["sync_status"] = status
            else:
                content.pop("sync_status", None)
            if message:
                content["sync_message"] = message
            else:
                content.pop("sync_message", None)
            if metadata is not None:
                content["meeting_metadata"] = {
                    **content.get("meeting_metadata", {}),
                    **{key: value for key, value in metadata.items() if value is not None},
                }
            facts = meeting_facts(content)
            db.execute(
                """UPDATE meetings SET content=?, occurred_at=?,
                source=?, settled=?, newest_transcript_at=? WHERE id=?""",
                (
                    json.dumps(content),
                    facts["occurred_at"],
                    facts["source"],
                    facts["settled"],
                    facts["newest_transcript_at"],
                    row["id"],
                ),
            )
        log.info(
            "Meeting sync state persisted user=%s meeting=%s status=%s terminal=%s",
            user_id,
            digest(meeting_id)[:8],
            status or "CLEARED",
            facts["settled"],
        )
        return True

    def save_transcript(self, user_id: str, meeting_id: str, transcript_id: str, text: str):
        with self.connect() as db:
            row = db.execute(
                """INSERT INTO transcripts(user_id, meeting_id, transcript_id, content)
                SELECT id, ?, ?, ? FROM users WHERE id=? AND enabled=1
                ON CONFLICT(user_id, meeting_id, transcript_id) DO UPDATE SET content=excluded.content
                RETURNING id""",
                (meeting_id, transcript_id, text, user_id),
            ).fetchone()
        local_id = row[0] if row else None
        log.info(
            "Transcript persisted user=%s meeting=%s transcript=%s local_id=%s chars=%s enrolled=%s",
            user_id,
            digest(meeting_id)[:8],
            digest(transcript_id)[:8],
            local_id if local_id is not None else "-",
            len(text),
            local_id is not None,
        )
        return local_id

    def transcript(self, user_id: str, transcript_id: int) -> str | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT content FROM transcripts WHERE id=? AND user_id=?", (transcript_id, user_id)
            ).fetchone()
        return row[0] if row else None

    def retain_latest_upload(self, user_id: str):
        """Keep only the most recently saved custom run for this user."""
        with self.connect() as db:
            rows = db.execute(
                "SELECT id, content FROM meetings WHERE user_id=? ORDER BY created DESC, id DESC",
                (user_id,),
            ).fetchall()
            uploads = [
                (row["id"], json.loads(row["content"]))
                for row in rows
                if json.loads(row["content"]).get("source") == "upload"
            ]
            for row_id, content in uploads[1:]:
                db.execute(
                    "DELETE FROM transcripts WHERE user_id=? AND meeting_id=?",
                    (user_id, content["meeting_id"]),
                )
                db.execute("DELETE FROM meetings WHERE user_id=? AND id=?", (user_id, row_id))

    def meetings(self, user_id: str) -> list[dict]:
        with self.connect() as db:
            rows = db.execute(
                """SELECT id, subject, content, created FROM meetings
                WHERE user_id=? ORDER BY COALESCE(occurred_at, created) DESC, id DESC LIMIT 100""",
                (user_id,),
            ).fetchall()
        return [dict(row, content=json.loads(row["content"])) for row in rows]

    def meeting(self, user_id: str, meeting_id: int) -> dict | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT id, subject, content FROM meetings WHERE id=? AND user_id=?",
                (meeting_id, user_id),
            ).fetchone()
        return dict(row, content=json.loads(row["content"])) if row else None

    def find_meeting(self, user_id: str, meeting_id: str) -> dict | None:
        """One meeting's content by its Graph id, without reading the others.

        Matches save_meeting's choice of row, so a caller that reads here and
        writes there cannot end up looking at a different duplicate.
        """
        with self.connect() as db:
            row = db.execute(
                """SELECT content FROM meetings WHERE user_id=? AND meeting_id=?
                ORDER BY id DESC LIMIT 1""",
                (user_id, meeting_id),
            ).fetchone()
        return json.loads(row["content"]) if row else None

    def sync_candidates(
        self,
        user_id: str,
        *,
        since: float,
        within_window: bool = True,
        only_unsettled: bool = True,
    ) -> list[str]:
        """The meetings worth a Graph poll, newest first.

        Answered entirely from the derived columns: this runs once a minute per
        user, and reading every saved card and summary to decide it was the
        sweep's dominant cost.

        Both bounds are dropped for the manual paths. within_window=False reaches
        meetings old enough that the background sweep has given up on them, for
        recovery; only_unsettled=False keeps Refresh exhaustive, which is the
        difference between backing off in the background and re-checking
        everything because a person asked.
        """
        conditions = [
            "user_id=?",
            "created>=?",
            "(source IS NULL OR source<>'upload')",
            "meeting_id<>''",
        ]
        parameters = [user_id, since]
        if only_unsettled:
            conditions.append("settled=0")
        if within_window:
            # NULL means "we could not tell", which has to keep polling: see
            # newest_transcript_at.
            conditions.append("(newest_transcript_at IS NULL OR newest_transcript_at>=?)")
            parameters.append(time.time() - PUBLICATION_WINDOW_HOURS * 3600)
        with self.connect() as db:
            rows = db.execute(
                f"SELECT meeting_id FROM meetings WHERE {' AND '.join(conditions)} "
                "ORDER BY COALESCE(occurred_at, created) DESC, id DESC",
                tuple(parameters),
            ).fetchall()
        # Duplicate rows for one meeting are retained deliberately; poll once.
        return list(dict.fromkeys(row["meeting_id"] for row in rows))

    def transcript_aliases(self, user_id: str) -> set[tuple[str, str]]:
        """Every (meeting, transcript alias) pair this user already holds.

        Projects the transcripts array in SQL rather than loading whole rows:
        the saved cards and summaries beside it are the bulk of the content and
        discovery never looks at them.
        """
        projection = (
            "content::jsonb -> 'transcripts'"
            if self.database_url
            else "json_extract(content, '$.transcripts')"
        )
        with self.connect() as db:
            rows = db.execute(
                f"SELECT meeting_id, {projection} AS transcripts FROM meetings WHERE user_id=?",
                (user_id,),
            ).fetchall()
        pairs = set()
        for row in rows:
            entries = row["transcripts"]
            # SQLite returns the projection as text; psycopg decodes jsonb already.
            if isinstance(entries, str):
                entries = json.loads(entries)
            for entry in entries or []:
                for alias in artifact_aliases(entry.get("transcript") or {}):
                    pairs.add((row["meeting_id"], alias))
        return pairs

    def request_repair(self):
        """Ask whichever process runs the worker to re-check subscriptions.

        Goes through the database so it still arrives when the web and worker
        roles are separate containers, or when more than one web replica is
        serving. An in-process worker also has the asyncio.Event, which is
        faster; both are consumed together so a repair never runs twice.
        """
        self.put("repair", "subscriptions", {"at": time.time()}, ttl=3600)

    def take_repair(self) -> bool:
        # Read before popping: the worker asks once a second, and a DELETE every
        # second is WAL churn for an answer that is almost always "no".
        if not self.get("repair", "subscriptions"):
            return False
        return self.pop("repair", "subscriptions") is not None

    def prune(
        self, *, job_days: float = 1, failed_job_days: float = 7, meeting_days: float | None = None
    ) -> dict:
        """Delete finished work the application will never read again.

        Jobs accumulate at roughly one row per user per minute from polling
        alone, and nothing removed them. Failed jobs are kept longer because
        they are the diagnostic record of what went wrong.

        Meeting retention is opt-in and off by default: deleting a user's saved
        meetings is a product decision, not a storage one, so it happens only
        when an operator sets a window.
        """
        now = time.time()
        counts = {}
        with self.connect() as db:
            counts["jobs"] = db.execute(
                "DELETE FROM jobs WHERE status NOT IN ('pending', 'failed') AND due<?",
                (now - job_days * 86400,),
            ).rowcount
            counts["failed_jobs"] = db.execute(
                "DELETE FROM jobs WHERE status='failed' AND due<?",
                (now - failed_job_days * 86400,),
            ).rowcount
            counts["meetings"] = 0
            counts["transcripts"] = 0
            if meeting_days is not None:
                cutoff = now - meeting_days * 86400
                # Transcript rows are reached through their meeting; once that is
                # gone nothing can load them, so they would leak silently. Deleted
                # by naming the expiring meetings rather than by sweeping for
                # orphans: process_transcript saves a transcript just before its
                # meeting row exists, and an orphan sweep would race that window.
                expiring = db.execute(
                    "SELECT user_id, meeting_id FROM meetings WHERE COALESCE(occurred_at, created)<?",
                    (cutoff,),
                ).fetchall()
                if expiring:
                    counts["transcripts"] = db.executemany(
                        "DELETE FROM transcripts WHERE user_id=? AND meeting_id=?",
                        [(row["user_id"], row["meeting_id"]) for row in expiring],
                    ).rowcount
                counts["meetings"] = db.execute(
                    "DELETE FROM meetings WHERE COALESCE(occurred_at, created)<?", (cutoff,)
                ).rowcount
        log.info(
            "Storage prune completed job_days=%s failed_job_days=%s meeting_days=%s counts=%s",
            job_days,
            failed_job_days,
            meeting_days,
            counts,
        )
        return counts

    def planner_cache(self, user_id: str) -> str | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT cache FROM planner_connections WHERE user_id=?", (user_id,)
            ).fetchone()
        return row["cache"] if row else None

    def save_planner_cache(self, user_id: str, cache: str):
        with self.connect() as db:
            db.execute(
                "INSERT INTO planner_connections(user_id, cache) VALUES (?, ?) "
                "ON CONFLICT(user_id) DO UPDATE SET cache=excluded.cache",
                (user_id, cache),
            )

    def update_planner_cache(self, user_id: str, previous: str, cache: str):
        # Never recreate credentials after disconnect, or overwrite a concurrent reconnect.
        with self.connect() as db:
            db.execute(
                "UPDATE planner_connections SET cache=? WHERE user_id=? AND cache=?",
                (cache, user_id, previous),
            )
