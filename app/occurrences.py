"""Project a meeting umbrella into isolated Teams call sessions.

Calendar occurrences are deliberately out of scope. A call is an actual Teams
session; rejoining after everyone leaves may create another session.
"""

import hashlib
import json
from datetime import datetime, timezone

from app.insight_limits import limit_openai_actions


def entries(content: dict, kind: str) -> list[dict]:
    values = content.get(kind + "s") or ([{kind: content[kind]}] if content.get(kind) else [])
    if kind != "transcript":
        return values
    # Historical list/detail aliases can share a recording correlation and
    # timestamp despite different IDs. Feed and display that transcript once.
    kept, index = [], {}
    for entry in values:
        transcript = entry["transcript"]
        correlation, created = (
            transcript.get("contentCorrelationId"),
            transcript.get("createdDateTime"),
        )
        key = (correlation, created) if correlation and created else None
        if key is not None and key in index:
            at = index[key]
            kept[at] = {
                "transcript": {
                    **kept[at]["transcript"],
                    **{k: v for k, v in transcript.items() if v is not None},
                }
            }
        else:
            if key is not None:
                index[key] = len(kept)
            kept.append(entry)
    return kept


def session_key(transcript: dict, content: dict | None = None) -> str:
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
    transcripts = entries(content, "transcript")
    for entry in transcripts:
        transcript = entry["transcript"]
        key = session_key(transcript, content)
        group = groups.setdefault(key, {"id": key, "transcripts": [], "insights": []})
        group["transcripts"].append(entry)
        correlation = transcript.get("contentCorrelationId")
        if correlation:
            correlations.setdefault(correlation, set()).add(key)
    for entry in entries(content, "insight"):
        insight = entry["insight"]
        key = insight.get("occurrence_id")
        # A legacy whole-meeting summary is safe when Graph proves every
        # transcript belongs to one actual call. Invite type is irrelevant:
        # scheduled and recurring links can both be joined more than once.
        one_known_call = len(groups) == 1 and all(
            e["transcript"].get("callId") for e in transcripts
        )
        if one_known_call and not key:
            key = next(iter(groups))
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
        group["metadata_pending"] = group["id"].startswith("transcript:")

        # A regenerated whole-meeting summary supersedes its legacy equivalent.
        def rank(entry):
            key = entry["insight"].get("occurrence_id")
            return 2 if key == group["id"] else 1 if key else 0

        best_rank = {
            provider: max(
                (rank(e) for e in group["insights"] if e["insight"].get("provider") == provider),
                default=0,
            )
            for provider in ("openai", "openrouter")
        }
        group["insights"] = [
            e
            for e in group["insights"]
            if rank(e) >= best_rank.get(e["insight"].get("provider"), 0)
        ]
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
        return limit_openai_actions({**content, "transcripts": entries(content, "transcript")})
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
    return limit_openai_actions(selected)


def present_meeting(meeting: dict) -> dict:
    content = meeting["content"]
    if not requires_sessions(content):
        return {
            **meeting,
            "content": limit_openai_actions(
                {**content, "transcripts": entries(content, "transcript")}
            ),
        }
    groups = sessions(content)
    assigned_ids = {id(e["insight"]) for group in groups for e in group["insights"]}
    # Old AI summaries remain stored for history, but stop warning after every
    # current session has a replacement from that provider.
    replaced_providers = {
        provider
        for provider in ("openai", "openrouter")
        if groups
        and all(
            any(e["insight"].get("provider") == provider for e in g["insights"]) for g in groups
        )
    }
    unassigned = sum(
        id(e["insight"]) not in assigned_ids
        and e["insight"].get("provider") not in replaced_providers
        for e in entries(content, "insight")
    )
    return {
        **meeting,
        "content": {
            **content,
            "occurrences": [limit_openai_actions(group) for group in groups],
            "unassigned_insights": unassigned,
        },
    }
