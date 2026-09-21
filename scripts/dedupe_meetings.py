"""Collapse duplicate transcripts and insights in saved meetings.

Rows written before save_meeting matched artifacts on all of their ids can hold
the same transcript twice -- once under each name Graph gave it -- and with it a
second OpenRouter summary of identical text. New writes are already guarded, so
this is a one-time backfill for rows nothing has touched since.

Reports by default and writes only with --apply. Each write is conditional on
the exact content it read, so a row the worker changes underneath is skipped and
reported rather than overwritten.
"""

import argparse
import json
from datetime import datetime, timezone

from app.config import settings
from app.store import Store, dedupe_content


def plan(store: Store) -> list[dict]:
    """Every row needing work, with the cleaned content ready to write."""
    with store.connect() as db:
        rows = db.execute("SELECT id, user_id, subject, content FROM meetings").fetchall()
    work = []
    for row in rows:
        # Compare against the exact text read: the UPDATE guard below relies on
        # it, so it must not be a re-serialised copy.
        before = row["content"]
        cleaned, report = dedupe_content(json.loads(before))
        if report:
            work.append(
                {
                    "id": row["id"],
                    "subject": row["subject"],
                    "before": before,
                    "after": json.dumps(cleaned),
                    "report": report,
                }
            )
    return work


def snapshot(store: Store) -> str:
    """Copy the meetings table before writing, and return the copy's name.

    Point-in-time restore rebuilds the whole server, which is a heavy way to undo
    one bad UPDATE. A table beside the original makes the rollback a single
    statement, and needs no access this script does not already have.
    """
    # Sub-second resolution: two runs in the same second must not collide on a
    # name, or the second one fails instead of taking its own backup.
    name = "meetings_backup_" + datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")
    with store.connect() as db:
        db.execute(f"CREATE TABLE {name} AS SELECT * FROM meetings")
    return name


def apply(store: Store, work: list[dict]) -> tuple[int, list[int]]:
    written, skipped = 0, []
    for item in work:
        with store.connect() as db:
            changed = db.execute(
                "UPDATE meetings SET content=? WHERE id=? AND content=?",
                (item["after"], item["id"], item["before"]),
            ).rowcount
        if changed:
            written += 1
        else:
            skipped.append(item["id"])
    return written, skipped


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="write changes (default: report only)")
    args = parser.parse_args()

    config = settings()
    store = Store(config.database, config.backup_database, config.database_url)
    work = plan(store)

    for item in work:
        summary = ", ".join(
            f"{field} {counts['before']}->{counts['after']}"
            for field, counts in sorted(item["report"].items())
        )
        print(f"meeting row={item['id']} subject={item['subject']!r} {summary}")

    if not work:
        print("No duplicates found.")
        return 0
    if not args.apply:
        print(f"\n{len(work)} row(s) would change. Re-run with --apply to write.")
        return 0

    table = snapshot(store)
    print(f"\nBacked up meetings to {table}")
    written, skipped = apply(store, work)
    print(f"Updated {written} row(s).")
    print(f"To undo: UPDATE meetings m SET content=b.content FROM {table} b WHERE m.id=b.id;")
    if skipped:
        print(f"Skipped {len(skipped)} row(s) changed by the worker mid-run: {skipped}")
        print("Re-run to pick them up.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
