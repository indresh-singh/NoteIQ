"""Resolve both modern Teams join links and legacy meeting URLs."""

import re
from urllib.parse import urlsplit


def meeting_filter(link: str) -> str:
    parsed = urlsplit(link)
    if parsed.scheme != "https" or parsed.hostname not in {"teams.microsoft.com", "teams.live.com"}:
        raise ValueError("Use a Teams HTTPS join link.")
    short = re.fullmatch(r"/meet/(\d+)/?", parsed.path)
    if short:
        return f"joinMeetingIdSettings/joinMeetingId eq '{short[1]}'"
    return "JoinWebUrl eq '" + link.replace("'", "''") + "'"


def meeting_participants(meeting: dict) -> list[dict] | None:
    """Organizer and attendees as {name, email, organizer}, for email drafts.

    Returns None when Graph omitted participants so a narrowed response never
    erases a list saved from an earlier, complete one.
    """
    participants = meeting.get("participants") or {}
    people = []
    seen = set()
    for info, organizer in [(participants.get("organizer"), True)] + [
        (attendee, False) for attendee in participants.get("attendees") or []
    ]:
        if not isinstance(info, dict):
            continue
        identity = info.get("identity") or {}
        user = identity.get("user") or identity.get("guest") or identity.get("phone") or {}
        email = (info.get("upn") or "").strip()
        name = (user.get("displayName") or "").strip()
        key = email.lower() or name.lower()
        if not key or key in seen:
            continue
        seen.add(key)
        people.append({"name": name or None, "email": email or None, "organizer": organizer})
    return people or None
