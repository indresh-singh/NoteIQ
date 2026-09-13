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
