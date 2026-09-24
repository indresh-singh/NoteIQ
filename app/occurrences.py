"""Project a meeting umbrella into isolated Teams call sessions.

Calendar occurrences are deliberately out of scope. A call is an actual Teams
session; rejoining after everyone leaves may create another session.
"""

import hashlib
import json
from datetime import datetime, timezone


def entries(content: dict, kind: str) -> list[dict]:
    return content.get(kind + "s") or ([{kind: content[kind]}] if content.get(kind) else [])


def session_key(transcript: dict) -> str:
    if transcript.get("callId"):
        return "call:" + transcript["callId"]
    # No day/time clustering: missing metadata must never combine separate calls.
    return "transcript:" + str(transcript.get("source_id") or transcript["id"])


def transcript_version(content: dict) -> str:
    records = sorted(
        json.dumps(e["transcript"], sort_keys=True) for e in entries(content, "transcript")
    )
    return hashlib.sha256(json.dumps(records).encode()).hexdigest()


def timestamp(value) -> float:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return (parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)).timestamp()
    except (ValueError, TypeError, OverflowError):
        return 0


def sessions(content: dict) -> list[dict]:
    groups: dict[str, dict] = {}
    correlations: dict[str, set[str]] = {}
    for entry in entries(content, "transcript"):
        transcript = entry["transcript"]
        key = session_key(transcript)
        group = groups.setdefault(key, {"id": key, "transcripts": [], "insights": []})
        group["transcripts"].append(entry)
        correlation = transcript.get("contentCorrelationId")
        if correlation:
            correlations.setdefault(correlation, set()).add(key)
    for entry in entries(content, "insight"):
        insight = entry["insight"]
        key = insight.get("occurrence_id")
        if not key and (insight.get("provider") or "copilot") == "copilot":
            matches = correlations.get(insight.get("contentCorrelationId"), set())
            if len(matches) == 1:
                key = next(iter(matches))
        # Legacy combined AI outputs cannot be assigned to a session, even when
        # only one transcript has been recovered so far.
        if key in groups:
            groups[key]["insights"].append(entry)
    for group in groups.values():
        group["transcripts"].sort(
            key=lambda entry: timestamp(entry["transcript"].get("createdDateTime"))
        )
        dates = [
            entry["transcript"].get("createdDateTime")
            for entry in group["transcripts"]
            if timestamp(entry["transcript"].get("createdDateTime"))
        ]
        group["started_at"] = dates[0] if dates else None
        group["metadata_pending"] = not group["id"].startswith("call:")
    return sorted(groups.values(), key=lambda group: timestamp(group["started_at"]), reverse=True)


def requires_sessions(content: dict) -> bool:
    if content.get("source") == "upload":
        return False
    return (
        (content.get("meeting_metadata") or {}).get("meeting_type") == "recurring"
        or any(e["transcript"].get("callId") for e in entries(content, "transcript"))
        or len(entries(content, "transcript")) > 1
    )


def select_session(content: dict, occurrence_id: str | None) -> dict:
    """Validate a client selection against this user's already authorized record."""
    if occurrence_id is None and not requires_sessions(content):
        return content
    if occurrence_id is None:
        raise ValueError("Select a meeting session first.")
    group = next((g for g in sessions(content) if g["id"] == occurrence_id), None)
    if group is None:
        raise KeyError(occurrence_id)
    selected = {
        k: v
        for k, v in content.items()
        if k not in {"transcript", "transcripts", "insight", "insights", "card", "occurrences"}
    }
    selected.update(
        transcripts=group["transcripts"],
        insights=group["insights"],
        occurrence_id=occurrence_id,
        started_at=group["started_at"],
    )
    return selected


def present_meeting(meeting: dict) -> dict:
    content = meeting["content"]
    if not requires_sessions(content):
        return meeting
    groups = sessions(content)
    assigned = sum(len(group["insights"]) for group in groups)
    return {
        **meeting,
        "content": {
            **content,
            "occurrences": groups,
            "unassigned_insights": len(entries(content, "insight")) - assigned,
        },
    }
