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

log = logging.getLogger(__name__)

# Microsoft documents that insights "might take up to four hours to be available
# after the call ends". Past that, with a margin, a poll is no longer waiting for
# anything on a published schedule, and the webhook remains the way a straggler
# arrives. Without a bound, one transcript Copilot never summarises keeps its
# meeting in the sweep for the full seven-day retention.
PUBLICATION_WINDOW_HOURS = 6


def _meeting_occurred_at(content: dict) -> float | None:
    """Best-effort real-world timestamp for a meeting.

    Used to sort the meetings list by when the meeting actually happened,
    not by when we last wrote to its row — otherwise regenerating an old
    meeting's insight (or Copilot delivering one late) would bump it to the
    top of the list ahead of meetings that happened more recently.
    """
    dates = []
    insights = content.get("insights") or (
        [{"insight": content["insight"]}] if content.get("insight") else []
    )
    for item in insights:
        end = (item.get("insight") or {}).get("endDateTime")
        if end:
            dates.append(end)
    transcripts = content.get("transcripts") or (
        [{"transcript": content["transcript"]}] if content.get("transcript") else []
    )
    for item in transcripts:
        created_at = (item.get("transcript") or {}).get("createdDateTime")
        if created_at:
            dates.append(created_at)
    if not dates:
        return None
    try:
        return max(datetime.fromisoformat(d.replace("Z", "+00:00")).timestamp() for d in dates)
    except (ValueError, AttributeError):
        return None


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
        fingerprint = json.dumps([provider, notes, actions], sort_keys=True, default=str)
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
            """CREATE TABLE IF NOT EXISTS clickup_connections (
                user_id TEXT PRIMARY KEY, token TEXT NOT NULL, list_id TEXT, workspaces TEXT NOT NULL)""",
            "ALTER TABLE clickup_connections ADD COLUMN IF NOT EXISTS list_name TEXT",
            """CREATE TABLE IF NOT EXISTS clickup_lists (
                user_id TEXT NOT NULL, list_id TEXT NOT NULL, list_name TEXT NOT NULL,
                PRIMARY KEY (user_id, list_id))""",
            """CREATE TABLE IF NOT EXISTS clickup_tasks (
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

    def _restore_if_needed(self):
        """Start a new container from the last consistent mounted snapshot."""
        if not self.backup_path or self.path.exists() or not self.backup_path.exists():
            return
        temporary = self.path.with_name(self.path.name + ".restore")
        try:
            shutil.copyfile(self.backup_path, temporary)
            with sqlite3.connect(temporary) as db:
                if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise RuntimeError("database integrity check failed")
                if not db.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='users'"
                ).fetchone():
                    raise RuntimeError("database snapshot has no NoteIQ schema")
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
                with sqlite3.connect(temporary) as copy:
                    db.backup(copy)
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
                (digest(token), user_id, time.time() + 8 * 3600),
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

    def disconnect(self, user_id: str):
        with self.connect() as db:
            db.execute("UPDATE users SET enabled=0, status='DISCONNECTED' WHERE id=?", (user_id,))
            db.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
            db.execute("DELETE FROM meetings WHERE user_id=?", (user_id,))
            db.execute("DELETE FROM transcripts WHERE user_id=?", (user_id,))
            db.execute("DELETE FROM activity_outbox WHERE user_id=?", (user_id,))
            db.execute("DELETE FROM clickup_connections WHERE user_id=?", (user_id,))
            db.execute("DELETE FROM clickup_lists WHERE user_id=?", (user_id,))
            db.execute("DELETE FROM clickup_tasks WHERE user_id=?", (user_id,))

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

    def clickup_task_id(self, user_id: str, action_key: str) -> str | None:
        """Return the ClickUp task id previously recorded for this action item, if any."""
        with self.connect() as db:
            row = db.execute(
                "SELECT task_id FROM clickup_tasks WHERE user_id=? AND action_key=?",
                (user_id, action_key),
            ).fetchone()
        return row["task_id"] if row and row["task_id"] else None

    def forget_clickup_task(self, user_id: str, action_key: str):
        """Drop a stale record so a deleted-in-ClickUp task can be re-exported."""
        with self.connect() as db:
            db.execute(
                "DELETE FROM clickup_tasks WHERE user_id=? AND action_key=?",
                (user_id, action_key),
            )

    def reserve_clickup_task(self, user_id: str, action_key: str) -> bool:
        """Atomically claim an action item before calling the ClickUp API.

        The check-then-act window between "was this already sent?" and the
        network call to create the task is otherwise wide enough for
        concurrent export requests to both pass the check and both create a
        duplicate task in the user's ClickUp workspace. Reserving the row
        first, inside a single statement, closes that window: only one
        concurrent caller can win the insert. Returns True if this call
        claimed it and should proceed to create the task; False if another
        call already claimed (or completed) it.
        """
        with self.connect() as db:
            if self.database_url:
                # ON CONFLICT must precede RETURNING; the generic "INSERT OR IGNORE"
                # translation appends ON CONFLICT at the end, which is invalid here.
                row = db.execute(
                    "INSERT INTO clickup_tasks(user_id, action_key, task_id) VALUES (?, ?, '') "
                    "ON CONFLICT DO NOTHING RETURNING 1",
                    (user_id, action_key),
                ).fetchone()
            else:
                row = db.execute(
                    "INSERT OR IGNORE INTO clickup_tasks(user_id, action_key, task_id) "
                    "VALUES (?, ?, '') RETURNING 1",
                    (user_id, action_key),
                ).fetchone()
        return row is not None

    def release_clickup_task(self, user_id: str, action_key: str):
        """Undo a reservation whose ClickUp API call failed, so a retry isn't
        permanently skipped as "already sent"."""
        with self.connect() as db:
            db.execute(
                "DELETE FROM clickup_tasks WHERE user_id=? AND action_key=? AND task_id=''",
                (user_id, action_key),
            )

    def save_clickup_task(self, user_id: str, action_key: str, task: dict):
        with self.connect() as db:
            db.execute(
                "UPDATE clickup_tasks SET task_id=?, task_url=? WHERE user_id=? AND action_key=?",
                (str(task["id"]), task.get("url"), user_id, action_key),
            )

    def enqueue(self, payloads: list[str]):
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

    def retry_job(self, job: dict):
        attempts = job["attempts"] + 1
        with self.connect() as db:
            db.execute(
                "UPDATE jobs SET attempts=?, due=?, status=? WHERE id=?",
                (
                    attempts,
                    time.time() + min(30 * 2**attempts, 900),
                    "failed" if attempts >= 5 else "pending",
                    job["id"],
                ),
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

    def save_meeting(self, user_id: str, subject: str, content: dict):
        with self.connect() as db:
            occurred_at = _meeting_occurred_at(content)
            if content.get("meeting_id"):
                row = db.execute(
                    """SELECT id, content FROM meetings WHERE user_id=? AND meeting_id=?
                    ORDER BY id DESC LIMIT 1""",
                    (user_id, content["meeting_id"]),
                ).fetchone()
                merged = json.loads(row["content"]) if row else {}
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
                        """UPDATE meetings SET subject=?, content=?, created=?,
                        occurred_at=COALESCE(?, occurred_at), source=?, settled=?,
                        newest_transcript_at=? WHERE id=?
                        AND EXISTS (SELECT 1 FROM users WHERE id=? AND enabled=1)""",
                        (
                            subject,
                            json.dumps(merged),
                            time.time(),
                            occurred_at,
                            facts["source"],
                            facts["settled"],
                            facts["newest_transcript_at"],
                            row["id"],
                            user_id,
                        ),
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

    def save_transcript(self, user_id: str, meeting_id: str, transcript_id: str, text: str):
        with self.connect() as db:
            row = db.execute(
                """INSERT INTO transcripts(user_id, meeting_id, transcript_id, content)
                SELECT id, ?, ?, ? FROM users WHERE id=? AND enabled=1
                ON CONFLICT(user_id, meeting_id, transcript_id) DO UPDATE SET content=excluded.content
                RETURNING id""",
                (meeting_id, transcript_id, text, user_id),
            ).fetchone()
        return row[0] if row else None

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
        return counts
