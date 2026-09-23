"""Trace a meeting through real Graph APIs without printing meeting content or tokens."""

import argparse
import asyncio
from urllib.parse import quote
from uuid import UUID

import httpx

from app.adaptive_cards import build_card
from app.config import settings
from app.graph_client import GraphClient
from app.meetings import meeting_filter
from app.models import Insight


async def resolve_user_id(graph: GraphClient, user_ref: str) -> str:
    try:
        UUID(user_ref)
        return user_ref
    except ValueError:
        pass
    escaped = user_ref.replace("'", "''")
    users = await graph.list(
        "/users?$filter=userPrincipalName eq '" + escaped + "' or mail eq '" + escaped + "'"
    )
    if not users:
        raise ValueError(f"No user found for {user_ref!r}; use a work email or Entra object ID.")
    return users[0]["id"]


async def diagnose(
    user_id: str | None, link: str, replay: bool = False, user_email: str | None = None
):
    graph = GraphClient()
    if user_id is None:
        if user_email is None:
            raise ValueError("Provide either --user-id or --user-email.")
        user_id = await resolve_user_id(graph, user_email)
    print(f"Organizer resolution: {user_id}")
    path = f"/users/{user_id}/onlineMeetings"
    meetings = await graph.list(path + "?$filter=" + quote(meeting_filter(link), safe=""))
    print(f"Meeting lookup: {len(meetings)} result(s)")
    if not meetings:
        return
    for meeting in meetings:
        owner = (
            meeting.get("participants", {})
            .get("organizer", {})
            .get("identity", {})
            .get("user", {})
            .get("id")
        )
        print(f"Organizer matches: {owner == user_id}")
        if owner != user_id:
            print("NoteIQ processes only the organizer's meetings.")
            continue
        meeting_path = path + "/" + quote(meeting["id"], safe="")
        for kind, resource in (
            ("transcripts", meeting_path),
            ("aiInsights", "/copilot" + meeting_path),
        ):
            try:
                items = await graph.list(resource + "/" + kind)
                print(f"{kind}: {len(items)} artifact(s)")
                for item in items:
                    artifact = resource + "/" + kind + "/" + quote(item["id"], safe="")
                    if kind == "transcripts":
                        content = await graph.request(
                            "GET",
                            artifact + "/content",
                            text=True,
                            headers={"Accept": "application/vnd.microsoft.graph.transcript+text"},
                        )
                        print(f"Transcript fetched: {len(content)} characters")
                    else:
                        insight = Insight.model_validate(await graph.request("GET", artifact))
                        print(
                            f"Insight fetched: {len(insight.meetingNotes)} notes, {len(insight.actionItems)} actions; card renderable: {build_card(insight, 'Meeting') is not None}"
                        )
                    if replay:
                        config = settings()
                        async with httpx.AsyncClient(timeout=30) as client:
                            response = await client.post(
                                config.public_url + "/api/graph/notifications",
                                json={
                                    "value": [
                                        {
                                            "changeType": "created",
                                            "tenantId": str(config.tenant_id),
                                            "clientState": config.client_state.get_secret_value(),
                                            "resource": artifact,
                                        }
                                    ]
                                },
                            )
                        print(
                            f"Replay {kind}: HTTP {response.status_code} (202 means accepted, verify worker separately)"
                        )
                        response.raise_for_status()
            except httpx.HTTPStatusError as error:
                report(error)
    subscriptions = await graph.list("/subscriptions")
    for sub in subscriptions:
        if user_id in sub.get("resource", ""):
            print(
                f"Subscription: {sub['resource']} expires={sub.get('expirationDateTime')} callback={sub.get('notificationUrl')}"
            )


def report(error):
    try:
        detail = error.response.json().get("error", {})
    except ValueError:
        detail = {}
    print(
        f"HTTP {error.response.status_code}: code={detail.get('code')} inner={detail.get('innerError', {}).get('code')} request-id={error.response.headers.get('request-id')}"
    )
    print(f"Graph explanation: {detail.get('message', 'No message returned')}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--user-id", type=UUID, help="Organizer Entra object ID (preferred if you already know it)."
    )
    parser.add_argument(
        "--user-email",
        help="Organizer work email or user principal name; used when you do not know the object ID.",
    )
    parser.add_argument("--meeting-url", required=True)
    parser.add_argument(
        "--replay",
        action="store_true",
        help="Replay available artifacts into the configured NoteIQ webhook; may notify the enrolled organizer.",
    )
    args = parser.parse_args()
    if args.user_id is None and args.user_email is None:
        raise SystemExit("Provide either --user-id or --user-email.")
    try:
        asyncio.run(
            diagnose(
                str(args.user_id) if args.user_id else None,
                args.meeting_url,
                args.replay,
                args.user_email,
            )
        )
    except httpx.HTTPStatusError as error:
        report(error)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
