"""Recover existing insights for one meeting, including a pre-demo meeting."""

import argparse
import asyncio
from urllib.parse import quote
from uuid import UUID

from app.config import settings
from app.graph_client import GraphClient
from app.meetings import meeting_filter
from app.models import InsightEvent, TranscriptEvent
from app.store import Store


async def recover(
    user_id: str,
    meeting_url: str | None = None,
    include_transcripts: bool = False,
    meeting_id: str | None = None,
):
    config = settings()
    store = Store(config.database, config.backup_database, config.database_url)
    if user_id not in store.users():
        raise ValueError("Connect this organizer to NoteIQ first.")
    graph = GraphClient()
    if meeting_id:
        meetings = [{"id": meeting_id}]
    else:
        meetings = await graph.list(
            f"/users/{user_id}/onlineMeetings?$filter="
            + quote(meeting_filter(meeting_url or ""), safe="")
        )
    if not meetings:
        raise ValueError("Meeting not found for this organizer.")
    count = 0
    for meeting in meetings:
        path = f"/copilot/users/{user_id}/onlineMeetings/{quote(meeting['id'], safe='')}/aiInsights"
        insights = await graph.list(path)
        store.enqueue(
            [
                InsightEvent(
                    user_id=user_id, meeting_id=meeting["id"], insight_id=item["id"]
                ).model_dump_json()
                for item in insights
            ]
        )
        count += len(insights)
        if include_transcripts:
            transcripts = await graph.list(
                f"/users/{user_id}/onlineMeetings/{quote(meeting['id'], safe='')}/transcripts"
            )
            store.enqueue(
                [
                    TranscriptEvent(
                        user_id=user_id, meeting_id=meeting["id"], transcript_id=item["id"]
                    ).model_dump_json()
                    for item in transcripts
                ]
            )
            print(f"Queued {len(transcripts)} transcript(s).")
    print(f"Queued {count} insight(s). Keep the server running and refresh NoteIQ.")
    if not count:
        print("Copilot has not provided insights for this meeting yet.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--user-id", type=UUID, required=True)
    meeting = parser.add_mutually_exclusive_group(required=True)
    meeting.add_argument("--meeting-url")
    meeting.add_argument("--meeting-id")
    parser.add_argument("--include-transcripts", action="store_true")
    args = parser.parse_args()
    asyncio.run(
        recover(
            str(args.user_id),
            args.meeting_url,
            args.include_transcripts,
            args.meeting_id,
        )
    )


if __name__ == "__main__":
    main()
