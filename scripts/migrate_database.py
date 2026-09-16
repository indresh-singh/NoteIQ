"""Copy an existing NoteIQ SQLite database into PostgreSQL once."""

import argparse
import os
import sqlite3
from pathlib import Path

from app.store import Store

TABLES = {
    "users": ("id", "name", "status", "enabled"),
    "temporary": ("kind", "key", "value", "expires"),
    "sessions": ("token_hash", "user_id", "expires"),
    "jobs": ("id", "payload", "attempts", "due", "status"),
    "meetings": ("id", "user_id", "subject", "content", "created"),
    "transcripts": ("id", "user_id", "meeting_id", "transcript_id", "content"),
    "activity_outbox": (
        "id",
        "user_id",
        "event_key",
        "subject",
        "message",
        "status",
        "attempts",
        "due",
    ),
    "clickup_connections": ("user_id", "token", "list_id", "workspaces"),
    "clickup_tasks": ("user_id", "action_key", "task_id", "task_url"),
}


def migrate(source: Path, database_url: str):
    if not source.is_file():
        raise ValueError(f"SQLite database not found: {source}")
    target = Store(Path("/tmp/noteiq-migration.sqlite3"), database_url=database_url)
    with target.connect() as db:
        if db.execute("SELECT 1 FROM users LIMIT 1").fetchone():
            raise ValueError("PostgreSQL already contains users; migration was not run.")

    copied = 0
    with sqlite3.connect(source) as old, target.connect() as new:
        old.row_factory = sqlite3.Row
        existing = {
            row[0] for row in old.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        for table, columns in TABLES.items():
            if table not in existing:
                continue
            rows = old.execute(f"SELECT {', '.join(columns)} FROM {table}").fetchall()
            if rows:
                placeholders = ", ".join("?" for _ in columns)
                new.executemany(
                    f"INSERT INTO {table} ({', '.join(columns)}) "
                    f"VALUES ({placeholders}) ON CONFLICT DO NOTHING",
                    [tuple(row) for row in rows],
                )
                copied += len(rows)
        for table in ("jobs", "meetings", "transcripts", "activity_outbox"):
            new.execute(
                f"SELECT setval(pg_get_serial_sequence('{table}', 'id'), "
                f"COALESCE(MAX(id), 1), MAX(id) IS NOT NULL) FROM {table}"
            )
    target.close()
    print(f"Copied {copied} rows from {source} to PostgreSQL.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    args = parser.parse_args()
    database_url = os.getenv("NOTEIQ_DATABASE_URL")
    if not database_url:
        raise ValueError("Set NOTEIQ_DATABASE_URL to the target PostgreSQL connection string.")
    migrate(args.source, database_url)


if __name__ == "__main__":
    main()
