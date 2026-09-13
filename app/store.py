"""Durable local storage for a single server process and worker."""

import hashlib
import json
import secrets
import sqlite3
import time
from contextlib import contextmanager, suppress
from pathlib import Path


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class Store:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
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
                    due REAL NOT NULL, status TEXT NOT NULL DEFAULT 'pending'
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
                -- Retire delegated chat credentials and pending sends from the prior prototype.
                DROP TABLE IF EXISTS chat_connections;
                DROP TABLE IF EXISTS outbox;
            """)
        # Some managed volume drivers set permissions at mount time and do not implement chmod.
        with suppress(OSError):
            path.chmod(0o600)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def healthy(self) -> bool:
        with self.connect() as db:
            return db.execute("SELECT 1").fetchone()[0] == 1

    def put(self, kind: str, key: str, value: dict, ttl: int = 600):
        with self.connect() as db:
            db.execute("DELETE FROM temporary WHERE expires < ?", (time.time(),))
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
                status=CASE WHEN enabled=0 THEN 'CONNECTING' ELSE status END""",
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

    def enqueue(self, payloads: list[str]):
        with self.connect() as db:
            db.executemany(
                "INSERT INTO jobs(payload, due) SELECT ?, ? WHERE NOT EXISTS "
                "(SELECT 1 FROM jobs WHERE payload=? AND status='pending')",
                [(payload, time.time(), payload) for payload in payloads],
            )

    def next_job(self) -> dict | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM jobs WHERE status='pending' AND due<=? ORDER BY id LIMIT 1",
                (time.time(),),
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

    def save_meeting(self, user_id: str, subject: str, content: dict):
        with self.connect() as db:
            if content.get("meeting_id"):
                row = db.execute(
                    """SELECT id, content FROM meetings
                    WHERE user_id=? AND json_extract(content, '$.meeting_id')=?
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
                        items = [x for x in items if x[kind]["id"] != content[kind]["id"]]
                        merged[field] = [*items, item]
                merged.update(content)
                if row:
                    db.execute(
                        """UPDATE meetings SET subject=?, content=?, created=? WHERE id=?
                        AND EXISTS (SELECT 1 FROM users WHERE id=? AND enabled=1)""",
                        (subject, json.dumps(merged), time.time(), row["id"], user_id),
                    )
                    return
                content = merged
            # Recheck enrollment in case the user disconnected while Graph was responding.
            db.execute(
                """INSERT INTO meetings(user_id, subject, content, created)
                SELECT id, ?, ?, ? FROM users WHERE id=? AND enabled=1""",
                (subject, json.dumps(content), time.time(), user_id),
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

    def meetings(self, user_id: str) -> list[dict]:
        with self.connect() as db:
            rows = db.execute(
                """SELECT id, subject, content, created FROM meetings
                WHERE user_id=? ORDER BY created DESC, id DESC LIMIT 100""",
                (user_id,),
            ).fetchall()
        return [dict(row, content=json.loads(row["content"])) for row in rows]
